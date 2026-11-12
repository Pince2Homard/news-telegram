"""metrics : responsabilité extraite sans changement de règle."""
import hashlib
import json
import math
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
from .settings import MAX_METRICS_HISTORY


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


def _feed_label(url):
    """Label stable de journal sans paramètres ni identifiants d’URL."""
    parsed = urlparse(url)
    host = parsed.hostname or "flux-inconnu"
    fingerprint = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return f"{host}#{fingerprint}"


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
