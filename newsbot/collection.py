"""Téléchargement RSS/Atom, parcours borné des flux et points de reprise."""
from contextlib import nullcontext
import json
import time
import feedparser
import requests
from .settings import (
    COLLECT_BUDGET, CHECKPOINT_EVERY, MAX_URL_SEEN, get_article_max_age_seconds,
)
from .admission import (
    prepare_pending_articles, admit_feed_entries, release_due_future_articles,
)
from .deduplication import prepare_seen_indexes
from .metrics import _http_status_from_exception, _http_status_category, _retry_after_seconds, _feed_label

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
def load_feeds(path="feeds.txt"):
    with open(path, encoding="utf-8") as source:
        return [
            line.strip() for line in source
            if line.strip() and not line.lstrip().startswith("#")
        ]


def parse_feed(
    url, http=requests, parser=feedparser.parse, timeout=(5, 20),
    timings=None, timing_clock=time.perf_counter,
):
    """Consomme et ferme la réponse avant parsing ; aucun réessai ajouté."""
    response = None
    started = timing_clock()
    try:
        response = http.get(url, headers={"User-Agent": UA}, timeout=timeout)
        response.raise_for_status()
        content = response.content
    finally:
        # Les doubles historiques peuvent ne pas implémenter close().
        if response is not None:
            close = getattr(response, "close", None)
            if close is not None:
                close()
        if timings is not None:
            timings["network"] = timing_clock() - started
    started = timing_clock()
    try:
        return parser(content)
    finally:
        if timings is not None:
            timings["parsing"] = timing_clock() - started


def entries_for_feed(
    url, http=requests, parser=feedparser.parse,
    timings=None, timing_clock=time.perf_counter,
):
    """Récupère directement les entrées d’un flux RSS/Atom configuré."""
    return parse_feed(
        url, http=http, parser=parser, timings=timings, timing_clock=timing_clock,
    ).entries


def _log_feed_timings(url, timings, admission_seconds=None):
    print(
        "RSS phases |", _feed_label(url),
        "| réseau_s:", round(timings["network"], 6),
        "| parsing_s:", round(timings["parsing"], 6) if "parsing" in timings else "n/a",
        "| admission_s:", round(admission_seconds, 6) if admission_seconds is not None else "n/a",
    )


def collect_articles(
    feeds, state, http=None, parser=feedparser.parse,
    budget=COLLECT_BUDGET, clock=time.monotonic, checkpoint=None,
    checkpoint_every=CHECKPOINT_EVERY, metrics=None, queue_clock=time.time,
    max_age_seconds=None, timing_clock=time.perf_counter,
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
    seen, url_seen, active_url_seen = prepare_seen_indexes(state)

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
    queue_max = prepare_pending_articles(
        state, seen=seen, url_seen=url_seen, active_url_seen=active_url_seen,
        counts=counts, now_epoch=now_epoch, queue_clock=queue_clock,
        max_age_seconds=max_age_seconds, queue_max=queue_max,
    )

    # Seule la collecte possède cette session ; un transport injecté reste
    # la propriété de l'appelant. La sortie du bloc ferme aussi les connexions
    # si parsing, admission ou checkpoint interrompt le parcours.
    with requests.Session() if http is None else nullcontext(http) as transport:
        for index, url in enumerate(ordered_feeds):
            if clock() - started >= budget:
                break
            feed_started = clock()
            counts["attempted"] += 1
            timings = {}
            try:
                entries = entries_for_feed(
                    url, http=transport, parser=parser,
                    timings=timings, timing_clock=timing_clock,
                )
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
                    _log_feed_timings(url, timings)
                state["feed_offset"] = (offset + index + 1) % len(feeds)
                if checkpoint:
                    checkpoint(state, seen)
                continue
            finally:
                if http is None:
                    # requests.get() isolait auparavant les cookies de chaque flux.
                    # Garder le pool HTTP, sans transférer de cookies au flux suivant.
                    transport.cookies.clear()

            admission_started = timing_clock()
            queue_max, feed_added, feed_deferred = admit_feed_entries(
                entries, state, url, was_initialized=was_initialized, seen=seen,
                url_seen=url_seen, active_url_seen=active_url_seen, counts=counts,
                now_epoch=now_epoch, queue_clock=queue_clock,
                max_age_seconds=max_age_seconds, queue_max=queue_max,
            )

            admission_seconds = timing_clock() - admission_started
            if feed_deferred:
                counts["deferred_feeds"] += 1
            duration = round(clock() - feed_started, 3)
            print("RSS |", _feed_label(url), "|", "vide" if not entries else "ok",
                  "|", len(entries), "entrée(s) | +", feed_added,
                  "article(s) | reportés:", feed_deferred, "|", duration, "s")
            _log_feed_timings(url, timings, admission_seconds)
            # Le curseur pointe sur le prochain flux avant que l’état soit écrit :
            # une interruption au flux suivant reprend sans rebalayer le préfixe.
            state["feed_offset"] = (offset + index + 1) % len(feeds)
            if checkpoint and (index + 1) % max(1, checkpoint_every) == 0:
                checkpoint(state, seen)

    queue_max = release_due_future_articles(
        state, seen=seen, url_seen=url_seen, active_url_seen=active_url_seen,
        counts=counts, now_epoch=now_epoch, queue_clock=queue_clock,
        max_age_seconds=max_age_seconds, queue_max=queue_max,
    )

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
