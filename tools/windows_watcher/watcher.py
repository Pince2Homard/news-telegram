"""Read-only GitHub comment watcher. It never executes comment content."""
from __future__ import annotations

import argparse
import hashlib
import time
from email.utils import parsedate_to_datetime
from contextlib import contextmanager
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


if __package__:
    from . import coordination, publication_tracking
else:
    import coordination
    import publication_tracking


API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
DEFAULT_REPOSITORY = "Pince2Homard/news-telegram"
DEFAULT_COORDINATION_ISSUE = 7
DEFAULT_POLL_INTERVAL_MINUTES = 30
MAX_API_PAGES = 100
MAX_PENDING_EVENTS = 1000
MAX_HISTORY_EVENTS = 500
MAX_SEEN_VERSIONS = 4096
MAX_TRACKED_PRS = 100
REQUEST_TIMEOUT_SECONDS = 20
PREFIX_CATEGORIES = {
    "CHATGPT-DECISION": "decision",
    "OWNER-DECISION": "decision",
    "CODEX-REPORT": "report",
    "CHATGPT-REVIEW": "review",
    "CHATGPT-TASK": "task",
}
PREFIX_PATTERN = re.compile(r"^\s*(?:#{1,6}\s*)?\[([A-Z0-9-]+)\]", re.IGNORECASE)
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

