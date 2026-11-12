"""Interprétation des dates et métadonnées de quarantaine, à temps fourni."""
import calendar
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from .settings import FUTURE_DATE_TOLERANCE_SECONDS, MAX_FUTURE_DATE_DAYS, UNKNOWN_DATE_QUARANTINE_SECONDS


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
