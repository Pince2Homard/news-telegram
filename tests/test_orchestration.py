"""Parcours complet avec état temporaire, transport et horloges simulés."""
import contextlib
from functools import partial
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cadence_simulator
import poster
from newsbot import collection, persistence, publication


class Clock:
    def __init__(self):
        self.value = 1_800_000_000.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class OrchestrationTests(unittest.TestCase):
    def run_bot(self, *, dry_run):
        clock = Clock()
        feed = "https://example.org/feed"
        pending = {"text": "pending", "link": "https://example.org/pending"}
        parser = Mock(return_value=Mock(entries=[{
            "id": "fresh", "title": "fresh", "link": "https://example.org/fresh",
            "published_parsed": time.gmtime(clock()),
        }]))
        transport = Mock(get=Mock(return_value=Mock(content=b"feed")))
        sent_at = []

        def sender(item, token, chat):
            sent_at.append((item["link"], clock()))
            return publication.TelegramSendResult(True, message_id=len(sent_at))

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            feeds_path = Path(directory) / "feeds.txt"
            feeds_path.write_text(feed, encoding="utf-8")
            state_path.write_text(json.dumps({
                "vus": [], "file": [pending], "feeds": [feed], "init": True,
                "last_successful_send_at": clock() - 10,
                "metrics_history": [{
                    "run_started_epoch": clock() - 3600,
                    "simulated_interval_seconds": 15,
                }],
            }), encoding="utf-8")
            before = state_path.read_bytes()
            with patch.dict("os.environ", {
                "DRY_RUN": str(int(dry_run)), "ALLOW_TELEGRAM_SEND": "1",
                "TELEGRAM_TOKEN": "synthetic", "TELEGRAM_CHAT": "synthetic",
                "TELEGRAM_INTERVAL_SECONDS": "60", "STATE_FILE": str(state_path),
                "FEEDS_FILE": str(feeds_path),
            }, clear=True), patch.object(poster.time, "time", clock), \
                    patch.object(poster.time, "monotonic", clock), \
                    patch.object(poster, "collect_articles", partial(
                        collection.collect_articles, http=transport, parser=parser,
                        clock=clock, queue_clock=clock,
                    )), patch.object(poster, "process_queue", partial(
                        publication.process_queue, sender=sender, sleep=clock.sleep,
                        clock=clock, monotonic=clock,
                    )), patch.object(cadence_simulator, "build_simulation",
                                     side_effect=AssertionError("simulation hors production")), \
                    contextlib.redirect_stdout(io.StringIO()):
                poster.main()
            return sent_at, persistence.load_state(state_path), before, state_path.read_bytes()

    def test_real_path_keeps_configured_cadence_and_persists_useful_metrics(self):
        sent, state, _, _ = self.run_bot(dry_run=False)
        self.assertEqual(sent, [
            ("https://example.org/pending", 1_800_000_050),
            ("https://example.org/fresh", 1_800_000_110),
        ])
        self.assertEqual(state["file"], [])
        self.assertEqual(state["last_successful_message_id"], 2)
        latest = state["metrics_history"][-1]
        self.assertEqual(latest["published"], 2)
        self.assertEqual(latest["articles_admitted"], 1)
        self.assertEqual(latest["intervals_seconds"], [60, 60])
        self.assertEqual(latest["pending_total_final"], 0)
        self.assertNotIn("simulated_interval_seconds", latest)
        self.assertEqual(state["metrics_history"][0]["simulated_interval_seconds"], 15)

    def test_explicit_dry_run_overrides_send_permission_without_writing(self):
        sent, _, before, after = self.run_bot(dry_run=True)
        self.assertEqual(sent, [])
        self.assertEqual(before, after)