_POWERSHELL_BALLOON = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$rawCount = [Console]::In.ReadLine()
$count = 0
if (-not [int]::TryParse($rawCount, [ref]$count) -or $count -lt 1) { exit 2 }
$icon = New-Object System.Windows.Forms.NotifyIcon
$icon.Icon = [System.Drawing.SystemIcons]::Information
$icon.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info
$icon.BalloonTipTitle = 'News Telegram — GitHub'
$icon.BalloonTipText = "$count nouvel(s) élément(s) de coordination. Consultez l'historique local et GitHub."
$icon.Visible = $true
$icon.ShowBalloonTip(10000)
Start-Sleep -Seconds 10
$icon.Dispose()
"""


class WatcherError(RuntimeError):
    """Safe operational error; response bodies and comment text are omitted."""


class RateLimitError(WatcherError):
    def __init__(self, retry_at):
        self.retry_at = retry_at
        super().__init__(f"GitHub différé jusqu’à epoch {retry_at:.0f}; curseurs conservés.")


def _retry_at(headers, now):
    deadlines = [now + 60]
    retry = headers.get("Retry-After")
    try:
        deadlines.append(now + max(0, int(retry)))
    except (TypeError, ValueError):
        try:
            deadlines.append(parsedate_to_datetime(retry).timestamp())
        except (TypeError, ValueError, OverflowError):
            pass
    if headers.get("X-RateLimit-Remaining") == "0":
        try:
            deadlines.append(float(headers.get("X-RateLimit-Reset")) + 1)
        except (TypeError, ValueError):
            pass
    return max(value for value in deadlines if math.isfinite(value))


def default_data_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "NewsTelegramWatcher"
    return Path.home() / "AppData" / "Local" / "NewsTelegramWatcher"


def default_config() -> dict:
    return {
        "repository": DEFAULT_REPOSITORY,
        "coordination_issue": DEFAULT_COORDINATION_ISSUE,
        "authorized_authors": ["Pince2Homard"],
        "poll_interval_minutes": DEFAULT_POLL_INTERVAL_MINUTES,
    }


def validate_config(config: object) -> dict:
    if not isinstance(config, dict):
        raise WatcherError("Le fichier de configuration doit être un objet JSON.")
    repository = config.get("repository")
    if not isinstance(repository, str) or not REPOSITORY_PATTERN.fullmatch(repository):
        raise WatcherError("repository doit être au format propriétaire/dépôt.")
    issue_number = config.get("coordination_issue", DEFAULT_COORDINATION_ISSUE)
    if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number < 1:
        raise WatcherError("coordination_issue doit être un entier positif.")
    authors = config.get("authorized_authors", [])
    if not isinstance(authors, list) or any(
            not isinstance(author, str) or not author.strip() for author in authors):
        raise WatcherError("authorized_authors doit être une liste de logins GitHub.")
    interval = config.get("poll_interval_minutes", DEFAULT_POLL_INTERVAL_MINUTES)
    if isinstance(interval, bool) or not isinstance(interval, int) or not 15 <= interval <= 1440:
        raise WatcherError("poll_interval_minutes doit être compris entre 15 et 1440.")
    return {
        "repository": repository,
        "coordination_issue": issue_number,
        "authorized_authors": sorted({author.strip().casefold() for author in authors}),
        "poll_interval_minutes": interval,
    }


def load_config(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as source:
            return validate_config(json.load(source))
    except FileNotFoundError as exc:
        raise WatcherError(
            f"Configuration absente : {path}. Copiez config.example.json dans le dossier local."
        ) from exc
    except json.JSONDecodeError as exc:
        raise WatcherError("Configuration JSON invalide; elle n'a pas été réinitialisée.") from exc


def _empty_state() -> dict:
    return {
        "schema_version": 1,
        "initialized": False,
        "threads": {},
        "pending_notifications": [],
        "seen_event_versions": [],
        "history": [],
        "inbox": [],
    }


def load_state(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as source:
            state = json.load(source)
    except FileNotFoundError:
        return _empty_state()
    except json.JSONDecodeError as exc:
        raise WatcherError("État JSON illisible; il n'a pas été réinitialisé.") from exc
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise WatcherError("Schéma d'état inconnu; aucune réinitialisation automatique.")
    for name in ("threads",):
        if not isinstance(state.get(name), dict):
            raise WatcherError(f"Format invalide dans l'état local : {name}.")
    for name in ("pending_notifications", "seen_event_versions", "history"):
        if not isinstance(state.get(name), list):
            raise WatcherError(f"Format invalide dans l'état local : {name}.")
    for key, thread in state["threads"].items():
        if (not isinstance(key, str) or not isinstance(thread, dict)
                or not isinstance(thread.get("cursors"), dict)):
            raise WatcherError("Format invalide dans les curseurs locaux.")
        for cursor in thread["cursors"].values():
            if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
                raise WatcherError("Curseur local invalide; aucun reset effectué.")
    for version in state["seen_event_versions"]:
        if not isinstance(version, str):
            raise WatcherError("Version d'événement locale invalide.")
    for name in ("pending_notifications", "history"):
        if any(not isinstance(event, dict) for event in state[name]):
            raise WatcherError(f"Format invalide dans l'historique local : {name}.")
    state["seen_event_versions"] = state["seen_event_versions"][-MAX_SEEN_VERSIONS:]
    state["history"] = state["history"][-MAX_HISTORY_EVENTS:]
    if len(state["pending_notifications"]) > MAX_PENDING_EVENTS:
        raise WatcherError("Boîte locale trop grande; aucun événement n'a été supprimé.")
    retry_at = state.get("next_poll_at", 0)
    if isinstance(retry_at, bool) or not isinstance(retry_at, (int, float)) or not math.isfinite(retry_at) or retry_at < 0:
        raise WatcherError("Échéance API invalide.")
    for thread in state["threads"].values():
        if not isinstance(thread.get("fingerprints", {}), dict):
            raise WatcherError("Empreintes locales invalides.")
    for field in ("coordination_registry", "pr_snapshots", "ci_snapshots", "publication_receipts"):
        if not isinstance(state.get(field, {}), dict):
            raise WatcherError("Registre ou instantané local invalide ; état conservé.")
    for identity, record in state.get("coordination_registry", {}).items():
        if (not isinstance(record, dict) or not isinstance(record.get("event"), dict)
                or not isinstance(record.get("references"), list)
                or type(record.get("conflict")) is not bool):
            raise WatcherError("Entrée du registre invalide ; état conservé.")
        event = record["event"]
        envelope = event.get("coordination")
        if not isinstance(envelope, dict):
            raise WatcherError("Enveloppe locale invalide ; état conservé.")
        canonical, error = coordination.parse_envelope(
            "```json\n" + json.dumps(envelope) + "\n```", state.get("repository", DEFAULT_REPOSITORY),
        )
        if error or canonical != envelope or identity != envelope["coordination_id"]:
            raise WatcherError("Enveloppe locale invalide ; état conservé.")
        if (event.get("prefix") not in PREFIX_CATEGORIES
                or not isinstance(event.get("event_id"), str)
                or not isinstance(event.get("event_key"), str)
                or type(event.get("authorized_author")) is not bool
                or not coordination.github_url(event.get("url"), envelope["repository"])
                or any(not coordination.github_url(url, envelope["repository"]) for url in record["references"])):
            raise WatcherError("Référence du registre invalide ; état conservé.")
    for snapshot in state.get("pr_snapshots", {}).values():
        if not isinstance(snapshot, dict):
            raise WatcherError("Instantané PR invalide ; état conservé.")
    for snapshot in state.get("ci_snapshots", {}).values():
        if not isinstance(snapshot, dict) or snapshot.get("result") not in {"success", "failure", "pending", "unknown", "incomplete"}:
            raise WatcherError("Instantané CI invalide ; état conservé.")
    for identity, receipt in state.get("publication_receipts", {}).items():
        try:
            publication_tracking.anchor(receipt["receipt_path"], identity, receipt["expected_url"],
                                        receipt["approval_digest"], state.get("repository", DEFAULT_REPOSITORY))
            if (receipt["report_id"] != identity or receipt["status"] not in publication_tracking.STATUSES
                    or not isinstance(receipt["checked_at"], (int, float)) or not math.isfinite(receipt["checked_at"])
                    or not isinstance(receipt["reason"], str) or not re.fullmatch(r"[a-z_]{1,50}", receipt["reason"])):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise WatcherError("Suivi de publication invalide ; état conservé.") from None
    state.setdefault("initialized", False)
    migrate_history = "inbox" not in state
    inbox = state.setdefault("inbox", [])
    if not isinstance(inbox, list) or any(not isinstance(e, dict) for e in inbox):
        raise WatcherError("Boîte locale invalide; aucune réinitialisation.")
    for event in inbox + state["history"] + state["pending_notifications"]:
        if not isinstance(event.get("event_key"), str) or not isinstance(event.get("url"), str):
            raise WatcherError("Référence d'événement invalide ; état conservé.")
    by_key = {e.get("event_key"): e for e in inbox}
    for event in (state["history"] if migrate_history else []) + state["pending_notifications"]:
        by_key.setdefault(event.get("event_key"), dict(event))
    state["inbox"] = list(by_key.values())
    for event in state["inbox"]:
        event.setdefault("event_id", hashlib.sha256(str(event.get("event_key")).encode()).hexdigest()[:20])
        event.setdefault("status", "new")
        if event["status"] not in {"new", "acknowledged", "done", "ignored"}:
            raise WatcherError("Statut local inconnu.")
    if sum(e["status"] in {"new", "acknowledged"} for e in state["inbox"]) > MAX_PENDING_EVENTS:
        raise WatcherError("Boîte non traitée trop grande; aucun événement supprimé.")
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=str(path.parent),
                prefix=f".{path.name}.", suffix=".tmp", delete=False) as output:
            temporary_path = Path(output.name)
            json.dump(state, output, ensure_ascii=False, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


@contextmanager
def single_instance_lock(path: Path):
    """OS-released exclusive lock prevents overlapping scheduled polls."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError:
                raise WatcherError("Un relevé est déjà en cours; ce passage est ignoré.") from None
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                raise WatcherError("Un relevé est déjà en cours; ce passage est ignoré.") from None
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class GitHubReader:
    """Anonymous, GET-only GitHub REST client for this public repository."""

    def __init__(self, timeout: int = REQUEST_TIMEOUT_SECONDS, clock=time.time):
        self.timeout = timeout
        self.clock = clock
        self.next_request_at = 0
        self.requests_made = 0

    def get_json(self, path: str):
        if self.clock() < self.next_request_at:
            raise RateLimitError(self.next_request_at)
        if self.requests_made >= 50:
            raise RateLimitError(self.clock() + 3600)
        self.requests_made += 1
        if not path.startswith("/repos/") or ".." in path or path.startswith("//"):
            raise WatcherError("Chemin API refusé.")
        request = Request(
            API_ROOT + path,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "news-telegram-readonly-watcher/1.0",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                headers = getattr(response, "headers", {})
                if headers.get("X-RateLimit-Remaining") == "0":
                    self.next_request_at = _retry_at(headers, self.clock())
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code in (403, 429):
                raise RateLimitError(_retry_at(exc.headers or {}, self.clock())) from None
            raise WatcherError(f"GitHub API HTTP {exc.code}; état non avancé.") from None
        except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError):
            raise WatcherError("GitHub API inaccessible ou réponse invalide; état non avancé.") from None

    def get_pages(self, path: str) -> list:
        results = []
        for page in range(1, MAX_API_PAGES + 1):
            separator = "&" if "?" in path else "?"
            payload = self.get_json(
                f"{path}{separator}{urlencode({'per_page': 100, 'page': page})}"
            )
            if not isinstance(payload, list):
                raise WatcherError("GitHub API a retourné une liste invalide.")
            results.extend(payload)
            if len(payload) < 100:
                return results
        raise WatcherError("Pagination dépassée; l'état n'a pas été avancé.")


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _extract_prefix(body: object) -> str | None:
    if not isinstance(body, str):
        return None
    for line in body.splitlines()[:20]:
        match = PREFIX_PATTERN.match(line)
        if match:
            prefix = match.group(1).upper()
            return prefix if prefix in PREFIX_CATEGORIES else None
    return None


