"""Collecte de flux et diffusion Telegram, avec publication verrouillée par défaut."""
import html
import hashlib
import calendar
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit

import feedparser
import requests
import cadence_simulator

DEFAULT_TELEGRAM_INTERVAL_SECONDS = 30
MAX_INLINE_CADENCE_WAIT_SECONDS = 120
TELEGRAM_REQUEST_TIMEOUT_SECONDS = 30
TELEGRAM_MAX_ATTEMPTS = 3
TELEGRAM_MAX_RETRY_AFTER_SECONDS = 60
MAX_TELEGRAM_SEND_BUDGET_SECONDS = (
    TELEGRAM_MAX_ATTEMPTS * TELEGRAM_REQUEST_TIMEOUT_SECONDS
    + (TELEGRAM_MAX_ATTEMPTS - 1) * TELEGRAM_MAX_RETRY_AFTER_SECONDS
)
MAX_PAR_RUN = 50
MAX_ERREURS = 500
MAX_FILE = 1000
COLLECT_BUDGET = 180
RUN_BUDGET = 12 * 60
CHECKPOINT_EVERY = 5
MAX_METRICS_HISTORY = 12
DEFAULT_ARTICLE_MAX_AGE_HOURS = 24
FUTURE_DATE_TOLERANCE_SECONDS = 5 * 60
MAX_FUTURE_DATE_DAYS = 7
UNKNOWN_DATE_QUARANTINE_SECONDS = 24 * 3600
MAX_DATE_QUARANTINE = 500
MAX_ADMISSION_BUFFER = 1000
MAX_URL_SEEN = 5000
DEFAULT_ESTIMATED_SEND_SECONDS = 0.5
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
GDATA = re.compile(r"https?://gdata\.youtube\.com/feeds/base/users/([^/]+)/uploads")
MOTIFS = [
    r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
    r'"externalId":"(UC[\w-]{22})"',
    r'itemprop="channelId" content="(UC[\w-]{22})"',
]


def load_feeds(path="feeds.txt"):
    with open(path, encoding="utf-8") as source:
        return [
            line.strip() for line in source
            if line.strip() and not line.lstrip().startswith("#")
        ]


class PermanentTelegramError(Exception):
    """Erreur Telegram propre à un article : l’article ne pourra pas être envoyé tel quel."""

    def __init__(self, message, http_status=None):
        super().__init__(message)
        self.http_status = http_status


class GlobalTelegramError(Exception):
    """Erreur de configuration ou d’accès Telegram : toute la file doit être préservée."""

    def __init__(self, message, http_status=None):
        super().__init__(message)
        self.http_status = http_status


@dataclass(frozen=True)
class TelegramSendResult:
    """Résultat confirmé d'un appel Telegram, sans conserver de donnée secrète."""

    success: bool
    message_id: int | str | None = None
    telegram_date: float | None = None
    attempts: int = 1
    attempt_errors: int = 0
    duration_seconds: float = 0.0
    http_errors: int = 0

    def __bool__(self):
        return self.success


def get_telegram_interval_seconds(value=None):
    """Lit une cadence positive configurable par variable d'environnement."""
    raw = os.getenv("TELEGRAM_INTERVAL_SECONDS", str(DEFAULT_TELEGRAM_INTERVAL_SECONDS)) \
        if value is None else value
    try:
        interval = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("TELEGRAM_INTERVAL_SECONDS doit être un entier positif") from exc
    if interval <= 0:
        raise ValueError("TELEGRAM_INTERVAL_SECONDS doit être un entier positif")
    return interval


def get_article_max_age_seconds(value=None):
    """Lit la fenêtre de fraîcheur RSS configurable, en heures entières positives."""
    raw = os.getenv(
        "ARTICLE_MAX_AGE_HOURS", str(DEFAULT_ARTICLE_MAX_AGE_HOURS),
    ) if value is None else value
    try:
        hours = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("ARTICLE_MAX_AGE_HOURS doit être un entier positif") from exc
    if hours <= 0:
        raise ValueError("ARTICLE_MAX_AGE_HOURS doit être un entier positif")
    return hours * 3600


def _utc_iso(timestamp=None):
    moment = datetime.now(timezone.utc) if timestamp is None else datetime.fromtimestamp(
        float(timestamp), timezone.utc,
    )
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _http_status_from_exception(exc):
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _http_status_category(status):
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 410:
        return "gone"
    if status == 429:
        return "rate_limited"
    if status is not None and status >= 500:
        return "upstream_error"
    if status is not None and status >= 400:
        return "client_error"
    return "unknown"


def _retry_after_seconds(exc):
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    value = headers.get("Retry-After")
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def load_state(path="state.json"): 
    try:
        with open(path, encoding="utf-8") as source:
            state = json.load(source)
    except FileNotFoundError:
        state = {"vus": [], "file": [], "init": False}

    if not isinstance(state, dict):
        raise ValueError("state.json doit contenir un objet JSON")
    state.setdefault("vus", [])
    state.setdefault("file", [])
    state.setdefault("erreurs", [])
    state.setdefault("feeds", [])
    state.setdefault("yt", {})
    state.setdefault("feed_health", {})
    state.setdefault("feed_offset", 0)
    state.setdefault("last_successful_send_at", None)
    state.setdefault("last_successful_message_id", None)
    state.setdefault("metrics_history", [])
    state.setdefault("admission_buffer", [])
    state.setdefault("date_quarantine", [])
    state.setdefault("url_seen", [])
    if (not isinstance(state["vus"], list) or not isinstance(state["file"], list)
            or not isinstance(state["erreurs"], list)):
        raise ValueError("Format invalide dans state.json : listes 'vus', 'file' et 'erreurs' requises")
    if not isinstance(state["feeds"], list) or not isinstance(state["yt"], dict):
        raise ValueError("Format invalide dans state.json : 'feeds' ou 'yt'")
    if not isinstance(state["feed_health"], dict):
        raise ValueError("Format invalide dans state.json : 'feed_health'")
    if not isinstance(state["metrics_history"], list):
        raise ValueError("Format invalide dans state.json : 'metrics_history'")
    if (not isinstance(state["admission_buffer"], list)
            or not isinstance(state["date_quarantine"], list)
            or not isinstance(state["url_seen"], list)):
        raise ValueError(
            "Format invalide dans state.json : 'admission_buffer', "
            "'date_quarantine' ou 'url_seen'",
        )
    for buffer_name in ("admission_buffer", "date_quarantine"):
        for record in state[buffer_name]:
            if not isinstance(record, dict):
                raise ValueError(f"Format invalide dans state.json : '{buffer_name}'")
            if not isinstance(record.get("item"), dict):
                raise ValueError(
                    f"Format invalide dans state.json : article de '{buffer_name}'",
                )
            uid = record.get("uid")
            if uid is not None and not isinstance(uid, str):
                raise ValueError(
                    f"Format invalide dans state.json : identifiant de '{buffer_name}'",
                )
            fingerprint = record.get("url_hash")
            if fingerprint is not None and (
                not isinstance(fingerprint, str)
                or not re.fullmatch(r"[0-9a-f]{32}", fingerprint)
            ):
                raise ValueError(
                    f"Format invalide dans state.json : empreinte de '{buffer_name}'",
                )
            for timestamp_name in ("first_seen_at", "admitted_at"):
                timestamp = record.get(timestamp_name)
                if timestamp is None and timestamp_name not in record:
                    continue
                if (isinstance(timestamp, bool)
                        or not isinstance(timestamp, (int, float))
                        or not math.isfinite(timestamp) or timestamp < 0):
                    raise ValueError(
                        f"Format invalide dans state.json : {timestamp_name} de '{buffer_name}'",
                    )
            published_at = record.get("published_at")
            if published_at is not None and (
                isinstance(published_at, bool)
                or not isinstance(published_at, (int, float))
                or not math.isfinite(published_at)
            ):
                raise ValueError(
                    f"Format invalide dans state.json : date de '{buffer_name}'",
                )
    if any(not isinstance(value, str) for value in state["url_seen"]):
        raise ValueError("Format invalide dans state.json : 'url_seen'")
    state["url_seen"] = list(dict.fromkeys(state["url_seen"]))[-MAX_URL_SEEN:]
    if not isinstance(state["feed_offset"], int):
        raise ValueError("Format invalide dans state.json : 'feed_offset'")
    last_send = state["last_successful_send_at"]
    if (last_send is not None and
            (isinstance(last_send, bool) or not isinstance(last_send, (int, float))
             or not math.isfinite(last_send) or last_send < 0)):
        raise ValueError("Format invalide dans state.json : 'last_successful_send_at'")
    message_id = state["last_successful_message_id"]
    if (message_id is not None and
            (isinstance(message_id, bool) or not isinstance(message_id, (int, str)))):
        raise ValueError("Format invalide dans state.json : 'last_successful_message_id'")

    # Migration non destructive : ne jamais effacer la file d'attente existante.
    if state.get("v") != 2:
        state["v"] = 2
    return state


