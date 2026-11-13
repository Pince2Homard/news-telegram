"""Orchestration de la collecte et de la diffusion Telegram."""
import os
import time
from newsbot.collection import load_feeds, collect_articles
from newsbot.persistence import load_state, save_state
from newsbot.publication import process_queue
from newsbot.settings import (
    COLLECT_BUDGET, RUN_BUDGET,
    get_telegram_interval_seconds, get_article_max_age_seconds,
)
from newsbot.metrics import (
    _utc_iso, _oldest_pending_age_seconds, _store_run_metrics, _log_phase51_metrics,
)


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
    previous_success = state.get("last_successful_send_at")
    metrics["oldest_pending_age_before_publication_seconds"] = _oldest_pending_age_seconds(
        state["file"], admission_buffer=state["admission_buffer"],
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