def _thread_url(repository: str, thread_type: str, number: int, source: str,
                comment_id: int) -> str:
    path_kind = "issues" if thread_type == "issue" else "pull"
    if source == "review_comments":
        anchor = f"discussion_r{comment_id}"
    elif source == "reviews":
        anchor = f"pullrequestreview-{comment_id}"
    else:
        anchor = f"issuecomment-{comment_id}"
    return f"https://github.com/{repository}/{path_kind}/{number}#{anchor}"


def _event_from_comment(config: dict, thread: dict, source: str,
                        comment: dict) -> dict | None:
    prefix = _extract_prefix(comment.get("body"))
    if prefix is None:
        return None
    user = comment.get("user")
    author = user.get("login") if isinstance(user, dict) else None
    author = author if isinstance(author, str) else "unknown"
    trusted = author.casefold() in config["authorized_authors"]
    comment_id = comment.get("id")
    if isinstance(comment_id, bool) or not isinstance(comment_id, int) or comment_id < 1:
        return None
    fingerprint = hashlib.sha256(json.dumps(
        [comment.get("body"), author, comment.get("updated_at"), comment.get("state")],
        ensure_ascii=True, separators=(",", ":"),
    ).encode()).hexdigest()
    identity = f"{config['repository']}:{thread['key']}:{source}:{comment_id}:{fingerprint}"
    envelope, protocol_error = coordination.parse_envelope(comment.get("body"), config["repository"])
    if envelope and envelope["kind"] != PREFIX_CATEGORIES[prefix]:
        envelope, protocol_error = None, "prefix_kind_mismatch"
    return {
        "coordination": envelope,
        "protocol_error": protocol_error,
        "content_digest": hashlib.sha256(comment.get("body", "").encode()).hexdigest(),
        "event_id": hashlib.sha256(identity.encode()).hexdigest()[:20],
        "status": "new",
        "event_key": (
            f"{thread['key']}:{source}:{comment_id}:"
            f"{comment.get('updated_at') or comment.get('created_at') or ''}:{fingerprint}"
        ),
        "thread": thread["key"],
        "number": thread["number"],
        "thread_type": thread["type"],
        "source": source,
        "comment_id": comment_id,
        "prefix": prefix,
        "category": PREFIX_CATEGORIES[prefix],
        "author": author,
        "authorized_author": trusted,
        "created_at": comment.get("created_at"),
        "url": _thread_url(
            config["repository"], thread["type"], thread["number"], source, comment_id,
        ),
    }


def _sources_for_thread(repository: str, thread: dict) -> dict:
    root = f"/repos/{repository}"
    number = thread["number"]
    if thread["type"] == "issue":
        return {"comments": f"{root}/issues/{number}/comments"}
    return {
        "issue_comments": f"{root}/issues/{number}/comments",
        "reviews": f"{root}/pulls/{number}/reviews",
        "review_comments": f"{root}/pulls/{number}/comments",
    }