def save_state(state, seen, path="state.json"):
    """Écriture atomique : un arrêt ne laisse pas un JSON partiellement écrit."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    state["vus"] = list(seen)[-5000:]
    state["erreurs"] = state.get("erreurs", [])[-MAX_ERREURS:]
    state["url_seen"] = list(dict.fromkeys(state.get("url_seen", [])))[-MAX_URL_SEEN:]
    state.setdefault("admission_buffer", [])
    state.setdefault("date_quarantine", [])
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(target.parent),
            prefix=f".{target.name}.", suffix=".tmp", delete=False,
        ) as temp:
            temp_path = temp.name
            json.dump(state, temp, ensure_ascii=False, separators=(",", ":"))
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_path, target)
        # Synchroniser le répertoire améliore la durabilité du remplacement atomique.
        try:
            directory_fd = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def is_http_url(value):
    try:
        parsed = urlparse(value)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except (TypeError, ValueError):
        return False


def format_item(entry):
    title = html.escape(str(entry.get("title") or "(sans titre)"))
    link = str(entry.get("link") or "").strip()
    if not is_http_url(link):
        return None
    return {
        "text": f"<b>{title}</b>\n{html.escape(link)}",
        "link": link,
        "pub": str(entry.get("published") or ""),
    }


def parse_feed(url, http=requests, parser=feedparser.parse, timeout=(5, 20)):
    """Télécharge un flux avec délais de connexion et de lecture bornés."""
    response = http.get(url, headers={"User-Agent": UA}, timeout=timeout)
    response.raise_for_status()
    return parser(response.content)


def entries_for_feed(url, state, http=requests, parser=feedparser.parse):
    match = GDATA.match(url)
    if not match:
        return parse_feed(url, http=http, parser=parser).entries

    username = match.group(1)
    data = parse_feed(
        "https://www.youtube.com/feeds/videos.xml?user=" + username,
        http=http, parser=parser,
    )
    if data.entries:
        return data.entries

    if username not in state["yt"]:
        try:
            response = http.get(
                "https://www.youtube.com/user/" + username,
                headers={"User-Agent": UA, "Accept-Language": "fr"},
                cookies={"CONSENT": "YES+1"}, timeout=30,
            )
            response.raise_for_status()
            for pattern in MOTIFS:
                found = re.search(pattern, response.text)
                if found:
                    state["yt"][username] = found.group(1)
                    break
        except requests.RequestException as exc:
            status = _http_status_from_exception(exc)
            print(
                "Erreur résolution YouTube |", type(exc).__name__,
                "| HTTP:", status if status is not None else "n/a",
                "| catégorie:", _http_status_category(status),
            )

    if username in state["yt"]:
        data = parse_feed(
            "https://www.youtube.com/feeds/videos.xml?channel_id=" + state["yt"][username],
            http=http, parser=parser,
        )
        return data.entries
    print("Chaîne YouTube non résolue")
    return []


def _feed_label(url):
    """Label stable de journal sans paramètres ni identifiants d’URL."""
    parsed = urlparse(url)
    host = parsed.hostname or "flux-inconnu"
    fingerprint = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return f"{host}#{fingerprint}"


def _canonical_url_fingerprint(value):
    """Hash URL sans fragment; conserve chemin et requête pour éviter les collisions sémantiques."""
    try:
        parsed = urlsplit(str(value or "").strip())
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return None
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port
        if port and not (
            parsed.scheme.lower() == "http" and port == 80
            or parsed.scheme.lower() == "https" and port == 443
        ):
            host = f"{host}:{port}"
        canonical = urlunsplit((
            parsed.scheme.lower(), host, parsed.path or "/", parsed.query, "",
        ))
    except (TypeError, ValueError, UnicodeError):
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _publication_timestamp(entry):
    """Retourne la date de publication fiable; une date de mise à jour est ignorée."""
    raw = entry.get("published")
    if raw is not None:
        raw = str(raw).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{8}", raw):
            return None

    parsed = entry.get("published_parsed")
    if parsed is not None:
        try:
            if isinstance(parsed, datetime):
                moment = parsed
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                return moment.timestamp()
            parts = tuple(parsed)
            if len(parts) >= 6:
                return float(calendar.timegm(parts))
        except (OverflowError, TypeError, ValueError):
            pass

    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            moment = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        timestamp = moment.timestamp()
    except (OverflowError, OSError, ValueError):
        return None
    return timestamp if math.isfinite(timestamp) else None


def _classify_publication(entry, now_epoch, max_age_seconds):
    published_at = _publication_timestamp(entry)
    if published_at is None:
        return "unknown", None, None
    age = float(now_epoch) - published_at
    if age < -FUTURE_DATE_TOLERANCE_SECONDS:
        return "future", published_at, age
    if age > max_age_seconds:
        return "stale", published_at, age
    return "fresh", published_at, age


def _age_bucket(age_seconds):
    if age_seconds is None:
        return "unknown"
    if age_seconds < 0:
        return "future"
    if age_seconds < 3600:
        return "under_1h"
    if age_seconds < 6 * 3600:
        return "1_to_6h"
    if age_seconds <= 24 * 3600:
        return "6_to_24h"
    return "over_24h"


def _remember_url(state, fingerprint):
    if fingerprint:
        values = state.setdefault("url_seen", [])
        if fingerprint not in values:
            values.append(fingerprint)
            if len(values) > MAX_URL_SEEN:
                del values[:-MAX_URL_SEEN]


def _entry_uid(entry):
    value = entry.get("id") or entry.get("link")
    return str(value) if value else None


def _quarantine_match(state, uid, fingerprint):
    for index, record in enumerate(state.get("date_quarantine", [])):
        if uid and record.get("uid") == uid:
            return index
        if fingerprint and record.get("url_hash") == fingerprint:
            return index
    return None


def _quarantine_deadline(record, max_age_seconds):
    if record.get("reason") == "unknown_date":
        return float(record.get("first_seen_at", 0)) + UNKNOWN_DATE_QUARANTINE_SECONDS
    if record.get("reason") == "future_date":
        published_at = record.get("published_at")
        first_seen = float(record.get("first_seen_at", 0))
        if published_at is not None and published_at - first_seen <= (
            MAX_FUTURE_DATE_DAYS * 86400
        ):
            return float(published_at) + max_age_seconds
        return first_seen + UNKNOWN_DATE_QUARANTINE_SECONDS
    return None


def _make_quarantine_record(uid, fingerprint, item, now_epoch, reason, published_at):
    return {
        "uid": uid,
        "url_hash": fingerprint,
        "item": item,
        "first_seen_at": float(now_epoch),
        "reason": reason,
        "published_at": published_at,
    }


def _admit_item(state, uid, fingerprint, item, now_epoch,
                first_seen_at=None, admitted_at=None):
    """Persistently accepts a fresh item without expiring it while it waits."""
    state.setdefault("file", [])
    buffer = state.setdefault("admission_buffer", [])
    first_seen_at = now_epoch if first_seen_at is None else first_seen_at
    admitted_at = now_epoch if admitted_at is None else admitted_at
    if len(state["file"]) < MAX_FILE and not buffer:
        queued = dict(item)
        queued.setdefault("queued_at", float(now_epoch))
        queued.setdefault("first_seen_at", float(first_seen_at))
        queued.setdefault("admitted_at", float(admitted_at))
        state["file"].append(queued)
        target = "file"
    elif len(buffer) < MAX_ADMISSION_BUFFER:
        buffer.append({
            "uid": uid,
            "url_hash": fingerprint,
            "item": dict(item),
            "first_seen_at": float(first_seen_at),
            "admitted_at": float(admitted_at),
        })
        target = "admission_buffer"
    else:
        return False
    _remember_url(state, fingerprint)
    return target


def _drain_admission_buffer(state, now_epoch):
    """Promotes the oldest admitted records after all already queued items."""
    state.setdefault("file", [])
    buffer = state.setdefault("admission_buffer", [])
    promoted = 0
    while buffer and len(state["file"]) < MAX_FILE:
        record = buffer.pop(0)
        item = dict(record.get("item") or {})
        item.setdefault("queued_at", float(now_epoch))
        item.setdefault("first_seen_at", float(record.get("first_seen_at", now_epoch)))
        item.setdefault("admitted_at", float(record.get("admitted_at", now_epoch)))
        state["file"].append(item)
        promoted += 1
    return promoted


def _refresh_quarantine_record(record, entry, item, uid, fingerprint, now_epoch,
                               max_age_seconds):
    """Updates quarantined source metadata and returns fresh/stale/held status."""
    status, published_at, age = _classify_publication(
        entry, now_epoch, max_age_seconds,
    )
    record["uid"] = uid or record.get("uid")
    record["url_hash"] = fingerprint or record.get("url_hash")
    record["item"] = item
    record["last_checked_at"] = float(now_epoch)
    if status == "unknown":
        record["reason"] = "unknown_date"
        return "held", None, None
    if status == "future":
        record["reason"] = "future_date"
        record["published_at"] = published_at
        return "held", published_at, age
    if status == "stale":
        return "stale", published_at, age
    record["published_at"] = published_at
    # The freshness check occurs now. If storage is full, retain the candidate
    # durably in quarantine without reapplying the 24-hour rule on a later run.
    record["reason"] = "admission_deferred"
    record["admitted_at"] = float(now_epoch)
    return "fresh", published_at, age


def _oldest_pending_age_seconds(queue, now_epoch=None, admission_buffer=None):
    """Âge depuis queued_at/admitted_at, jamais depuis la date RSS."""
    timestamps = []
    if queue and isinstance(queue[0], dict):
        timestamps.append(queue[0].get("queued_at"))
    if admission_buffer and isinstance(admission_buffer[0], dict):
        timestamps.append(admission_buffer[0].get("admitted_at"))
    timestamps = [
        value for value in timestamps
        if not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value) and value >= 0
    ]
    if not timestamps:
        return None
    now = time.time() if now_epoch is None else float(now_epoch)
    return round(max(0.0, now - min(timestamps)), 3)


def _store_run_metrics(state, metrics):
    """Garde un petit historique borné, sans article, URL ni donnée secrète."""
    history = state.setdefault("metrics_history", [])
    state["metrics_history"] = (history + [{
        "run_started_epoch": round(metrics["run_started_epoch"], 3),
        "feeds_examined": metrics.get("feeds_examined", 0),
        "entries_examined": metrics.get("entries_examined", 0),
        "articles_discovered": metrics.get("articles_discovered", 0),
        "articles_admissible": metrics.get("articles_admissible", 0),
        "articles_added": metrics.get("articles_added", 0),
        "articles_admitted": metrics.get("articles_admitted", 0),
        "articles_promoted_from_admission_buffer": metrics.get(
            "articles_promoted_from_admission_buffer", 0,
        ),
        "articles_added_to_admission_buffer": metrics.get(
            "articles_added_to_admission_buffer", 0,
        ),
        "articles_rejected_as_stale": metrics.get("articles_rejected_as_stale", 0),
        "articles_without_reliable_pubdate": metrics.get(
            "articles_without_reliable_pubdate", 0,
        ),
        "articles_with_future_pubdate": metrics.get("articles_with_future_pubdate", 0),
        "articles_quarantined": metrics.get("articles_quarantined", 0),
        "quarantine_rechecks": metrics.get("quarantine_rechecks", 0),
        "quarantine_released": metrics.get("quarantine_released", 0),
        "quarantine_expired": metrics.get("quarantine_expired", 0),
        "age_distribution": metrics.get("age_distribution", {}),
        "articles_rejected_by_freshness": metrics.get("articles_rejected_by_freshness", 0),
        "articles_skipped_by_seen_id": metrics.get("articles_skipped_by_seen_id", 0),
        "articles_skipped_by_feed_cursor": metrics.get("articles_skipped_by_feed_cursor", 0),
        "articles_with_pubdate": metrics.get("articles_with_pubdate", 0),
        "articles_without_pubdate": metrics.get("articles_without_pubdate", 0),
        "freshness_policy_enabled": metrics.get("freshness_policy_enabled", False),
        "article_max_age_hours": metrics.get("article_max_age_hours", 24),
        "published": metrics.get("published", 0),
        "rss_deferred": metrics.get("rss_deferred", 0),
        "queue_initial": metrics.get("queue_initial", 0),
        "queue_final": metrics.get("queue_final", 0),
        "admission_buffer_initial": metrics.get("admission_buffer_initial", 0),
        "admission_buffer_final": metrics.get("admission_buffer_final", 0),
        "date_quarantine_initial": metrics.get("date_quarantine_initial", 0),
        "date_quarantine_final": metrics.get("date_quarantine_final", 0),
        "pending_total_final": metrics.get("pending_total_final", 0),
        "oldest_pending_age_seconds": metrics.get("oldest_pending_age_seconds"),
        "collection_seconds": metrics.get("collection_seconds", 0),
        "publication_seconds": metrics.get("publication_seconds", 0),
        "total_seconds": metrics.get("total_seconds", 0),
        "intervals_seconds": metrics.get("intervals_seconds", []),
        "telegram_attempts": metrics.get("telegram_attempts", 0),
        "telegram_request_duration_seconds": metrics.get("telegram_request_duration_seconds", 0),
        "telegram_send_operations": metrics.get("telegram_send_operations", 0),
        "telegram_call_duration_seconds": metrics.get("telegram_call_duration_seconds", 0),
        "simulated_interval_seconds": metrics.get("simulation", {})
            .get("recommendation", {}).get("interval_seconds", 30),
        "simulated_candidate_interval_seconds": metrics.get("simulation", {})
            .get("recommendation", {}).get("candidate_interval_seconds"),
        "simulated_candidate_observations": metrics.get("simulation", {})
            .get("recommendation", {}).get("candidate_observations", 0),
    }])[-MAX_METRICS_HISTORY:]


def _log_phase51_metrics(metrics):
    """Résumé JSON compact, sans URL de flux, token, chat ni identifiant de message."""
    print(
        "PHASE51_METRICS |",
        json.dumps(metrics, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
    )


def collect_articles(
    feeds, state, http=requests, parser=feedparser.parse,
    budget=COLLECT_BUDGET, clock=time.monotonic, checkpoint=None,
    checkpoint_every=CHECKPOINT_EVERY, metrics=None, queue_clock=time.time,
    max_age_seconds=None,
):
    state.setdefault("feed_health", {})
    state.setdefault("feeds", [])
    state.setdefault("file", [])
    state.setdefault("vus", [])
    state.setdefault("yt", {})
    state.setdefault("admission_buffer", [])
    state.setdefault("date_quarantine", [])
    state.setdefault("url_seen", [])
    max_age_seconds = (
        get_article_max_age_seconds() if max_age_seconds is None
        else int(max_age_seconds)
    )
    if max_age_seconds <= 0:
        raise ValueError("La fenêtre de fraîcheur doit être positive")
    now_epoch = float(queue_clock())
    queue_initial = len(state["file"])
    admission_initial = len(state["admission_buffer"])
    quarantine_initial = len(state["date_quarantine"])
    queue_max = queue_initial
    seen = dict.fromkeys(state["vus"])
    # Un GUID peut ressembler à une URL sans être le lien de l'article. Ne pas
    # fabriquer de correspondance URL à partir de l'ancien historique d'IDs.
    url_seen = set(state["url_seen"])
    active_url_seen = set()
    for item in state["file"]:
        if isinstance(item, dict):
            fingerprint = _canonical_url_fingerprint(item.get("link"))
            _remember_url(state, fingerprint)
            if fingerprint:
                active_url_seen.add(fingerprint)
    for record in state["admission_buffer"] + state["date_quarantine"]:
        if record.get("uid"):
            seen[str(record["uid"])] = None
        fingerprint = record.get("url_hash")
        if not fingerprint and isinstance(record.get("item"), dict):
            fingerprint = _canonical_url_fingerprint(record["item"].get("link"))
            record["url_hash"] = fingerprint
        _remember_url(state, fingerprint)
        if fingerprint:
            active_url_seen.add(fingerprint)

    # Les états historiques sans init mais avec de l’activité sont déjà en service.
    was_initialized = (
        bool(state["init"]) if "init" in state
        else bool(state.get("feeds") or state.get("vus") or state.get("file"))
    )
    # Rotation persistante : chaque exécution commence au flux suivant.
    # Elle évite que les derniers flux soient toujours pénalisés si la file sature.
    if feeds:
        offset = int(state.get("feed_offset", 0)) % len(feeds)
        ordered_feeds = feeds[offset:] + feeds[:offset]
    else:
        offset = 0
        ordered_feeds = []
        state["feed_offset"] = 0
    started = clock()
    counts = {
        "ok": 0, "empty": 0, "error": 0, "http_error": 0,
        "deferred_feeds": 0, "entries_examined": 0,
        "discovered": 0, "admissible": 0, "added": 0,
        "admitted": 0, "buffered": 0, "deferred": 0,
        "buffer_promoted": 0,
        "freshness_rejected": 0, "unknown_date": 0, "future_date": 0,
        "quarantined": 0, "quarantine_rechecked": 0,
        "quarantine_expired": 0, "quarantine_released": 0,
        "invalid_link": 0,
        "duplicate_id": 0, "cursor_skipped": 0,
        "pubdate_known": 0, "pubdate_unknown": 0,
        "age_distribution": {
            "under_1h": 0, "1_to_6h": 0, "6_to_24h": 0,
            "over_24h": 0, "future": 0, "unknown": 0,
        },
        "attempted": 0,
    }
    promoted = _drain_admission_buffer(state, now_epoch)
    queue_max = max(queue_max, len(state["file"]))
    if promoted:
        counts["buffer_promoted"] += promoted
        counts["added"] += promoted

    # Libérer avant le parcours RSS les dates inconnues arrivées à échéance,
    # afin qu'elles ne bloquent pas inutilement une nouvelle entrée.
    quarantine = state["date_quarantine"]
    expiration_now = float(queue_clock())
    for record in list(quarantine):
        if (record.get("reason") == "unknown_date"
                and expiration_now >= _quarantine_deadline(record, max_age_seconds)):
            quarantine.remove(record)
            if record.get("uid"):
                seen[str(record["uid"])] = None
            _remember_url(state, record.get("url_hash"))
            if record.get("url_hash"):
                url_seen.add(record["url_hash"])
                active_url_seen.add(record["url_hash"])
            counts["freshness_rejected"] += 1
            counts["quarantine_expired"] += 1

    # Les articles déjà admis gardent leur éligibilité et sont réessayés avant
    # de consulter de nouvelles dates RSS.
    for record in list(quarantine):
        reason = record.get("reason")
        if reason == "admission_deferred":
            record_now = float(queue_clock())
            target = _admit_item(
                state, str(record.get("uid") or ""),
                record.get("url_hash"), record.get("item") or {}, record_now,
                first_seen_at=record.get("first_seen_at", now_epoch),
                admitted_at=record.get("admitted_at", now_epoch),
            )
            if target:
                seen[str(record.get("uid") or "")] = None
                quarantine.remove(record)
                counts["quarantine_released"] += 1
                counts["admitted"] += 1
                if record.get("url_hash"):
                    url_seen.add(record["url_hash"])
                    active_url_seen.add(record["url_hash"])
                if target == "file":
                    counts["added"] += 1
                else:
                    counts["buffered"] += 1
                queue_max = max(queue_max, len(state["file"]))

    for index, url in enumerate(ordered_feeds):
        if clock() - started >= budget:
            break
        feed_started = clock()
        counts["attempted"] += 1
        feed_deferred = 0
        feed_added = 0
        try:
            entries = entries_for_feed(url, state, http=http, parser=parser)
        except Exception as exc:
            counts["error"] += 1
            health = state["feed_health"].setdefault(url, {"consecutive_errors": 0})
            health["consecutive_errors"] = int(health.get("consecutive_errors", 0)) + 1
            health["last_error"] = type(exc).__name__
            error_duration = round(clock() - feed_started, 3)
            status = _http_status_from_exception(exc)
            if status is not None:
                counts["http_error"] += 1
            retry_after = _retry_after_seconds(exc)
            health["last_http_status"] = status
            health["last_error_category"] = _http_status_category(status)
            # Log only at powers of two to limit recurring failure noise.
            streak = health["consecutive_errors"]
            if streak & (streak - 1) == 0:
                print(
                    "RSS erreur |", _feed_label(url), "|", type(exc).__name__,
                    "| HTTP:", status if status is not None else "n/a",
                    "| catégorie:", _http_status_category(status),
                    "| retry_after_s:", retry_after if retry_after is not None else "n/a",
                    "| échecs consécutifs:", streak,
                    "| durée:", error_duration, "s",
                )
            state["feed_offset"] = (offset + index + 1) % len(feeds)
            if checkpoint:
                checkpoint(state, seen)
            continue

        is_new_feed = url not in state["feeds"]
        counts["entries_examined"] += len(entries)
        if entries:
            counts["ok"] += 1
        else:
            counts["empty"] += 1
        health = state["feed_health"].setdefault(url, {"consecutive_errors": 0})
        health["consecutive_errors"] = 0
        health.pop("last_error", None)
        health.pop("last_http_status", None)
        health.pop("last_error_category", None)
        if entries and is_new_feed:
            state["feeds"].append(url)

        # On le premier démarrage, initialiser silencieusement tout l’historique.
        # Une source ajoutée plus tard est amorcée sans archive : ses anciennes
        # entrées sont baselinées, tandis que la plus récente reste publiable.
        silent_bootstrap = is_new_feed and not was_initialized
        ordered_entries = list(reversed(entries))
        cursor_uid = health.get("latest_uid")
        cursor_index = -1
        if cursor_uid is not None:
            cursor_index = next((
                i for i, entry in enumerate(ordered_entries)
                if str(entry.get("id") or entry.get("link") or "") == str(cursor_uid)
            ), -1)
        current_cursor = cursor_uid
        blocked_cursor = False
        for entry_index, entry in enumerate(ordered_entries):
            uid = _entry_uid(entry)
            if not uid:
                continue
            fingerprint = _canonical_url_fingerprint(entry.get("link"))
            quarantine_index = _quarantine_match(state, uid, fingerprint)
            if quarantine_index is not None:
                record = state["date_quarantine"][quarantine_index]
                if record.get("reason") == "admission_deferred":
                    counts["cursor_skipped"] += 1
                    if not blocked_cursor:
                        current_cursor = uid
                    continue
                item = format_item(entry)
                if item is None:
                    counts["invalid_link"] += 1
                    blocked_cursor = True
                    continue
                counts["quarantine_rechecked"] += 1
                record_now = float(queue_clock())
                status, published_at, age = _refresh_quarantine_record(
                    record, entry, item, uid, fingerprint, record_now,
                    max_age_seconds,
                )
                counts["age_distribution"][_age_bucket(age)] += 1
                if status == "stale":
                    state["date_quarantine"].pop(quarantine_index)
                    seen[uid] = None
                    _remember_url(state, fingerprint)
                    url_seen.add(fingerprint) if fingerprint else None
                    counts["freshness_rejected"] += 1
                    counts["quarantine_expired"] += 1
                    if not blocked_cursor:
                        current_cursor = uid
                elif status == "fresh":
                    counts["admissible"] += 1
                    if age is not None and age < 0:
                        counts["future_date"] += 1
                    target = _admit_item(
                        state, uid, fingerprint, item, record_now,
                        first_seen_at=record.get("first_seen_at", now_epoch),
                        admitted_at=record.get("admitted_at", now_epoch),
                    )
                    if target:
                        state["date_quarantine"].pop(quarantine_index)
                        seen[uid] = None
                        counts["quarantine_released"] += 1
                        counts["admitted"] += 1
                        if fingerprint:
                            url_seen.add(fingerprint)
                        active_url_seen.add(fingerprint)
                        if target == "file":
                            counts["added"] += 1
                        else:
                            counts["buffered"] += 1
                        queue_max = max(queue_max, len(state["file"]))
                        if not blocked_cursor:
                            current_cursor = uid
                    else:
                        record["reason"] = "admission_deferred"
                        record["admitted_at"] = record_now
                        counts["deferred"] += 1
                else:
                    if status == "unknown":
                        counts["unknown_date"] += 1
                    else:
                        counts["future_date"] += 1
                    if not blocked_cursor:
                        current_cursor = uid
                continue

            # Les articles déjà traités sont filtrés par ID puis URL canonique,
            # indépendamment de l'ordre courant du flux.
            if uid in seen:
                counts["duplicate_id"] += 1
                _remember_url(state, fingerprint)
                if fingerprint:
                    url_seen.add(fingerprint)
                if not blocked_cursor:
                    current_cursor = uid
                continue
            if fingerprint and (
                    fingerprint in url_seen or fingerprint in active_url_seen):
                counts["duplicate_id"] += 1
                seen[uid] = None
                if not blocked_cursor:
                    current_cursor = uid
                continue

            if cursor_index >= 0 and entry_index == cursor_index:
                counts["cursor_skipped"] += 1
                seen[uid] = None
                _remember_url(state, fingerprint)
                if fingerprint:
                    url_seen.add(fingerprint)
                    active_url_seen.add(fingerprint)
                if not blocked_cursor:
                    current_cursor = uid
                continue

            # Ne marquer comme vu qu'un article dont le lien est publiable.
            item = format_item(entry)
            if item is None:
                counts["invalid_link"] += 1
                print("Article ignoré : lien HTTP(S) absent ou invalide.")
                blocked_cursor = True
                continue

            is_latest_new_feed_item = is_new_feed and entry_index == len(entries) - 1
            if is_new_feed and (silent_bootstrap or not is_latest_new_feed_item):
                seen[uid] = None
                _remember_url(state, fingerprint)
                if fingerprint:
                    url_seen.add(fingerprint)
                if not blocked_cursor:
                    current_cursor = uid
                continue

            counts["discovered"] += 1
            article_now = float(queue_clock())
            status, published_at, age = _classify_publication(
                entry, article_now, max_age_seconds,
            )
            counts["age_distribution"][_age_bucket(age)] += 1
            if age is None:
                counts["pubdate_unknown"] += 1
                counts["unknown_date"] += 1
            else:
                counts["pubdate_known"] += 1
                if age < 0:
                    counts["future_date"] += 1

            if status == "stale":
                seen[uid] = None
                _remember_url(state, fingerprint)
                if fingerprint:
                    url_seen.add(fingerprint)
                counts["freshness_rejected"] += 1
                if not blocked_cursor:
                    current_cursor = uid
                continue

            if status in ("unknown", "future"):
                if len(state["date_quarantine"]) >= MAX_DATE_QUARANTINE:
                    feed_deferred += 1
                    counts["deferred"] += 1
                    blocked_cursor = True
                    continue
                reason = "unknown_date" if status == "unknown" else "future_date"
                state["date_quarantine"].append(_make_quarantine_record(
                    uid, fingerprint, item, article_now, reason, published_at,
                ))
                if fingerprint:
                    active_url_seen.add(fingerprint)
                counts["quarantined"] += 1
                if not blocked_cursor:
                    current_cursor = uid
                continue

            counts["admissible"] += 1
            target = _admit_item(state, uid, fingerprint, item, article_now)
            if not target:
                feed_deferred += 1
                counts["deferred"] += 1
                blocked_cursor = True
                continue
            seen[uid] = None
            counts["admitted"] += 1
            if target == "file":
                counts["added"] += 1
                feed_added += 1
            else:
                counts["buffered"] += 1
            queue_max = max(queue_max, len(state["file"]))
            if fingerprint:
                url_seen.add(fingerprint)
                active_url_seen.add(fingerprint)
            if not blocked_cursor:
                current_cursor = uid

        if current_cursor is not None:
            health["latest_uid"] = current_cursor

        if feed_deferred:
            counts["deferred_feeds"] += 1
        duration = round(clock() - feed_started, 3)
        print("RSS |", _feed_label(url), "|", "vide" if not entries else "ok",
              "|", len(entries), "entrée(s) | +", feed_added,
              "article(s) | reportés:", feed_deferred, "|", duration, "s")
        # Le curseur pointe sur le prochain flux avant que l’état soit écrit :
        # une interruption au flux suivant reprend sans rebalayer le préfixe.
        state["feed_offset"] = (offset + index + 1) % len(feeds)
        if checkpoint and (index + 1) % max(1, checkpoint_every) == 0:
            checkpoint(state, seen)

    # Une date future doit être revalidée contre le flux disponible avant son
    # admission : l'éditeur peut avoir corrigé la date depuis sa découverte.
    # Si le flux est indisponible ou ne contient plus l'entrée, la date fiable
    # mémorisée permet tout de même une reprise dès qu'elle devient admissible.
    for record in list(quarantine):
        if record.get("reason") != "future_date":
            continue
        record_now = float(queue_clock())
        published_at = record.get("published_at")
        if (isinstance(published_at, bool)
                or not isinstance(published_at, (int, float))
                or not math.isfinite(published_at)):
            continue
        age = record_now - float(published_at)
        first_seen = float(record.get("first_seen_at", now_epoch))
        if (
            float(published_at) - first_seen > MAX_FUTURE_DATE_DAYS * 86400
            and record_now - first_seen >= UNKNOWN_DATE_QUARANTINE_SECONDS
        ):
            quarantine.remove(record)
            if record.get("uid"):
                seen[str(record["uid"])] = None
            _remember_url(state, record.get("url_hash"))
            if record.get("url_hash"):
                url_seen.add(record["url_hash"])
                active_url_seen.add(record["url_hash"])
            counts["freshness_rejected"] += 1
            counts["quarantine_expired"] += 1
            continue
        if age < -FUTURE_DATE_TOLERANCE_SECONDS:
            continue
        if age > max_age_seconds:
            quarantine.remove(record)
            if record.get("uid"):
                seen[str(record["uid"])] = None
            _remember_url(state, record.get("url_hash"))
            if record.get("url_hash"):
                url_seen.add(record["url_hash"])
                active_url_seen.add(record["url_hash"])
            counts["freshness_rejected"] += 1
            counts["quarantine_expired"] += 1
            continue
        record["reason"] = "admission_deferred"
        record["admitted_at"] = record_now
        counts["admissible"] += 1
        target = _admit_item(
            state, str(record.get("uid") or ""),
            record.get("url_hash"), record.get("item") or {}, record_now,
            first_seen_at=record.get("first_seen_at", now_epoch),
            admitted_at=record_now,
        )
        if target:
            seen[str(record.get("uid") or "")] = None
            quarantine.remove(record)
            counts["quarantine_released"] += 1
            counts["admitted"] += 1
            if record.get("url_hash"):
                url_seen.add(record["url_hash"])
                active_url_seen.add(record["url_hash"])
            if target == "file":
                counts["added"] += 1
            else:
                counts["buffered"] += 1
            queue_max = max(queue_max, len(state["file"]))
        else:
            counts["deferred"] += 1

    completed = counts["attempted"] == len(ordered_feeds)
    if completed and feeds:
        # Après un cycle complet, décaler le début d’un flux pour l’équité.
        state["feed_offset"] = (offset + 1) % len(feeds)
    elif not feeds:
        state["feed_offset"] = 0
    state["url_seen"] = list(dict.fromkeys(state["url_seen"]))[-MAX_URL_SEEN:]
    state["init"] = True
    recurrent = [
        _feed_label(url) for url, health in state["feed_health"].items()
        if url in feeds and int(health.get("consecutive_errors", 0)) >= 3
    ]
    collection_duration = round(clock() - started, 3)
    print(
        "Collecte RSS | flux:", counts["attempted"], "/", len(ordered_feeds),
        "| ok:", counts["ok"], "| vides:", counts["empty"],
        "| erreurs:", counts["error"], "| erreurs HTTP:", counts["http_error"],
        "| flux avec reports:", counts["deferred_feeds"],
        "| entrées examinées:", counts["entries_examined"],
        "| découverts:", counts["discovered"],
        "| admissibles:", counts["admissible"], "| ajoutés:", counts["added"],
        "| admis:", counts["admitted"], "| tampon_admission:", counts["buffered"],
        "| quarantaine_dates:", len(state["date_quarantine"]),
        "| quarantaine_expirée:", counts["quarantine_expired"],
        "| doublons_URL_ou_ID:", counts["duplicate_id"],
        "| âge_articles:", json.dumps(
            counts["age_distribution"], separators=(",", ":"), sort_keys=True,
        ),
        "| reportés:", counts["deferred"], "| durée:", collection_duration, "s",
        "| défaillances récurrentes:", ", ".join(recurrent) if recurrent else "aucune",
    )
    if checkpoint:
        checkpoint(state, seen)
    if metrics is not None:
        metrics.update({
            "feeds_total": len(ordered_feeds),
            "feeds_examined": counts["attempted"],
            "entries_examined": counts["entries_examined"],
            "articles_discovered": counts["discovered"],
            "articles_admissible": counts["admissible"],
            "articles_added": counts["added"],
            "articles_admitted": counts["admitted"],
            "articles_promoted_from_admission_buffer": counts["buffer_promoted"],
            "articles_added_to_admission_buffer": counts["buffered"],
            "articles_rejected_as_stale": counts["freshness_rejected"],
            "articles_without_reliable_pubdate": counts["unknown_date"],
            "articles_with_future_pubdate": counts["future_date"],
            "articles_quarantined": counts["quarantined"],
            "quarantine_rechecks": counts["quarantine_rechecked"],
            "quarantine_released": counts["quarantine_released"],
            "quarantine_expired": counts["quarantine_expired"],
            "age_distribution": counts["age_distribution"],
            "articles_rejected_by_freshness": counts["freshness_rejected"],
            "articles_skipped_by_seen_id": counts["duplicate_id"],
            "articles_skipped_by_feed_cursor": counts["cursor_skipped"],
            "articles_invalid_link": counts["invalid_link"],
            "articles_with_pubdate": counts["pubdate_known"],
            "articles_without_pubdate": counts["pubdate_unknown"],
            "freshness_policy_enabled": True,
            "rss_deferred": counts["deferred"],
            "rss_errors": counts["error"],
            "rss_http_errors": counts["http_error"],
            "queue_initial": queue_initial,
            "queue_max": queue_max,
            "queue_after_collection": len(state["file"]),
            "admission_buffer_initial": admission_initial,
            "admission_buffer_final": len(state["admission_buffer"]),
            "date_quarantine_initial": quarantine_initial,
            "date_quarantine_final": len(state["date_quarantine"]),
            "pending_total_final": (
                len(state["file"]) + len(state["admission_buffer"])
                + len(state["date_quarantine"])
            ),
            "collection_seconds": collection_duration,
        })
    return seen


def send_message(
    item, token, chat_id, http=requests, sleep=time.sleep,
    monotonic=time.monotonic, clock=time.time,
):
    attempt_errors = 0
    http_errors = 0
    request_duration = 0.0
    for attempt in range(TELEGRAM_MAX_ATTEMPTS):
        attempt_at = clock()
        request_started = monotonic()
        try:
            response = http.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": item["text"], "parse_mode": "HTML"},
                timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            # Résultat ambigu : Telegram a peut-être reçu le message.
            # Ne pas réessayer automatiquement pour éviter un doublon immédiat.
            duration = max(0, monotonic() - request_started)
            request_duration += duration
            attempt_errors += 1
            status = _http_status_from_exception(exc)
            if status is not None:
                http_errors += 1
            print(
                "TELEGRAM tentative | début_utc:", _utc_iso(attempt_at),
                "| tentative:", f"{attempt + 1}/{TELEGRAM_MAX_ATTEMPTS}",
                "| résultat: réseau_ambigu | HTTP:", status if status is not None else "n/a",
                "| durée_s:", round(duration, 3), "| erreur:", type(exc).__name__,
            )
            return TelegramSendResult(
                False, attempts=attempt + 1, attempt_errors=attempt_errors,
                duration_seconds=request_duration, http_errors=http_errors,
            )

        duration = max(0, monotonic() - request_started)
        request_duration += duration
        status = getattr(response, "status_code", None)
        if response.ok:
            message_id = None
            telegram_date = None
            try:
                payload = response.json()
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict):
                result = payload.get("result")
                if isinstance(result, dict):
                    candidate = result.get("message_id")
                    if isinstance(candidate, (int, str)) and not isinstance(candidate, bool):
                        message_id = candidate
                    candidate_date = result.get("date")
                    if (isinstance(candidate_date, (int, float))
                            and not isinstance(candidate_date, bool)):
                        telegram_date = float(candidate_date)
            confirmed_at = telegram_date if telegram_date is not None else clock()
            print(
                "TELEGRAM tentative | début_utc:", _utc_iso(attempt_at),
                "| tentative:", f"{attempt + 1}/{TELEGRAM_MAX_ATTEMPTS}",
                "| résultat: succès | HTTP:", status if status is not None else "n/a",
                "| durée_s:", round(duration, 3),
                "| confirmé_utc:", _utc_iso(confirmed_at),
                "| message_id:", message_id if message_id is not None else "indisponible",
            )
            return TelegramSendResult(
                True, message_id, telegram_date, attempt + 1,
                attempt_errors, request_duration, http_errors,
            )

        if status == 429:
            attempt_errors += 1
            http_errors += 1
            try:
                wait = int(response.json()["parameters"]["retry_after"])
            except (KeyError, TypeError, ValueError):
                wait = 30
            print(
                "TELEGRAM tentative | début_utc:", _utc_iso(attempt_at),
                "| tentative:", f"{attempt + 1}/{TELEGRAM_MAX_ATTEMPTS}",
                "| résultat: rate_limited | HTTP: 429",
                "| durée_s:", round(duration, 3), "| retry_after_s:", wait,
            )
            if attempt < TELEGRAM_MAX_ATTEMPTS - 1:
                sleep(min(max(wait, 1), TELEGRAM_MAX_RETRY_AFTER_SECONDS))
            continue

        message = f"HTTP {status}"
        if status is not None:
            http_errors += 1
        attempt_errors += 1
        print(
            "TELEGRAM tentative | début_utc:", _utc_iso(attempt_at),
            "| tentative:", f"{attempt + 1}/{TELEGRAM_MAX_ATTEMPTS}",
            "| résultat: échec | HTTP:", status if status is not None else "n/a",
            "| catégorie:", _http_status_category(status),
            "| durée_s:", round(duration, 3),
        )
        if status in (401, 403):
            raise GlobalTelegramError(message, http_status=status)
        if isinstance(status, int) and 400 <= status < 500:
            raise PermanentTelegramError(message, http_status=status)
        return TelegramSendResult(
            False, attempts=attempt + 1, attempt_errors=attempt_errors,
            duration_seconds=request_duration, http_errors=http_errors,
        )
    return TelegramSendResult(
        False, attempts=TELEGRAM_MAX_ATTEMPTS, attempt_errors=attempt_errors,
        duration_seconds=request_duration, http_errors=http_errors,
    )


def process_queue(
    state, seen, token, chat_id, state_path="state.json", dry_run=True,
    sender=send_message, sleep=time.sleep, max_per_run=MAX_PAR_RUN,
    interval_seconds=None, time_limit=11 * 60, clock=time.time,
    monotonic=time.monotonic, metrics=None,
):
    if dry_run:
        print("DRY-RUN : aucune requête Telegram et aucune sauvegarde.")
        for item in state["file"]:
            print("SIMULATION |", item.get("pub") or "?", "|", _feed_label(item.get("link", "")))
        return 0

    interval = get_telegram_interval_seconds(interval_seconds)
    state.setdefault("last_successful_send_at", None)
    state.setdefault("last_successful_message_id", None)
    state.setdefault("admission_buffer", [])
    state.setdefault("date_quarantine", [])
    promoted_during_publication = _drain_admission_buffer(state, clock())

    # La file et les identifiants sont persistés avant toute tentative d'envoi.
    save_state(state, seen, state_path)
    started = monotonic()
    queue_before = len(state["file"])
    sent = 0
    cadence_wait_seconds = 0.0
    telegram_errors = 0
    telegram_send_operations = 0
    telegram_attempts = 0
    telegram_attempt_errors = 0
    telegram_http_errors = 0
    telegram_call_duration = 0.0
    telegram_request_duration = 0.0
    successful_intervals = []
    queue_max = max(queue_before, int((metrics or {}).get("queue_max", 0)))
    last_success = state.get("last_successful_send_at")
    next_allowed = last_success + interval if last_success is not None else None
    print(
        "CADENCE Telegram | intervalle_s:", interval,
        "| dernier_succès_utc:", _utc_iso(last_success) if last_success is not None else "aucun",
        "| prochaine_publication_utc:", _utc_iso(next_allowed) if next_allowed is not None else "immédiate",
        "| file:", len(state["file"]),
    )

    while state["file"] and sent < max_per_run:
        elapsed = monotonic() - started
        remaining_budget = time_limit - elapsed
        if remaining_budget < MAX_TELEGRAM_SEND_BUDGET_SECONDS:
            print(
                "PUBLICATION REPORTÉE | raison: budget_insuffisant",
                "| file:", len(state["file"]),
                "| prochaine_publication_utc:", _utc_iso(next_allowed) if next_allowed is not None else "immédiate",
            )
            break

        now = clock()
        last_success = state.get("last_successful_send_at")
        next_allowed = last_success + interval if last_success is not None else now
        wait_seconds = max(0, next_allowed - now)
        if wait_seconds:
            if wait_seconds > MAX_INLINE_CADENCE_WAIT_SECONDS:
                print(
                    "PUBLICATION REPORTÉE | raison: attente_trop_longue_pour_ce_workflow",
                    "| file:", len(state["file"]),
                    "| attente_s:", round(wait_seconds, 3),
                    "| prochaine_publication_utc:", _utc_iso(next_allowed),
                )
                break
            if wait_seconds + MAX_TELEGRAM_SEND_BUDGET_SECONDS > remaining_budget:
                print(
                    "PUBLICATION REPORTÉE | raison: budget_insuffisant_avant_prochaine_cadence",
                    "| file:", len(state["file"]),
                    "| prochaine_publication_utc:", _utc_iso(next_allowed),
                )
                break
            print(
                "ATTENTE CADENCE | secondes:", round(wait_seconds, 3),
                "| file:", len(state["file"]),
                "| prochaine_publication_utc:", _utc_iso(next_allowed),
            )
            sleep(wait_seconds)
            cadence_wait_seconds += max(0.0, monotonic() - (started + elapsed))
            if clock() < next_allowed:
                print(
                    "PUBLICATION REPORTÉE | raison: attente_interrompue",
                    "| file:", len(state["file"]),
                    "| prochaine_publication_utc:", _utc_iso(next_allowed),
                )
                break

        item = state["file"][0]
        call_started = monotonic()
        telegram_send_operations += 1
        try:
            outcome = sender(item, token, chat_id)
        except GlobalTelegramError as exc:
            telegram_call_duration += max(0.0, monotonic() - call_started)
            telegram_errors += 1
            telegram_attempts += 1
            telegram_attempt_errors += 1
            if getattr(exc, "http_status", None) is not None:
                telegram_http_errors += 1
            # Erreur globale d'identifiants/droits : ne jamais mettre les articles
            # en quarantaine. On arrête et conserve la file pour correction.
            print("ARRÊT DE SÉCURITÉ Telegram | file préservée |", exc)
            break
        except PermanentTelegramError as exc:
            telegram_call_duration += max(0.0, monotonic() - call_started)
            telegram_errors += 1
            telegram_attempts += 1
            telegram_attempt_errors += 1
            if getattr(exc, "http_status", None) is not None:
                telegram_http_errors += 1
            # Une erreur propre à l'article ne sera pas corrigée par une nouvelle tentative.
            # Isoler l’article défectueux pour ne pas bloquer toute la file.
            state["erreurs"].append({
                "item": item, "error": str(exc), "failed_at": int(clock()),
            })
            state["file"].pop(0)
            promoted_during_publication += _drain_admission_buffer(state, clock())
            save_state(state, seen, state_path)
            print("ARTICLE MIS EN QUARANTAINE |", _feed_label(item.get("link", "")), "|", exc)
            queue_max = max(queue_max, len(state["file"]))
            continue
        telegram_call_duration += max(0.0, monotonic() - call_started)
        if isinstance(outcome, TelegramSendResult):
            telegram_attempts += outcome.attempts
            telegram_attempt_errors += outcome.attempt_errors
            telegram_request_duration += outcome.duration_seconds
            telegram_http_errors += outcome.http_errors
        else:
            telegram_attempts += 1
        if not outcome:
            # Résultat ambigu ou erreur temporaire : conserver l'article en tête.
            telegram_errors += 1
            if not isinstance(outcome, TelegramSendResult) or not outcome.attempt_errors:
                telegram_attempt_errors += 1
            break

        telegram_date = outcome.telegram_date if isinstance(outcome, TelegramSendResult) else None
        message_id = outcome.message_id if isinstance(outcome, TelegramSendResult) else None
        successful_at = clock()
        if telegram_date is not None:
            # Le champ date Telegram est à la seconde : arrondir prudemment vers
            # le futur pour ne jamais raccourcir l'intervalle minimal.
            successful_at = max(successful_at, telegram_date + 1)
        previous_success = state.get("last_successful_send_at")
        if previous_success is not None and successful_at >= previous_success:
            successful_intervals.append(round(successful_at - previous_success, 3))
        state["file"].pop(0)
        sent += 1
        state["last_successful_send_at"] = successful_at
        state["last_successful_message_id"] = message_id
        promoted_during_publication += _drain_admission_buffer(state, successful_at)
        try:
            save_state(state, seen, state_path)
        except Exception as exc:
            print(
                "ALERTE PERSISTANCE | Telegram avait confirmé l'envoi",
                "| source:", _feed_label(item.get("link", "")),
                "| message_id:", message_id if message_id is not None else "indisponible",
                "| état_non_sauvé:", type(exc).__name__,
                "| doublon_possible_après_reprise: oui",
            )
            raise
        next_allowed = successful_at + interval
        queue_max = max(queue_max, len(state["file"]))
        print(
            "ENVOYÉ | publié:", item.get("pub") or "?",
            "| source:", _feed_label(item["link"]),
            "| confirmé_utc:", _utc_iso(successful_at),
            "| message_id:", message_id if message_id is not None else "indisponible",
            "| file:", len(state["file"]),
            "| prochaine_publication_utc:", _utc_iso(next_allowed),
        )

    save_state(state, seen, state_path)
    remaining = state["file"]
    publication_duration = round(max(0.0, monotonic() - started), 3)
    last_success = state.get("last_successful_send_at")
    next_allowed = last_success + interval if last_success is not None else None
    print(
        "FILE D'ATTENTE après envoi :", len(remaining), "article(s) |",
        sent, "envoyé(s) ce passage | plus ancien en attente, publié :",
        (remaining[0].get("pub") or "?") if remaining else "-",
        "| prochaine_publication_utc:", _utc_iso(next_allowed) if next_allowed is not None else "immédiate",
    )
    if metrics is not None:
        metrics.update({
            "queue_before_publication": queue_before,
            "queue_final": len(remaining),
            "queue_max": max(queue_max, len(remaining)),
            "admission_buffer_final": len(state["admission_buffer"]),
            "date_quarantine_final": len(state["date_quarantine"]),
            "pending_total_final": (
                len(remaining) + len(state["admission_buffer"])
                + len(state["date_quarantine"])
            ),
            "articles_promoted_from_admission_buffer": (
                int((metrics or {}).get(
                    "articles_promoted_from_admission_buffer", 0,
                )) + promoted_during_publication
            ),
            "published": sent,
            "publication_deferred": len(remaining),
            "publication_seconds": publication_duration,
            "cadence_wait_seconds": round(cadence_wait_seconds, 3),
            "telegram_errors": telegram_errors,
            "telegram_send_operations": telegram_send_operations,
            "telegram_attempts": telegram_attempts,
            "telegram_attempt_errors": telegram_attempt_errors,
            "telegram_http_errors": telegram_http_errors,
            "telegram_call_duration_seconds": round(telegram_call_duration, 3),
            "telegram_request_duration_seconds": round(telegram_request_duration, 3),
            "intervals_seconds": successful_intervals,
            "next_publication_allowed_utc": _utc_iso(next_allowed) if next_allowed is not None else None,
        })
    return sent


def _previous_simulation(history):
    if not history or not isinstance(history[-1], dict):
        return None
    latest = history[-1]
    return {
        "interval_seconds": latest.get("simulated_interval_seconds", 30),
        "candidate_interval_seconds": latest.get("simulated_candidate_interval_seconds"),
        "candidate_observations": latest.get("simulated_candidate_observations", 0),
    }


def main():
    # Double verrou : sans ALLOW_TELEGRAM_SEND=1, publication impossible.
    # DRY_RUN=1 reste prioritaire même si l'autorisation est présente.
    dry_run = os.getenv("DRY_RUN", "1").strip().lower() in ("1", "true", "yes")
    allow_send = os.getenv("ALLOW_TELEGRAM_SEND") == "1"
    if not allow_send:
        dry_run = True

    interval_seconds = get_telegram_interval_seconds()
    freshness_window_seconds = get_article_max_age_seconds()

    token = None
    chat_id = None
    if not dry_run:
        token = os.getenv("TELEGRAM_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT")
        if not token or not chat_id:
            raise RuntimeError("Publication refusée : TELEGRAM_TOKEN et TELEGRAM_CHAT sont requis.")

    run_started = time.monotonic()
    run_started_epoch = time.time()
    feeds = load_feeds(os.getenv("FEEDS_FILE", "feeds.txt"))
    state_path = os.getenv("STATE_FILE", "state.json")
    state = load_state(state_path)
    state.setdefault("admission_buffer", [])
    state.setdefault("date_quarantine", [])
    state.setdefault("url_seen", [])
    history = state.get("metrics_history", [])
    metrics = {
        "run_started_utc": _utc_iso(run_started_epoch),
        "run_started_epoch": run_started_epoch,
        "telegram_interval_configured_seconds": interval_seconds,
        "article_max_age_hours": freshness_window_seconds / 3600,
        "queue_initial": len(state["file"]),
        "queue_max": len(state["file"]),
    }
    checkpoint = None if dry_run else lambda current, seen: save_state(current, seen, state_path)
    seen = collect_articles(
        feeds, state, budget=min(COLLECT_BUDGET, RUN_BUDGET), checkpoint=checkpoint,
        metrics=metrics, max_age_seconds=freshness_window_seconds,
    )

    print("FILE D'ATTENTE avant traitement :", len(state["file"]), "article(s)")
    available_for_publication = max(0.0, RUN_BUDGET - (time.monotonic() - run_started))
    oldest_age_before = _oldest_pending_age_seconds(
        state["file"], admission_buffer=state["admission_buffer"],
    )
    previous_success = state.get("last_successful_send_at")
    seconds_since_last_success = (
        max(0.0, time.time() - previous_success)
        if previous_success is not None else None
    )
    metrics["oldest_pending_age_before_publication_seconds"] = oldest_age_before
    metrics["simulation"] = cadence_simulator.build_simulation(
        history=history,
        queue_depth=len(state["file"]) + len(state["admission_buffer"]),
        oldest_age_seconds=oldest_age_before,
        available_seconds=available_for_publication,
        run_started_epoch=run_started_epoch,
        articles_discovered=metrics.get("articles_discovered", 0),
        articles_admissible=metrics.get("articles_admissible", 0),
        previous_simulation=_previous_simulation(history),
        next_run_seconds=300.0,
        workflow_nonpublishing_seconds=(
            metrics.get("collection_seconds", 0)
            + cadence_simulator.estimated_nonpublishing_seconds(history)
        ),
        send_budget_floor_seconds=MAX_TELEGRAM_SEND_BUDGET_SECONDS,
        seconds_since_last_success=seconds_since_last_success,
        max_per_run=MAX_PAR_RUN,
        max_queue=MAX_FILE + MAX_ADMISSION_BUFFER,
        send_request_seconds=cadence_simulator.estimated_send_request_seconds(
            history, fallback=DEFAULT_ESTIMATED_SEND_SECONDS,
        ),
        send_operation_seconds=cadence_simulator.estimated_send_operation_seconds(
            history, fallback=DEFAULT_ESTIMATED_SEND_SECONDS,
        ),
    )
    if dry_run:
        process_queue(
            state, seen, None, None, state_path=state_path, dry_run=True,
            interval_seconds=interval_seconds,
        )
        metrics.update({
            "queue_after_publication": len(state["file"]),
            "queue_final": len(state["file"]),
            "admission_buffer_final": len(state["admission_buffer"]),
            "date_quarantine_final": len(state["date_quarantine"]),
            "pending_total_final": (
                len(state["file"]) + len(state["admission_buffer"])
                + len(state["date_quarantine"])
            ),
            "publication_deferred": len(state["file"]),
            "published": 0,
            "publication_seconds": 0.0,
            "cadence_wait_seconds": 0.0,
            "telegram_errors": 0,
            "telegram_send_operations": 0,
            "telegram_attempts": 0,
            "telegram_attempt_errors": 0,
            "telegram_http_errors": 0,
            "telegram_call_duration_seconds": 0.0,
            "telegram_request_duration_seconds": 0.0,
            "intervals_seconds": [],
            "next_publication_allowed_utc": (
                _utc_iso(previous_success + interval_seconds)
                if previous_success is not None else None
            ),
        })
        metrics["oldest_pending_age_seconds"] = _oldest_pending_age_seconds(
            state["file"], admission_buffer=state["admission_buffer"],
        )
        metrics["total_seconds"] = round(time.monotonic() - run_started, 3)
        _log_phase51_metrics(metrics)
        return

    remaining = max(0, RUN_BUDGET - (time.monotonic() - run_started))
    process_queue(
        state, seen, token, chat_id, state_path=state_path, dry_run=False,
        interval_seconds=interval_seconds, time_limit=remaining, metrics=metrics,
    )
    metrics["queue_after_publication"] = metrics.get("queue_final", len(state["file"]))
    metrics["admission_buffer_final"] = len(state["admission_buffer"])
    metrics["date_quarantine_final"] = len(state["date_quarantine"])
    metrics["pending_total_final"] = (
        len(state["file"]) + len(state["admission_buffer"])
        + len(state["date_quarantine"])
    )
    metrics["oldest_pending_age_seconds"] = _oldest_pending_age_seconds(
        state["file"], admission_buffer=state["admission_buffer"],
    )
    metrics["total_seconds"] = round(time.monotonic() - run_started, 3)
    _store_run_metrics(state, metrics)
    save_state(state, seen, state_path)
    metrics["total_seconds"] = round(time.monotonic() - run_started, 3)
    _log_phase51_metrics(metrics)


if __name__ == "__main__":
    main()
