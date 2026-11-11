"""Read-only GitHub comment watcher. It never executes comment content."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import logging
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
    state.setdefault("initialized", False)
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

    def __init__(self, timeout: int = REQUEST_TIMEOUT_SECONDS):
        self.timeout = timeout

    def get_json(self, path: str):
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
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise WatcherError(f"GitHub API HTTP {exc.code}; état non avancé.") from None
        except (URLError, TimeoutError, OSError, json.JSONDecodeError):
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
    return {
        "event_key": (
            f"{thread['key']}:{source}:{comment_id}:"
            f"{comment.get('updated_at') or comment.get('created_at') or ''}"
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

    def _fetch_threads(self) -> list:
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
        valid_pulls = [pull for pull in pulls if isinstance(pull, dict)]
        if len(valid_pulls) > MAX_TRACKED_PRS:
            raise WatcherError("Plus de 100 PR ouvertes; relever l'intervalle avant de continuer.")
        for pull in valid_pulls:
            number = pull.get("number")
            if isinstance(number, int) and not isinstance(number, bool) and number > 0:
                threads.append({
                    "key": f"pr:{number}", "type": "pr", "number": number,
                    "updated_at": pull.get("updated_at"),
                })
        return threads

    def _scan_thread(self, state: dict, thread: dict, is_baseline: bool) -> list:
        repository = self.config["repository"]
        prior = state["threads"].get(thread["key"], _new_thread_state(None))
        prior_updated = _timestamp(prior.get("updated_at"))
        snapshot_updated = _timestamp(thread.get("updated_at"))
        cursors = prior.setdefault("cursors", {})
        found = []
        for source, path in _sources_for_thread(repository, thread).items():
            entries = self.client.get_pages(path)
            entries = [
                entry for entry in entries
                if isinstance(entry, dict) and isinstance(entry.get("id"), int)
                and not isinstance(entry.get("id"), bool) and entry["id"] > 0
            ]
            entries.sort(key=lambda entry: entry["id"])
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
                updated = _timestamp(comment.get("updated_at") or comment.get("created_at"))
                created = _timestamp(comment.get("created_at"))
                version_key = f"{thread['key']}:{source}:{comment_id}:{comment.get('updated_at') or comment.get('created_at')}"

                if is_baseline:
                    # Quietly establish a baseline. A comment later than the
                    # metadata snapshot is retained as a candidate for next poll.
                    if updated is not None and snapshot_updated is not None and updated <= snapshot_updated:
                        if event:
                            _record_version(state, version_key)
                        continue
                    if created is not None and snapshot_updated is not None and created > snapshot_updated:
                        if event and version_key not in state["seen_event_versions"]:
                            found.append(event)
                            _record_version(state, version_key)
                    continue

                is_new = comment_id > high_water
                is_edit = (
                    prior_updated is not None and updated is not None
                    and updated > prior_updated
                )
                if not (is_new or is_edit):
                    continue
                if event and version_key not in state["seen_event_versions"]:
                    found.append(event)
                _record_version(state, version_key)
            cursors[source] = newest
        state["threads"][thread["key"]] = {
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

    def run_once(self) -> dict:
        lock_path = self.state_path.with_name("watcher.lock")
        with single_instance_lock(lock_path):
            return self._run_once_locked()

    def _run_once_locked(self) -> dict:
        state = load_state(self.state_path)
        pending_delivered = self._deliver_pending(state)
        threads = self._fetch_threads()
        baseline = not state.get("initialized", False)
        new_events = []
        for thread in threads:
            previous = state["threads"].get(thread["key"])
            is_new_thread = previous is None
            changed = (
                is_new_thread or thread.get("updated_at") != previous.get("updated_at")
            )
            if not changed:
                continue
            new_events.extend(self._scan_thread(
                state, thread, is_baseline=baseline,
            ))
        existing_keys = {event.get("event_key") for event in state["pending_notifications"]}
        new_events = [event for event in new_events if event["event_key"] not in existing_keys]
        if len(state["pending_notifications"]) + len(new_events) > MAX_PENDING_EVENTS:
            raise WatcherError("Trop d'événements en attente; état et curseurs non sauvegardés.")
        state["pending_notifications"].extend(new_events)
        state["initialized"] = True
        save_state(self.state_path, state)
        new_count = self._deliver_pending(state)
        return {
            "baseline": baseline,
            "threads_checked": len(threads),
            "new_events": len(new_events),
            "notifications_delivered": new_count + pending_delivered,
            "pending_events": len(state["pending_notifications"]),
        }

    def history(self) -> list:
        return load_state(self.state_path).get("history", [])


def show_windows_notification(event_count: int) -> bool:
    if os.name != "nt" or event_count < 1:
        return False
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command", _POWERSHELL_BALLOON],
            input=f"{int(event_count)}\n", text=True, capture_output=True,
            timeout=REQUEST_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


class _SimulationClient:
    def __init__(self):
        self.issue_updated = "2026-11-11T12:00:00Z"
        self.comments = []

    def get_json(self, path):
        if path.endswith("/issues/7"):
            return {"updated_at": self.issue_updated}
        raise AssertionError("endpoint inattendu en simulation")

    def get_pages(self, path):
        if "/pulls?state=open" in path:
            return []
        if path.endswith("/issues/7/comments"):
            return list(self.comments)
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
        print(json.dumps({"résultat": result, "notifications_simulées": notifications}))
    return 0


def _configure_logging(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        data_dir / "watcher.log", maxBytes=512_000, backupCount=2, encoding="utf-8",
    )
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
    except WatcherError as exc:
        logging.error("surveillance différée: %s", exc)
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