def _new_thread_state(updated_at: object) -> dict:
    return {"updated_at": updated_at, "cursors": {}}


def _record_version(state: dict, key: str) -> None:
    versions = state.setdefault("seen_event_versions", [])
    if key not in versions:
        versions.append(key)
    state["seen_event_versions"] = versions[-MAX_SEEN_VERSIONS:]


class Watcher:
    def __init__(self, config: dict, state_path: Path, client=None, notifier=None):
        self.config = validate_config(config)
        self.state_path = Path(state_path)
        self.client = client or GitHubReader()
        self.notifier = notifier or show_windows_notification

    def _fetch_threads(self, state) -> list:
        repository = self.config["repository"]
        issue_number = self.config["coordination_issue"]
        root = f"/repos/{repository}"
        issue = self.client.get_json(f"{root}/issues/{issue_number}")
        if not isinstance(issue, dict):
            raise WatcherError("Métadonnées d'issue invalides; état non avancé.")
        pulls = self.client.get_pages(f"{root}/pulls?state=open&sort=updated&direction=desc")
        threads = [{
            "key": f"issue:{issue_number}", "type": "issue", "number": issue_number,
            "updated_at": issue.get("updated_at"),
        }]
        snapshots = state.setdefault("pr_snapshots", {})
        valid_pulls = [pull for pull in pulls if isinstance(pull, dict)]
        if len(valid_pulls) > MAX_TRACKED_PRS:
            raise WatcherError("Plus de 100 PR ouvertes; relever l'intervalle avant de continuer.")
        for pull in valid_pulls:
            number = pull.get("number")
            if isinstance(number, int) and not isinstance(number, bool) and number > 0:
                head = pull.get("head", {}).get("sha")
                snapshots[str(number)] = {"head_sha": head if isinstance(head, str) and coordination.SHA.fullmatch(head) else None,
                                          "closed": False}
                threads.append({
                    "key": f"pr:{number}", "type": "pr", "number": number,
                    "updated_at": pull.get("updated_at"),
                })
        # Un dernier relevé des PR suivies qui viennent de fermer.
        active = {thread["key"] for thread in threads}
        for key, previous in state["threads"].items():
            if key.startswith("pr:") and key not in active and not previous.get("closed_scanned"):
                number = int(key.split(":")[1])
                closed = self.client.get_json(f"{root}/pulls/{number}")
                snapshots[str(number)] = {"head_sha": closed.get("head", {}).get("sha"), "closed": True}
                threads.append({"key": key, "type": "pr", "number": number,
                                "updated_at": closed.get("updated_at"), "closed_scanned": True})
        return threads

    def _scan_thread(self, state: dict, thread: dict, is_baseline: bool) -> list:
        repository = self.config["repository"]
        prior = state["threads"].get(thread["key"], _new_thread_state(None))
        prior_updated = _timestamp(prior.get("updated_at"))
        snapshot_updated = _timestamp(thread.get("updated_at"))
        cursors = prior.setdefault("cursors", {})
        fingerprints = prior.get("fingerprints", {})
        next_fingerprints = {}
        found = []
        for source, path in _sources_for_thread(repository, thread).items():
            entries = self.client.get_pages(path)
            entries = [
                entry for entry in entries
                if isinstance(entry, dict) and isinstance(entry.get("id"), int)
                and not isinstance(entry.get("id"), bool) and entry["id"] > 0
            ]
            entries.sort(key=lambda entry: entry["id"])
            # Une suppression ou édition qui retire le préfixe/JSON doit aussi
            # invalider les anciens éléments, même sans nouvel événement reconnu.
            present = {entry["id"]: entry for entry in entries}
            for record in state.get("coordination_registry", {}).values():
                origin = record["event"]
                if origin["thread"] != thread["key"] or origin["source"] != source:
                    continue
                current = present.get(origin["comment_id"])
                if current is None:
                    record["source_missing"] = True
                    continue
                body = current.get("body")
                user = current.get("user")
                author = user.get("login") if isinstance(user, dict) else None
                if (not isinstance(body, str) or hashlib.sha256(body.encode()).hexdigest() != origin["content_digest"]
                        or author != origin["author"]):
                    record["source_changed"] = True
            high_water = int(cursors.get(source, 0))
            newest = high_water
            for comment in entries:
                if not isinstance(comment, dict):
                    continue
                comment_id = comment.get("id")
                if isinstance(comment_id, bool) or not isinstance(comment_id, int) or comment_id < 1:
                    continue
                newest = max(newest, comment_id)
                event = _event_from_comment(self.config, thread, source, comment)
                if event:
                    try:
                        coordination.register(state, event)
                    except ValueError:
                        raise WatcherError("Registre de coordination plein ; curseurs conservés.") from None
                updated = _timestamp(comment.get("updated_at") or comment.get("created_at"))
                created = _timestamp(comment.get("created_at"))
                version_key = event["event_key"] if event else ""
                item_key = f"{source}:{comment_id}"
                # Même un commentaire sans préfixe reçoit une empreinte :
                # l'ajout ultérieur d'un marqueur dans la même seconde est visible.
                digest = hashlib.sha256(json.dumps(comment, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
                next_fingerprints[item_key] = digest

                if is_baseline:
                    # Quietly establish a baseline. A comment later than the
                    # metadata snapshot is retained as a candidate for next poll.
                    if updated is not None and snapshot_updated is not None and updated <= snapshot_updated:
                        if event:
                            _record_version(state, version_key)
                        continue
                    if created is not None and snapshot_updated is not None and created > snapshot_updated:
                        if event and not event.get("coordination_duplicate") and version_key not in state["seen_event_versions"]:
                            found.append(event)
                            _record_version(state, version_key)
                    continue

                is_new = comment_id > high_water
                is_edit = (item_key in fingerprints and fingerprints[item_key] != digest) or (
                    prior_updated is not None and updated is not None
                    and updated > prior_updated
                )
                if not (is_new or is_edit):
                    continue
                if event and not event.get("coordination_duplicate") and version_key not in state["seen_event_versions"]:
                    found.append(event)
                _record_version(state, version_key)
            cursors[source] = newest
        state["threads"][thread["key"]] = {
            "fingerprints": dict(list(next_fingerprints.items())[-MAX_SEEN_VERSIONS:]),
            "closed_scanned": thread.get("closed_scanned", False),
            "updated_at": thread.get("updated_at"), "cursors": cursors,
        }
        return found

    def _deliver_pending(self, state: dict) -> int:
        pending = state["pending_notifications"]
        if not pending:
            return 0
        # Commit the inbox before showing a Windows notification. If the process
        # stops between display and acknowledgment, the summary may repeat once;
        # keeping the durable inbox avoids silently losing an intervention.
        if not self.notifier(len(pending)):
            raise WatcherError("Notification Windows non confirmée; événements conservés.")
        state["history"] = (state.get("history", []) + pending)[-MAX_HISTORY_EVENTS:]
        state["pending_notifications"] = []
        save_state(self.state_path, state)
        return len(pending)

    def run_once(self, notify=True, transfer_id=None, prepare_ready=False) -> dict:
        lock_path = self.state_path.with_name("watcher.lock")
        with single_instance_lock(lock_path):
            state = load_state(self.state_path)
            if time.time() < state.get("next_poll_at", 0):
                raise RateLimitError(state["next_poll_at"])
            try:
                result = self._run_once_locked(notify=notify)
                if transfer_id:
                    result["prepared_transfer"] = str(self._prepare_transfer_locked(transfer_id))
                elif prepare_ready:
                    # Préparation de données seulement, lors du relevé manuel.
                    # Un paquet existant n'est jamais réécrit ou relancé.
                    state = load_state(self.state_path)
                    result["prepared_transfers"] = []
                    for identity in state.get("coordination_registry", {}):
                        try:
                            coordination.packet_data(state, identity, self.config["repository"], time.time())
                        except ValueError:
                            continue
                        target = self.state_path.parent / "transfers" / identity
                        if not target.exists():
                            result["prepared_transfers"].append(str(self._prepare_transfer_locked(identity)))
                return result
            except RateLimitError as exc:
                # Recharger l'état durable : aucun curseur partiel en mémoire.
                state = load_state(self.state_path)
                state["next_poll_at"] = exc.retry_at
                self._invalidate_receipts(state, "rate_limited")
                save_state(self.state_path, state)
                raise
            except WatcherError:
                state = load_state(self.state_path)
                if self._invalidate_receipts(state, "refresh_failed"):
                    save_state(self.state_path, state)
                raise

    def _run_once_locked(self, notify=True) -> dict:
        state = load_state(self.state_path)
        pending_delivered = self._deliver_pending(state) if notify else 0
        if state.get("repository", self.config["repository"]) != self.config["repository"]:
            raise WatcherError("Le dépôt diffère de celui des curseurs; utiliser un autre dossier local.")
        state["repository"] = self.config["repository"]
        threads = self._fetch_threads(state)
        baseline = not state.get("initialized", False)
        new_events = []
        for thread in threads:
            new_events.extend(self._scan_thread(
                state, thread, is_baseline=baseline,
            ))
        self._refresh_ci(state)
        self._refresh_receipts(state)
        state["last_poll_at"] = datetime.now(timezone.utc).isoformat()
        existing_keys = {event.get("event_key") for event in state["pending_notifications"]}
        new_events = [event for event in new_events if event["event_key"] not in existing_keys]
        if len(state["pending_notifications"]) + len(new_events) > MAX_PENDING_EVENTS:
            raise WatcherError("Trop d'événements en attente; état et curseurs non sauvegardés.")
        inbox = state.setdefault("inbox", [])
        known = {e["event_key"] for e in inbox}
        inbox.extend(e for e in new_events if e["event_key"] not in known)
        open_events = [e for e in inbox if e["status"] in {"new", "acknowledged"}]
        if len(open_events) > MAX_PENDING_EVENTS:
            raise WatcherError("Boîte non traitée pleine; curseurs non sauvegardés.")
        handled = [e for e in inbox if e["status"] in {"done", "ignored"}]
        state["inbox"] = handled[-MAX_HISTORY_EVENTS:] + open_events
        state.pop("next_poll_at", None)
        if getattr(self.client, "next_request_at", 0) > time.time():
            state["next_poll_at"] = self.client.next_request_at
        state["pending_notifications"].extend(new_events)
        state["initialized"] = True
        save_state(self.state_path, state)
        new_count = self._deliver_pending(state) if notify else 0
        return {
            "baseline": baseline,
            "threads_checked": len(threads),
            "new_events": len(new_events),
            "notifications_delivered": new_count + pending_delivered,
            "pending_events": len(state["pending_notifications"]),
        }

    @staticmethod
    def _invalidate_receipts(state, reason):
        entries = state.get("publication_receipts", {})
        for entry in entries.values():
            entry.update(status="uncertain", reason=reason, checked_at=time.time())
        return bool(entries)

    def _refresh_receipts(self, state):
        try:
            entries = publication_tracking.discover(self.state_path.parent / "publication-receipts", self.config["repository"])
            for entry in entries:
                identity = entry["report_id"]
                previous = state.get("publication_receipts", {}).get(identity)
                if previous and any(previous[key] != entry[key] for key in ("expected_url", "approval_digest")):
                    raise ValueError("contradictory attachment")
                if not previous:
                    state.setdefault("publication_receipts", {})[identity] = entry
        except (OSError, ValueError, KeyError, TypeError):
            raise WatcherError("Rattachement durable invalide ; aucune association remplacée.") from None
        for identity, entry in state.get("publication_receipts", {}).items():
            state["publication_receipts"][identity] = publication_tracking.assess(
                entry, state, self.config["repository"], self.client, time.time(),
            )

    def import_receipt(self, path, identity, url, approved_digest):
        try:
            entry = publication_tracking.anchor(path, identity, url, approved_digest, self.config["repository"])
        except ValueError:
            raise WatcherError("Ancres du reçu invalides ; aucun import.") from None
        # Actualiser les commentaires via le parcours GET existant, sans notifier
        # ni préparer de transfert ou appeler le publicateur.
        self.run_once(notify=False)
        with single_instance_lock(self.state_path.with_name("watcher.lock")):
            state = load_state(self.state_path)
            previous = state.get("publication_receipts", {}).get(identity)
            if previous and any(previous[key] != entry[key] for key in ("expected_url", "approval_digest")):
                raise WatcherError("Un autre reçu est déjà associé à cet UUID ; arbitrage requis.")
            try:
                result = publication_tracking.assess(entry, state, self.config["repository"], self.client, time.time())
            except RateLimitError as exc:
                state["next_poll_at"] = exc.retry_at
                state.setdefault("publication_receipts", {})[identity] = {
                    **entry, "status": "uncertain", "reason": "rate_limited", "checked_at": time.time(),
                }
                save_state(self.state_path, state)
                raise
            try:
                result = publication_tracking.retain(result, self.state_path.parent / "publication-receipts")
            except (OSError, ValueError):
                raise WatcherError("Copie du reçu non concordante ; aucune pièce existante écrasée.") from None
            state.setdefault("publication_receipts", {})[identity] = result
            save_state(self.state_path, state)
            return publication_tracking.view(state, identity, time.time())

    def _refresh_ci(self, state):
        ci = state.setdefault("ci_snapshots", {})
        needed = {row["event"]["coordination"]["pull_request"]
                  for row in state.get("coordination_registry", {}).values()}
        for number in sorted(n for n in needed if isinstance(n, int)):
            snapshot = state.get("pr_snapshots", {}).get(str(number), {})
            head = snapshot.get("head_sha")
            if not head or snapshot.get("closed"):
                continue
            root = f"/repos/{self.config['repository']}/commits/{head}"
            checks = self.client.get_json(root + "/check-runs?per_page=100")
            statuses = self.client.get_json(root + "/status?per_page=100")
            try:
                result = coordination.ci_summary(checks, statuses)
            except (ValueError, TypeError, AttributeError):
                raise WatcherError("Réponse CI invalide ; état non avancé.") from None
            ci[head] = {"result": result, "checked_at": time.time()}
        state["ci_snapshots"] = dict(list(ci.items())[-100:])

    def _prepare_transfer_locked(self, identity):
        state = load_state(self.state_path)
        registry = state.get("coordination_registry", {})
        record = registry.get(identity)
        if not record:
            raise WatcherError("Identifiant de coordination inconnu.")
        try:
            packet = coordination.packet_data(state, identity, self.config["repository"], time.time())
        except ValueError as exc:
            raise WatcherError(str(exc)) from None
        event = record["event"]
        envelope = event["coordination"]
        root = f"/repos/{self.config['repository']}"
        pull = self.client.get_json(f"{root}/pulls/{envelope['pull_request']}")
        if pull.get("head", {}).get("sha") != envelope["head_sha"] or pull.get("state") != "open":
            raise WatcherError("SHA ou état de PR modifié ; transfert refusé.")
        # Revalider les références liées à la directive avant l'export, sans
        # corps persistant. Une course après ces lectures reste possible :
        # le lancement ultérieur exige une nouvelle validation humaine.
        candidates = [event, registry[packet["owner_declaration"]]["event"]]
        for candidate in candidates:
            source, comment_id = candidate["source"], candidate["comment_id"]
            if source in {"comments", "issue_comments"}:
                path = f"{root}/issues/comments/{comment_id}"
            elif source == "review_comments":
                path = f"{root}/pulls/comments/{comment_id}"
            else:
                path = f"{root}/pulls/{candidate['number']}/reviews/{comment_id}"
            current = self.client.get_json(path)
            rebuilt = _event_from_comment(self.config, {"key": candidate["thread"],
                "type": candidate["thread_type"], "number": candidate["number"]}, source, current)
            if not rebuilt or rebuilt["event_key"] != candidate["event_key"]:
                raise WatcherError("Commentaire modifié ou auteur différent ; transfert refusé.")
        try:
            return coordination.prepare_packet(state, identity, self.config["repository"],
                                               self.state_path.parent / "transfers", time.time())
        except ValueError as exc:
            raise WatcherError(str(exc)) from None

    def write_dashboard(self):
        with single_instance_lock(self.state_path.with_name("watcher.lock")):
            state = load_state(self.state_path)
            path = self.state_path.with_name("dashboard.html")
            content = coordination.dashboard(state, self.config["repository"], time.time(),
                                               self.state_path.parent / "transfers")
            # Remplacement atomique ; aucune ouverture automatique du navigateur.
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             delete=False, suffix=".html") as output:
                temporary = Path(output.name)
                output.write(content)
            try:
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return path

    def history(self) -> list:
        return load_state(self.state_path).get("history", [])


    def acknowledge(self, event_id, status):
        if status not in {"acknowledged", "done", "ignored"}:
            raise WatcherError("Statut de traitement invalide.")
        with single_instance_lock(self.state_path.with_name("watcher.lock")):
            state = load_state(self.state_path)
            event = next((e for e in state["inbox"] if e["event_id"] == event_id), None)
            if event is None:
                event = next((dict(row["event"]) for row in state.get("coordination_registry", {}).values()
                              if row["event"]["event_id"] == event_id), None)
                if event is None:
                    raise WatcherError("Identifiant local inconnu.")
                state["inbox"].append(event)
            event["status"] = status
            event["handled_at"] = datetime.now(timezone.utc).isoformat()
            save_state(self.state_path, state)

    def events(self):
        return load_state(self.state_path)["inbox"]


def show_windows_notification(event_count: int) -> bool:
    if os.name != "nt" or event_count < 1:
        return False
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command", _POWERSHELL_BALLOON],
            input=f"{int(event_count)}\n", text=True, capture_output=True,
            timeout=REQUEST_TIMEOUT_SECONDS, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


class _SimulationClient:
    def __init__(self):
        self.issue_updated = "2026-11-11T12:00:00Z"
        self.comments = []
        self.pulls = []

    def get_json(self, path):
        if path.endswith("/issues/7"):
            return {"updated_at": self.issue_updated}
        if "/issues/comments/" in path:
            number = int(path.rsplit("/", 1)[-1])
            return next(comment for comment in self.comments if comment["id"] == number)
        if path.endswith("/check-runs?per_page=100"):
            return {"total_count": 1, "check_runs": [{"status": "completed", "conclusion": "success"}]}
        if path.endswith("/status?per_page=100"):
            return {"total_count": 0, "statuses": []}
        raise AssertionError("endpoint inattendu en simulation")

    def get_pages(self, path):
        if "/pulls?state=open" in path:
            return list(self.pulls)
        if path.endswith("/issues/7/comments"):
            return list(self.comments)
        if path.endswith(("/issues/10/comments", "/pulls/10/comments", "/pulls/10/reviews")):
            return []
        raise AssertionError("endpoint inattendu en simulation")


def run_simulation() -> int:
    """Offline demo: fake API, temporary state, and no Windows notification."""
    config = validate_config(default_config())
    api = _SimulationClient()
    notifications = []

    def record_notification(count):
        notifications.append(count)
        return True

    with tempfile.TemporaryDirectory(prefix="news-telegram-watcher-") as directory:
        state_path = Path(directory) / "state.json"
        watcher = Watcher(config, state_path, api, record_notification)
        print("Simulation lecture seule : établissement du point de départ.")
        watcher.run_once()
        api.comments.append({
            "id": 1001,
            "user": {"login": "Pince2Homard"},
            "body": "[CHATGPT-DECISION] Exemple simulé; aucun texte ne sera exécuté.",
            "created_at": "2026-11-11T12:01:00Z",
            "updated_at": "2026-11-11T12:01:00Z",
        })
        api.issue_updated = "2026-11-11T12:01:00Z"
        result = watcher.run_once()
        task_id = "11111111-1111-4111-8111-111111111111"
        owner_id = "22222222-2222-4222-8222-222222222222"
        head = "a" * 40
        api.pulls = [{"number": 10, "head": {"sha": head}}]
        for number, identity, kind, prefix in (
            (1002, task_id, "task", "CHATGPT-TASK"),
            (1003, owner_id, "decision", "OWNER-DECISION"),
        ):
            envelope = {"schema_version": 1, "coordination_id": identity,
                        "repository": DEFAULT_REPOSITORY, "pull_request": 10,
                        "head_sha": head, "kind": kind,
                        "status": "authorized" if kind == "decision" else "proposed",
                        "requested_action": "review_only",
                        "reply_to": task_id if kind == "decision" else None,
                        "evidence_urls": [], "expires_at": None}
            api.comments.append({"id": number, "user": {"login": "Pince2Homard"},
                                 "body": f"[{prefix}]\n```json\n{json.dumps(envelope)}\n```",
                                 "created_at": "2026-11-11T12:02:00Z"})
        watcher.run_once(notify=False)
        state = load_state(state_path)
        packet = coordination.prepare_packet(state, task_id, DEFAULT_REPOSITORY,
                                              state_path.parent / "transfers", time.time())
        if __package__:
            from . import report_publication as formatter
        else:
            import report_publication as formatter
        report_id = "33333333-3333-4333-8333-333333333333"
        report = {"envelope": {**state["coordination_registry"][task_id]["event"]["coordination"],
                              "coordination_id": report_id, "kind": "report", "status": "completed", "reply_to": task_id},
                  "summary": "Simulation factice hors ligne."}
        report_file = state_path.parent / "result.json"
        validation_file = state_path.parent / "validation.json"
        save_state(report_file, report)
        save_state(validation_file, {"schema_valid": True, "ids_and_sha_valid": True,
                                    "input_files_unchanged": True,
                                    "execution": {"launches": 1, "exit_code": 0, "error": None}})
        outbox = state_path.parent / "outbox"
        manifest = formatter.prepare(report_file, validation_file, outbox, report_id, task_id, "Pince2Homard")
        body = (outbox / "comment.md").read_text(encoding="utf-8")
        url = f"https://github.com/{DEFAULT_REPOSITORY}/issues/7#issuecomment-1004"
        remote = {"id": 1004, "html_url": url, "body": body, "user": {"login": "Pince2Homard"},
                  "issue_url": f"https://api.github.com/repos/{DEFAULT_REPOSITORY}/issues/7"}
        api.comments.append(remote)
        save_state(outbox / "receipt.json", formatter.receipt(remote, manifest, body))
        # Même remise durable qu'après une publication, mais sans aucun POST.
        publication_tracking.stage_confirmed(outbox / "receipt.json",
            formatter.receipt(remote, manifest, body), manifest["approval_digest"],
            DEFAULT_REPOSITORY, state_path.parent / "publication-receipts")
        watcher.run_once(notify=False)
        publication = publication_tracking.view(load_state(state_path), report_id, time.time())
        board = watcher.write_dashboard()
        print(json.dumps({"résultat": result, "notifications_simulées": notifications,
                          "coordination": {"registre": len(state["coordination_registry"]),
                                           "tableau_local": board.exists(),
                                           "packet_files": sorted(p.name for p in packet.iterdir()),
                                           "assistant_started": False, "publication_simulée": publication["status"]}}))
    return 0


def _configure_logging(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        data_dir / "watcher.log", maxBytes=512_000, backupCount=2, encoding="utf-8",
    )
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(
        level=logging.INFO, handlers=[handler],
        format="%(asctime)sZ %(levelname)s %(message)s",
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Surveillance GitHub locale, GET seulement.")
    parser.add_argument("--config", type=Path, default=default_data_dir() / "config.json")
    parser.add_argument("--init", action="store_true", help="crée une configuration locale d'exemple")
    parser.add_argument("--once", action="store_true", help="effectue un seul relevé GitHub")
    parser.add_argument("--simulate", action="store_true", help="test hors ligne sans écrire d'état réel")
    parser.add_argument("--import-receipt", type=Path, help="rapprochement GET d'un reçu existant")
    parser.add_argument("--receipt-uuid")
    parser.add_argument("--receipt-url")
    parser.add_argument("--receipt-digest")
    parser.add_argument("--refresh", action="store_true", help="relevé, paquets de lecture seule admissibles et tableau local")
    parser.add_argument("--dashboard", action="store_true", help="tableau local sans réseau")
    parser.add_argument("--prepare-transfer", metavar="COORDINATION_UUID", help="prépare un dossier, sans lancer Codex")
    parser.add_argument("--list", action="store_true", help="liste locale et liens GitHub")
    parser.add_argument("--json", action="store_true", help="export structuré de la liste")
    parser.add_argument("--ack", metavar="EVENT_ID", help="accusé local, jamais une autorisation GitHub")
    parser.add_argument("--status", choices=["acknowledged", "done", "ignored"], default="acknowledged")
    parser.add_argument("--history", action="store_true", help="affiche l'historique local récent")
    args = parser.parse_args(argv)
    try:
        if args.simulate:
            return run_simulation()
        if args.init:
            if args.config.exists():
                raise WatcherError("La configuration existe déjà; aucun écrasement effectué.")
            args.config.parent.mkdir(parents=True, exist_ok=True)
            save_state(args.config, default_config())
            print(f"Configuration locale créée : {args.config}")
            return 0
        config = load_config(args.config)
        data_dir = args.config.parent
        _configure_logging(data_dir)
        state_path = data_dir / "state.json"
        watcher = Watcher(config, state_path)
        if args.import_receipt:
            if not all((args.receipt_uuid, args.receipt_url, args.receipt_digest)):
                raise WatcherError("UUID, URL et digest attendus sont obligatoires pour l'import.")
            result = watcher.import_receipt(args.import_receipt, args.receipt_uuid, args.receipt_url, args.receipt_digest)
            print(json.dumps(result, ensure_ascii=True))
            print(watcher.write_dashboard())
            return 0 if result["status"] == "confirmed" else 2
        if args.prepare_transfer:
            print(json.dumps(watcher.run_once(notify=False, transfer_id=args.prepare_transfer)))
            print("Données préparées ; lancement d'assistant et opérations sensibles non autorisés.")
            return 0
        if args.refresh:
            result = watcher.run_once(prepare_ready=True)
            logging.info("refresh terminé threads=%d nouveaux=%d", result["threads_checked"], result["new_events"])
            print(json.dumps(result))
            print(watcher.write_dashboard())
            return 0
        if args.dashboard:
            print(watcher.write_dashboard())
            return 0
        if args.ack:
            watcher.acknowledge(args.ack, args.status)
            print("Traitement local enregistré ; aucune action GitHub autorisée ou exécutée.")
            return 0
        if args.list:
            events = watcher.events()
            if args.json:
                print(json.dumps(events, ensure_ascii=True, indent=2))
            else:
                for event in events:
                    print(event["event_id"], event["status"], event.get("prefix", ""),
                          "auteur connu" if event.get("authorized_author") else "non vérifié")
                    print("  " + event["url"])
                print(f"{len(events)} événement(s) ; accusé local distinct d'une validation humaine.")
            return 0
        if args.history:
            for event in watcher.history():
                print(json.dumps(event, ensure_ascii=False, sort_keys=True))
            return 0
        if not args.once:
            parser.error("précisez --once, --history, --simulate ou --init")
        result = watcher.run_once()
        logging.info(
            "poll terminé baseline=%s threads=%d nouveaux=%d notifications=%d en_attente=%d",
            result["baseline"], result["threads_checked"], result["new_events"],
            result["notifications_delivered"], result["pending_events"],
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (WatcherError, OSError) as exc:
        logging.error("surveillance différée: %s", exc)
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
