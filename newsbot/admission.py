"""Admission des entrées parsées et transitions des files, sans réseau."""
import html
import math

from .settings import (
    MAX_FILE, MAX_ADMISSION_BUFFER,
    MAX_DATE_QUARANTINE, MAX_FUTURE_DATE_DAYS,
    UNKNOWN_DATE_QUARANTINE_SECONDS, FUTURE_DATE_TOLERANCE_SECONDS,
)
from .deduplication import (
    is_http_url, _remember_url, _canonical_url_fingerprint, _entry_uid,
    _quarantine_match,
)
from .dates import (
    _classify_publication, _age_bucket, _quarantine_deadline,
    _make_quarantine_record, _refresh_quarantine_record,
)


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


def prepare_pending_articles(
    state, *, seen, url_seen, active_url_seen, counts, now_epoch,
    queue_clock, max_age_seconds, queue_max,
):
    """Transfère les admis puis expire/réessaie la quarantaine avant le réseau.

    Modifie les files, historiques et compteurs passés explicitement ; renvoie
    la profondeur maximale observée de la file de publication.
    """
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

    return queue_max


def admit_feed_entries(
    entries, state, url, *, was_initialized, seen, url_seen, active_url_seen,
    counts, now_epoch, queue_clock, max_age_seconds, queue_max,
):
    """Applique amorçage, doublons, dates et capacités à un flux déjà parsé.

    Aucun transport ni sauvegarde. Modifie l’état, les index et les compteurs
    du passage, puis renvoie (profondeur maximale, ajoutés au flux, différés).
    L’horloge d’admission est fournie par l’appelant.
    """
    feed_deferred = 0
    feed_added = 0
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

    return queue_max, feed_added, feed_deferred


def release_due_future_articles(
    state, *, seen, url_seen, active_url_seen, counts, now_epoch,
    queue_clock, max_age_seconds, queue_max,
):
    """Réexamine les dates futures mémorisées après le parcours des flux.

    Le parcours RSS a priorité pour corriger une date. En son absence, la date
    mémorisée reste utilisable selon les règles existantes. Modifie files,
    historiques et compteurs ; renvoie la profondeur maximale de publication.
    """
    quarantine = state["date_quarantine"]
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

    return queue_max
