import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import poster
from newsbot import collection, persistence, admission, dates, deduplication, metrics as run_metrics, publication, settings


TEST_RSS_NOW = 1_800_000_000
_COLLECT_ARTICLES_RAW = collection.collect_articles


def collect_with_fresh_fixtures(*args, **kwargs):
    """Legacy collection tests model newly published entries unless stated otherwise."""
    parser = kwargs.get("parser", collection.feedparser.parse)

    def parser_with_fresh_default(content):
        parsed = parser(content)
        entries = getattr(parsed, "entries", None)
        if isinstance(entries, list):
            for entry in entries:
                if (isinstance(entry, dict) and not entry.get("published")
                        and not entry.get("published_parsed")):
                    entry["published_parsed"] = time.gmtime(TEST_RSS_NOW)
        return parsed

    kwargs["parser"] = parser_with_fresh_default
    kwargs.setdefault("queue_clock", lambda: TEST_RSS_NOW)
    kwargs.setdefault("max_age_seconds", 24 * 3600)
    return _COLLECT_ARTICLES_RAW(*args, **kwargs)


class FakeClock:
    def __init__(self, value=0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds

    def advance(self, seconds):
        self.value += seconds


class PosterSafetyTests(unittest.TestCase):
    def collect_entries(self, entries, state=None, now=TEST_RSS_NOW,
                        feed="https://example.org/feed", parser=None):
        state = state or {
            "vus": [], "file": [], "feeds": [feed], "yt": {}, "init": True,
        }
        response = Mock(content=feed.encode())
        response.raise_for_status.return_value = None
        transport = Mock()
        transport.get.return_value = response
        if parser is None:
            parser = Mock(return_value=Mock(entries=entries))
        seen = _COLLECT_ARTICLES_RAW(
            [feed], state, http=transport, parser=parser,
            queue_clock=lambda: now, max_age_seconds=24 * 3600,
        )
        return state, seen, transport

    def test_main_is_dry_run_without_explicit_send_permission(self):
        with patch.dict(os.environ, {"DRY_RUN": "0"}, clear=True):
            with patch.object(poster, "load_feeds", return_value=[]), \
                 patch.object(poster, "load_state", return_value={"vus": [], "file": [], "feeds": [], "yt": {}}), \
                 patch.object(poster, "collect_articles", return_value={}), \
                 patch.object(poster, "process_queue") as process:
                poster.main()
        self.assertTrue(process.call_args.kwargs["dry_run"])
        self.assertIsNone(process.call_args.args[2])

    def test_main_requires_credentials_when_send_is_explicitly_enabled(self):
        with patch.dict(os.environ, {"DRY_RUN": "0", "ALLOW_TELEGRAM_SEND": "1"}, clear=True):
            with patch.object(poster, "load_feeds", return_value=[]), \
                 patch.object(poster, "load_state", return_value={"vus": [], "file": [], "feeds": [], "yt": {}}), \
                 patch.object(poster, "collect_articles", return_value={}), \
                 patch.object(poster, "process_queue") as process:
                with self.assertRaisesRegex(RuntimeError, "TELEGRAM_TOKEN"):
                    poster.main()
        process.assert_not_called()

    def test_format_item_escapes_html_and_rejects_invalid_urls(self):
        item = admission.format_item({
            "title": "<script>alert(1)</script>",
            "link": "https://example.org/?a=1&b=2",
        })
        self.assertIn("&lt;script&gt;", item["text"])
        self.assertIn("&amp;", item["text"])
        self.assertIsNone(admission.format_item({"title": "bad", "link": "javascript:alert(1)"}))
        self.assertIsNone(admission.format_item({"title": "bad", "link": ""}))

    def test_dry_run_never_calls_sender_or_writes_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.json")
            state = {"vus": [], "file": [{"text": "test", "link": "https://example.org"}]}
            sender = Mock(side_effect=AssertionError("Telegram ne doit jamais être appelé"))
            sent = publication.process_queue(
                state, {}, "fake-token", "fake-chat",
                state_path=state_path, dry_run=True, sender=sender,
            )
            self.assertEqual(sent, 0)
            sender.assert_not_called()
            self.assertFalse(Path(state_path).exists())

    def test_state_is_written_as_valid_json_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = {"file": [], "feeds": [], "yt": {}, "v": 2}
            persistence.save_state(state, {"one": None, "two": None}, str(path))
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["vus"], ["one", "two"])
            self.assertFalse(list(Path(directory).glob("*.tmp")))

    def test_corrupt_state_is_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                persistence.load_state(str(path))

    def test_state_migration_preserves_pending_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            pending = [{
                "text": "<b>à publier</b>", "link": "https://example.org",
                "pub": "", "queued_at": 123, "first_seen_at": 100,
            }]
            path.write_text(json.dumps({"vus": ["old"], "file": pending, "feeds": ["feed"], "v": 1}))
            migrated = persistence.load_state(str(path))
            self.assertEqual(migrated["v"], 2)
            self.assertEqual(migrated["file"], pending)
            self.assertEqual(migrated["feeds"], ["feed"])
            self.assertEqual(migrated["admission_buffer"], [])
            self.assertEqual(migrated["date_quarantine"], [])
            self.assertEqual(migrated["url_seen"], [])

    def test_freshness_accepts_23h59_and_exactly_24h_but_rejects_24h01(self):
        for age_seconds, expected_queue, expected_rejected in (
            (23 * 3600 + 59 * 60, 1, 0),
            (24 * 3600, 1, 0),
            (24 * 3600 + 60, 0, 1),
        ):
            with self.subTest(age_seconds=age_seconds):
                entry = {
                    "id": f"article-{age_seconds}", "title": "Article",
                    "link": f"https://example.org/{age_seconds}",
                    "published_parsed": time.gmtime(TEST_RSS_NOW - age_seconds),
                }
                metrics = {}
                state = {
                    "vus": [], "file": [], "feeds": ["https://example.org/feed"],
                    "yt": {}, "init": True,
                }
                response = Mock(content=b"<rss/>")
                response.raise_for_status.return_value = None
                transport = Mock()
                transport.get.return_value = response
                parser = Mock(return_value=Mock(entries=[entry]))
                _COLLECT_ARTICLES_RAW(
                    ["https://example.org/feed"], state, http=transport,
                    parser=parser, queue_clock=lambda: TEST_RSS_NOW,
                    max_age_seconds=24 * 3600, metrics=metrics,
                )
                self.assertEqual(len(state["file"]), expected_queue)
                self.assertEqual(metrics["articles_rejected_by_freshness"], expected_rejected)

    def test_missing_invalid_and_day_only_dates_are_quarantined_not_published(self):
        entries = [
            {
                "id": "missing", "title": "Missing date",
                "link": "https://example.org/missing",
                "updated_parsed": time.gmtime(TEST_RSS_NOW),
            },
            {
                "id": "invalid", "title": "Invalid date",
                "link": "https://example.org/invalid", "published": "not-a-date",
            },
            {
                "id": "day-only", "title": "Imprecise date",
                "link": "https://example.org/day", "published": "2026-11-11",
                "published_parsed": time.gmtime(TEST_RSS_NOW),
            },
        ]
        state, seen, _ = self.collect_entries(entries)
        self.assertEqual(state["file"], [])
        self.assertEqual(len(state["date_quarantine"]), 3)
        self.assertFalse({"missing", "invalid", "day-only"} & set(seen))
        self.assertEqual(
            {record["reason"] for record in state["date_quarantine"]},
            {"unknown_date"},
        )

    def test_unknown_date_quarantine_expires_after_24_hours_without_publication(self):
        entry = {"id": "unknown", "title": "Unknown", "link": "https://example.org/u"}
        state, _, _ = self.collect_entries([entry])
        first_seen = state["date_quarantine"][0]["first_seen_at"]
        state, seen, _ = self.collect_entries(
            [entry], state=state, now=TEST_RSS_NOW + 24 * 3600,
        )
        self.assertEqual(state["file"], [])
        self.assertEqual(state["date_quarantine"], [])
        self.assertIn("unknown", seen)
        self.assertEqual(first_seen, TEST_RSS_NOW)

    def test_quarantined_article_is_released_when_reliable_publication_date_appears(self):
        original = {"id": "repair", "title": "Before", "link": "https://example.org/repair"}
        state, _, _ = self.collect_entries([original])
        first_seen = state["date_quarantine"][0]["first_seen_at"]
        now = TEST_RSS_NOW + 10 * 3600
        corrected = {
            **original, "title": "Corrected title",
            "published_parsed": time.gmtime(now - 60),
        }
        state, seen, _ = self.collect_entries([corrected], state=state, now=now)
        self.assertEqual(len(state["date_quarantine"]), 0)
        self.assertEqual(len(state["file"]), 1)
        self.assertIn("Corrected title", state["file"][0]["text"])
        self.assertEqual(state["file"][0]["first_seen_at"], first_seen)
        self.assertEqual(state["file"][0]["admitted_at"], now)
        self.assertEqual(state["file"][0]["queued_at"], now)
        self.assertIn("repair", seen)

    def test_updated_date_is_never_used_as_publication_date(self):
        entry = {
            "id": "updated-only", "title": "Updated",
            "link": "https://example.org/updated",
            "updated": "2026-11-11T00:00:00Z",
            "updated_parsed": time.gmtime(TEST_RSS_NOW),
        }
        state, _, _ = self.collect_entries([entry])
        self.assertEqual(state["file"], [])
        self.assertEqual(state["date_quarantine"][0]["reason"], "unknown_date")

    def test_future_within_tolerance_is_admitted_and_later_future_is_released(self):
        for offset in (4 * 60, 5 * 60):
            with self.subTest(offset=offset):
                within = {
                    "id": f"skew-{offset}", "title": "Clock skew",
                    "link": f"https://example.org/skew-{offset}",
                    "published_parsed": time.gmtime(TEST_RSS_NOW + offset),
                }
                state, _, _ = self.collect_entries([within])
                self.assertEqual(len(state["file"]), 1)

        future = {
            "id": "future", "title": "Future", "link": "https://example.org/future",
            "published_parsed": time.gmtime(TEST_RSS_NOW + 6 * 60),
        }
        state, _, _ = self.collect_entries([future])
        self.assertEqual(state["file"], [])
        self.assertEqual(state["date_quarantine"][0]["reason"], "future_date")
        ready_at = TEST_RSS_NOW + 6 * 60
        state, seen, _ = self.collect_entries([], state=state, now=ready_at)
        self.assertEqual(len(state["date_quarantine"]), 0)
        self.assertEqual(len(state["file"]), 1)
        self.assertIn("future", seen)

    def test_modified_rss_entry_does_not_requeue_or_replace_an_already_queued_article(self):
        original = {
            "id": "stable-guid", "title": "Original",
            "link": "https://example.org/story?utm_source=feed",
            "published_parsed": time.gmtime(TEST_RSS_NOW - 60),
        }
        state, _, _ = self.collect_entries([original])
        modified = {
            **original, "title": "Updated title",
            "published_parsed": time.gmtime(TEST_RSS_NOW),
        }
        state, _, _ = self.collect_entries(
            [modified], state=state, now=TEST_RSS_NOW + 60,
        )
        self.assertEqual(len(state["file"]), 1)
        self.assertIn("Original", state["file"][0]["text"])
        self.assertNotIn("Updated title", state["file"][0]["text"])

    def test_far_future_date_expires_after_24_hour_correction_grace(self):
        entry = {
            "id": "far-future", "title": "Far future",
            "link": "https://example.org/far-future",
            "published_parsed": time.gmtime(TEST_RSS_NOW + 8 * 86400),
        }
        state, _, _ = self.collect_entries([entry])
        self.assertEqual(state["date_quarantine"][0]["reason"], "future_date")
        state, seen, _ = self.collect_entries(
            [entry], state=state, now=TEST_RSS_NOW + 24 * 3600,
        )
        self.assertEqual(state["date_quarantine"], [])
        self.assertEqual(state["file"], [])
        self.assertIn("far-future", seen)

    def test_unknown_date_corrected_to_stale_is_expired_without_publication(self):
        original = {
            "id": "corrected-old", "title": "Unknown",
            "link": "https://example.org/corrected-old",
        }
        state, _, _ = self.collect_entries([original])
        corrected = {
            **original,
            "published_parsed": time.gmtime(TEST_RSS_NOW - 30 * 3600),
        }
        state, seen, _ = self.collect_entries(
            [corrected], state=state, now=TEST_RSS_NOW + 3600,
        )
        self.assertEqual(state["date_quarantine"], [])
        self.assertEqual(state["file"], [])
        self.assertIn("corrected-old", seen)

    def test_future_quarantine_rechecks_corrected_stale_publication_date(self):
        future = {
            "id": "corrected-future", "title": "Future",
            "link": "https://example.org/corrected-future",
            "published_parsed": time.gmtime(TEST_RSS_NOW + 10 * 60),
        }
        state, _, _ = self.collect_entries([future])
        corrected = {
            **future,
            "published_parsed": time.gmtime(TEST_RSS_NOW - 30 * 3600),
        }
        state, seen, _ = self.collect_entries(
            [corrected], state=state, now=TEST_RSS_NOW + 20 * 60,
        )
        self.assertEqual(state["date_quarantine"], [])
        self.assertEqual(state["file"], [])
        self.assertIn("corrected-future", seen)

    def test_canonical_url_deduplicates_syntax_variants_but_keeps_query_intact(self):
        feeds = ["https://example.org/a", "https://example.net/b"]
        entries = {
            feeds[0]: [{
                "id": "guid-a", "title": "A",
                "link": "https://News.Example.com:443/story?id=7&edition=weekly#top",
                "published_parsed": time.gmtime(TEST_RSS_NOW),
            }],
            feeds[1]: [{
                "id": "guid-b", "title": "B",
                "link": "https://news.example.com/story?id=7&edition=weekly",
                "published_parsed": time.gmtime(TEST_RSS_NOW),
            }],
        }
        transport = Mock()
        transport.get.side_effect = lambda url, **kwargs: Mock(
            content=url.encode(), raise_for_status=Mock(return_value=None),
        )
        parser = Mock(side_effect=lambda content: Mock(entries=entries[content.decode()]))
        state = {
            "vus": [], "file": [], "feeds": feeds.copy(), "yt": {}, "init": True,
        }
        _COLLECT_ARTICLES_RAW(
            feeds, state, http=transport, parser=parser,
            queue_clock=lambda: TEST_RSS_NOW, max_age_seconds=24 * 3600,
        )
        self.assertEqual(len(state["file"]), 1)
        self.assertEqual(len(state["url_seen"]), 1)
        self.assertEqual(len(state["url_seen"][0]), 32)

    def test_tracking_and_unknown_query_parameters_are_not_discarded(self):
        base = "https://example.org/story?id=7"
        self.assertNotEqual(
            deduplication._canonical_url_fingerprint(base + "&utm_source=rss"),
            deduplication._canonical_url_fingerprint(base + "&utm_source=web"),
        )
        self.assertNotEqual(
            deduplication._canonical_url_fingerprint(base + "&edition=morning"),
            deduplication._canonical_url_fingerprint(base + "&edition=evening"),
        )

    def test_url_shaped_legacy_guid_does_not_hide_a_distinct_article(self):
        feed = "https://example.org/feed"
        legacy_guid = "https://example.org/legacy-guid/item"
        entry = {
            "id": "a-different-stable-guid", "title": "Different article",
            "link": legacy_guid,
            "published_parsed": time.gmtime(TEST_RSS_NOW),
        }
        state = {
            "vus": [legacy_guid], "file": [], "feeds": [feed], "yt": {},
            "init": True,
        }
        state, _, _ = self.collect_entries([entry], state=state)
        self.assertEqual(len(state["file"]), 1)
        self.assertEqual(state["file"][0]["link"], legacy_guid)

    def test_url_index_survives_save_restart_without_rebuilding_from_legacy_guids(self):
        protected_hash = "f" * 32
        state = {
            "v": 2, "vus": [f"https://guid.example/{index}" for index in range(5000)],
            "file": [], "feeds": [], "yt": {},
            "url_seen": [f"{index:032x}" for index in range(4999)] + [protected_hash],
            "init": True,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            persistence.save_state(state, state["vus"], str(path))
            resumed = persistence.load_state(str(path))
            _COLLECT_ARTICLES_RAW(
                [], resumed, queue_clock=lambda: TEST_RSS_NOW,
                max_age_seconds=24 * 3600,
            )
        self.assertIn(protected_hash, resumed["url_seen"])
        self.assertEqual(len(resumed["url_seen"]), settings.MAX_URL_SEEN)

    def test_reordered_feed_still_discovers_a_fresh_entry_before_its_cursor(self):
        feed = "https://example.org/feed"
        entries = [
            {
                "id": "latest", "title": "Already seen",
                "link": "https://example.org/latest",
                "published_parsed": time.gmtime(TEST_RSS_NOW),
            },
            {
                "id": "cursor", "title": "Cursor",
                "link": "https://example.org/cursor",
                "published_parsed": time.gmtime(TEST_RSS_NOW - 3600),
            },
            {
                "id": "reordered", "title": "Reordered but fresh",
                "link": "https://example.org/reordered",
                "published_parsed": time.gmtime(TEST_RSS_NOW - 120),
            },
        ]
        state = {
            "vus": ["latest"], "file": [], "feeds": [feed], "yt": {}, "init": True,
            "feed_health": {feed: {"consecutive_errors": 0, "latest_uid": "cursor"}},
        }
        state, seen, _ = self.collect_entries(entries, state=state)
        self.assertEqual([item["link"] for item in state["file"]], [
            "https://example.org/reordered",
        ])
        self.assertIn("reordered", seen)

    def test_both_publication_buffers_full_leave_candidate_recoverable(self):
        feed = "https://example.org/feed"
        candidate = {
            "id": "not-stored", "title": "Candidate",
            "link": "https://example.org/candidate",
            "published_parsed": time.gmtime(TEST_RSS_NOW),
        }
        state = {
            "vus": [], "file": [{"text": "existing", "link": "https://example.org/old"}],
            "feeds": [feed], "yt": {}, "init": True,
            "feed_health": {feed: {"consecutive_errors": 0, "latest_uid": "prior"}},
            "admission_buffer": [{
                "uid": "buffered", "url_hash": "a" * 32,
                "item": {"text": "buffered", "link": "https://example.org/buffered"},
                "first_seen_at": TEST_RSS_NOW - 1, "admitted_at": TEST_RSS_NOW - 1,
            }],
        }
        with patch.object(admission, "MAX_FILE", 1), patch.object(admission, "MAX_ADMISSION_BUFFER", 1,
        ):
            state, seen, _ = self.collect_entries([candidate], state=state)
        self.assertNotIn("not-stored", seen)
        self.assertEqual(state["feed_health"][feed]["latest_uid"], "prior")
        self.assertEqual(len(state["file"]), 1)
        self.assertEqual(len(state["admission_buffer"]), 1)

    def test_full_date_quarantine_does_not_mark_candidate_or_advance_cursor(self):
        feed = "https://example.org/feed"
        existing = {
            "uid": "held", "url_hash": "b" * 32,
            "item": {"text": "held", "link": "https://example.org/held"},
            "first_seen_at": TEST_RSS_NOW, "reason": "unknown_date",
        }
        entry = {
            "id": "quarantine-overflow", "title": "Unknown",
            "link": "https://example.org/overflow",
        }
        state = {
            "vus": [], "file": [], "feeds": [feed], "yt": {}, "init": True,
            "date_quarantine": [existing],
            "feed_health": {feed: {"consecutive_errors": 0, "latest_uid": "prior"}},
        }
        with patch.object(admission, "MAX_DATE_QUARANTINE", 1):
            state, seen, _ = self.collect_entries([entry], state=state)
        self.assertNotIn("quarantine-overflow", seen)
        self.assertEqual(state["feed_health"][feed]["latest_uid"], "prior")
        self.assertEqual(len(state["date_quarantine"]), 1)

    def test_expired_unknown_date_frees_quarantine_space_before_feed_scan(self):
        feed = "https://example.org/feed"
        expired = {
            "uid": "expired-unknown", "url_hash": "c" * 32,
            "item": {"text": "expired", "link": "https://example.org/expired"},
            "first_seen_at": TEST_RSS_NOW - 24 * 3600,
            "reason": "unknown_date",
        }
        candidate = {
            "id": "new-unknown", "title": "New unknown",
            "link": "https://example.org/new-unknown",
        }
        state = {
            "vus": [], "file": [], "feeds": [feed], "yt": {}, "init": True,
            "date_quarantine": [expired],
        }
        with patch.object(admission, "MAX_DATE_QUARANTINE", 1):
            state, seen, _ = self.collect_entries([candidate], state=state)
        self.assertEqual([record["uid"] for record in state["date_quarantine"]], ["new-unknown"])
        self.assertIn("expired-unknown", seen)
        self.assertNotIn("new-unknown", seen)

    def test_future_candidate_waiting_for_capacity_is_not_rechecked_after_admission(self):
        feed = "https://example.org/feed"
        now = TEST_RSS_NOW
        state = {
            "vus": [], "file": [{"text": "older", "link": "https://example.org/older"}],
            "feeds": [feed], "yt": {}, "init": True,
            "date_quarantine": [{
                "uid": "wait-for-capacity",
                "url_hash": deduplication._canonical_url_fingerprint(
                    "https://example.org/wait-for-capacity",
                ),
                "item": {
                    "text": "Candidate", "link": "https://example.org/wait-for-capacity",
                },
                "first_seen_at": now - 600, "reason": "future_date",
                "published_at": now,
            }],
        }
        with patch.object(admission, "MAX_FILE", 1), patch.object(admission, "MAX_ADMISSION_BUFFER", 0,
        ):
            state, _, _ = self.collect_entries([], state=state, now=now)
            self.assertEqual(state["date_quarantine"][0]["reason"], "admission_deferred")
            state["file"].clear()
            state, seen, _ = self.collect_entries(
                [], state=state, now=now + 48 * 3600,
            )
        self.assertEqual(state["date_quarantine"], [])
        self.assertEqual(len(state["file"]), 1)
        self.assertEqual(state["file"][0]["link"], "https://example.org/wait-for-capacity")
        self.assertIn("wait-for-capacity", seen)

    def test_process_queue_never_publishes_date_quarantine_items(self):
        with tempfile.TemporaryDirectory() as directory:
            sender = Mock(side_effect=AssertionError("quarantaine must not publish"))
            state = {
                "vus": [], "file": [], "feeds": [], "yt": {},
                "date_quarantine": [{
                    "uid": "unknown", "item": {
                        "text": "Unknown date", "link": "https://example.org/unknown",
                    }, "first_seen_at": TEST_RSS_NOW, "reason": "unknown_date",
                }],
            }
            result = publication.process_queue(
                state, {}, "unused", "unused",
                state_path=str(Path(directory) / "state.json"), dry_run=False,
                sender=sender,
            )
        self.assertEqual(result, 0)
        sender.assert_not_called()

    def test_admission_buffer_promotes_fifo_after_existing_file_items(self):
        feed = "https://example.org/feed"
        state = {
            "vus": [], "file": [{"text": "existing", "link": "https://example.org/existing"}],
            "feeds": [feed], "yt": {}, "init": True,
            "admission_buffer": [
                {
                    "uid": name, "url_hash": f"{index:032x}",
                    "item": {"text": name, "link": f"https://example.org/{name}"},
                    "first_seen_at": TEST_RSS_NOW + index,
                    "admitted_at": TEST_RSS_NOW + index,
                }
                for index, name in enumerate(("first", "second"))
            ],
        }
        metrics = {}
        with patch.object(admission, "MAX_FILE", 3):
            _COLLECT_ARTICLES_RAW(
                [feed], state, http=Mock(get=Mock(return_value=Mock(
                    content=b"<rss/>", raise_for_status=Mock(return_value=None),
                ))), parser=Mock(return_value=Mock(entries=[])),
                queue_clock=lambda: TEST_RSS_NOW + 10,
                max_age_seconds=24 * 3600, metrics=metrics,
            )
        self.assertEqual(
            [item["link"] for item in state["file"]],
            [
                "https://example.org/existing",
                "https://example.org/first",
                "https://example.org/second",
            ],
        )
        self.assertEqual(state["file"][1]["queued_at"], TEST_RSS_NOW + 10)
        self.assertEqual(state["file"][1]["first_seen_at"], TEST_RSS_NOW)
        self.assertEqual(state["file"][1]["admitted_at"], TEST_RSS_NOW)
        self.assertEqual(state["admission_buffer"], [])
        self.assertEqual(metrics["articles_promoted_from_admission_buffer"], 2)

    def test_publication_fills_freed_queue_space_from_admission_buffer(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(admission, "MAX_FILE", 1,
        ):
            path = str(Path(directory) / "state.json")
            first = {"text": "first", "link": "https://example.org/first", "pub": ""}
            second = {"text": "second", "link": "https://example.org/second", "pub": ""}
            state = {
                "vus": [], "file": [first], "admission_buffer": [{
                    "uid": "second-guid",
                    "url_hash": deduplication._canonical_url_fingerprint(second["link"]),
                    "item": second, "first_seen_at": 800, "admitted_at": 900,
                }],
                "feeds": [], "yt": {}, "v": 2,
            }
            fake_clock = FakeClock(1000)
            sent_links = []

            def sender(item, token, chat_id):
                sent_links.append(item["link"])
                return True

            metrics = {}
            sent = publication.process_queue(
                state, {}, "token", "chat", state_path=path, dry_run=False,
                sender=sender, sleep=fake_clock.sleep,
                clock=fake_clock, monotonic=fake_clock, max_per_run=2,
                time_limit=400, metrics=metrics,
            )
        self.assertEqual(sent, 2)
        self.assertEqual(sent_links, [first["link"], second["link"]])
        self.assertEqual(state["file"], [])
        self.assertEqual(state["admission_buffer"], [])
        self.assertEqual(metrics["articles_promoted_from_admission_buffer"], 1)

    def test_admission_buffer_survives_restart_and_freshness_expiry(self):
        feed = "https://example.org/feed"
        entry = {
            "id": "buffered-fresh", "title": "Buffered",
            "link": "https://example.org/buffered-fresh",
            "published_parsed": time.gmtime(TEST_RSS_NOW),
        }
        with tempfile.TemporaryDirectory() as directory, patch.object(admission, "MAX_FILE", 1,
        ):
            path = str(Path(directory) / "state.json")
            state = {
                "vus": [], "file": [{"text": "older", "link": "https://example.org/older"}],
                "feeds": [feed], "yt": {}, "init": True,
            }
            response = Mock(content=b"<rss/>")
            response.raise_for_status.return_value = None
            transport = Mock()
            transport.get.return_value = response
            parser = Mock(return_value=Mock(entries=[entry]))
            seen = _COLLECT_ARTICLES_RAW(
                [feed], state, http=transport, parser=parser,
                queue_clock=lambda: TEST_RSS_NOW, max_age_seconds=24 * 3600,
                checkpoint=lambda current, ids: persistence.save_state(current, ids, path),
            )
            self.assertIn("buffered-fresh", seen)
            self.assertEqual(len(state["admission_buffer"]), 1)
            resumed = persistence.load_state(path)
            resumed["file"].clear()  # Simule la confirmation et sauvegarde de l’article précédent.
            _COLLECT_ARTICLES_RAW(
                [feed], resumed, http=transport, parser=Mock(return_value=Mock(entries=[])),
                queue_clock=lambda: TEST_RSS_NOW + 48 * 3600,
                max_age_seconds=24 * 3600,
            )
            self.assertEqual(len(resumed["admission_buffer"]), 0)
            self.assertEqual(len(resumed["file"]), 1)
            promoted = resumed["file"][0]
            self.assertEqual(promoted["link"], entry["link"])
            self.assertEqual(promoted["first_seen_at"], TEST_RSS_NOW)
            self.assertEqual(promoted["admitted_at"], TEST_RSS_NOW)
            self.assertEqual(promoted["queued_at"], TEST_RSS_NOW + 48 * 3600)

    def test_legacy_queued_article_is_never_filtered_by_freshness(self):
        old_item = {
            "text": "Already queued", "link": "https://example.org/old",
            "pub": "2000-01-01",
        }
        state = {
            "vus": [], "file": [old_item.copy()], "feeds": [], "yt": {}, "init": True,
        }
        state, _, _ = self.collect_entries([], state=state, now=TEST_RSS_NOW)
        self.assertEqual(state["file"], [old_item])

    def test_youtube_atom_entry_uses_the_same_publication_freshness_policy(self):
        feed = "https://www.youtube.com/feeds/videos.xml?channel_id=UC123"
        entry = {
            "id": "yt:video:123", "title": "Video",
            "link": "https://www.youtube.com/watch?v=123",
            "published": "2026-11-11T00:00:00+00:00",
            "published_parsed": time.gmtime(TEST_RSS_NOW),
        }
        state, _, _ = self.collect_entries([entry], feed=feed)
        self.assertEqual(len(state["file"]), 1)

    def test_configured_article_freshness_window_is_positive_and_defaults_to_24_hours(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(settings.get_article_max_age_seconds(), 24 * 3600)
        with patch.dict(os.environ, {"ARTICLE_MAX_AGE_HOURS": "12"}, clear=True):
            self.assertEqual(settings.get_article_max_age_seconds(), 12 * 3600)
        for invalid in ("0", "-1", "nope", "12.5"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                settings.get_article_max_age_seconds(invalid)

    def test_synthetic_full_state_checkpoint_measures_serialization_and_memory(self):
        title = "T" * 500
        long_url = "https://example.org/" + ("p" * 150)
        item = {"text": title, "link": long_url, "pub": "2026-11-11"}
        state = {
            "v": 2, "file": [dict(item) for _ in range(1000)],
            "admission_buffer": [{
                "uid": f"guid-{index}", "url_hash": f"{index:032x}",
                "item": dict(item), "first_seen_at": TEST_RSS_NOW,
                "admitted_at": TEST_RSS_NOW,
            } for index in range(1000)],
            "date_quarantine": [{
                "uid": f"quarantine-{index}", "url_hash": f"{index + 1000:032x}",
                "item": dict(item), "first_seen_at": TEST_RSS_NOW,
                "reason": "unknown_date", "published_at": None,
            } for index in range(500)],
            "url_seen": [f"{index:032x}" for index in range(5000)],
            "vus": [f"guid-{index}" for index in range(4755)],
            "feeds": [], "yt": {}, "erreurs": [], "feed_health": {},
            "feed_offset": 0, "last_successful_send_at": None,
            "last_successful_message_id": None, "metrics_history": [
                {"run_started_epoch": TEST_RSS_NOW} for _ in range(12)
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            import tracemalloc
            tracemalloc.start()
            started = time.perf_counter()
            persistence.save_state(state, state["vus"], str(path))
            duration = time.perf_counter() - started
            _, peak_bytes = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            serialized_bytes = path.stat().st_size
            reloaded = persistence.load_state(str(path))
        self.assertGreater(duration, 0)
        self.assertGreater(peak_bytes, 0)
        self.assertGreater(serialized_bytes, 0)
        self.assertLess(serialized_bytes, 4_000_000)
        self.assertEqual(len(reloaded["file"]), 1000)
        self.assertEqual(len(reloaded["admission_buffer"]), 1000)
        self.assertEqual(len(reloaded["date_quarantine"]), 500)
        self.assertEqual(len(reloaded["url_seen"]), 5000)
        self.assertEqual(len(reloaded["metrics_history"]), 12)
        self.assertEqual(len(reloaded["vus"]), 4755)

    def test_state_rejects_invalid_last_successful_send_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({
                "vus": [], "file": [], "last_successful_send_at": "not-a-timestamp",
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "last_successful_send_at"):
                persistence.load_state(str(path))

    def test_telegram_interval_is_configurable_and_defaults_to_30(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(settings.get_telegram_interval_seconds(), 30)
        for interval in (30, 60, 90, 120):
            with self.subTest(interval=interval):
                self.assertEqual(settings.get_telegram_interval_seconds(str(interval)), interval)
                with patch.dict(os.environ, {"TELEGRAM_INTERVAL_SECONDS": str(interval)}):
                    self.assertEqual(settings.get_telegram_interval_seconds(), interval)
        for invalid in ("0", "-1", "abc", "30.5"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                settings.get_telegram_interval_seconds(invalid)

    def test_legacy_active_state_without_init_does_not_silence_new_feed_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({
                "vus": ["old"],
                "file": [{"text": "pending", "link": "https://example.org/pending"}],
                "feeds": ["https://example.org/old-feed"], "v": 1,
            }), encoding="utf-8")
            state = persistence.load_state(str(path))
            feed = "https://example.org/new-feed"
            entries = [
                {"id": "latest", "title": "Latest", "link": "https://example.org/latest"},
                {"id": "archive", "title": "Archive", "link": "https://example.org/archive"},
            ]
            response = Mock(content=b"<rss/>")
            response.raise_for_status.return_value = None
            transport = Mock()
            transport.get.return_value = response
            parser = Mock(return_value=Mock(entries=entries))
            collect_with_fresh_fixtures([feed], state, http=transport, parser=parser)
            self.assertIn("https://example.org/latest", [item["link"] for item in state["file"]])
            self.assertNotIn("https://example.org/archive", [item["link"] for item in state["file"]])

    def test_invalid_article_link_is_not_marked_seen_or_queued(self):
        feed = "https://example.org/feed"
        parser = Mock(return_value=Mock(entries=[
            {"id": "bad-article", "title": "Sans lien", "link": "javascript:alert(1)"},
            {"id": "good-article", "title": "Valide", "link": "https://example.org/article"},
        ]))
        state = {"vus": [], "file": [], "feeds": [feed], "yt": {}}
        response = Mock(content=b"<rss/>")
        response.raise_for_status.return_value = None
        transport = Mock()
        transport.get.return_value = response
        seen = collect_with_fresh_fixtures([feed], state, http=transport, parser=parser)
        transport.get.assert_called_once_with(
            feed, headers={"User-Agent": collection.UA}, timeout=(5, 20),
        )
        self.assertNotIn("bad-article", seen)
        self.assertIn("good-article", seen)
        self.assertEqual(len(state["file"]), 1)

    def test_send_message_does_not_retry_ambiguous_network_failure(self):
        transport = Mock()
        transport.post.side_effect = requests.Timeout("timeout")
        sleeper = Mock()
        with patch("builtins.print") as log:
            self.assertFalse(publication.send_message(
                {"text": "test"}, "fake-token", "fake-chat", http=transport, sleep=sleeper,
            ))
        transport.post.assert_called_once()
        sleeper.assert_not_called()
        self.assertNotIn("fake-token", str(log.call_args_list))

    def test_send_message_retries_only_explicit_telegram_rate_limit(self):
        rate_limit = Mock(ok=False, status_code=429)
        rate_limit.json.return_value = {"parameters": {"retry_after": 2}}
        success = Mock(ok=True, status_code=200)
        transport = Mock()
        transport.post.side_effect = [rate_limit, success]
        sleeper = Mock()
        self.assertTrue(publication.send_message(
            {"text": "test"}, "fake-token", "fake-chat", http=transport, sleep=sleeper,
        ))
        self.assertEqual(transport.post.call_count, 2)
        sleeper.assert_called_once_with(2)

    def test_send_message_logs_utc_duration_and_message_id_without_credentials(self):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {
            "ok": True, "result": {"message_id": 987, "date": 1700000123},
        }
        transport = Mock()
        transport.post.return_value = response
        fake_clock = FakeClock(1700000124)
        with patch("builtins.print") as log:
            result = publication.send_message(
                {"text": "test"}, "sensitive-token", "sensitive-chat",
                http=transport, clock=fake_clock, monotonic=fake_clock,
            )
        self.assertTrue(result)
        self.assertEqual(result.message_id, 987)
        self.assertEqual(result.telegram_date, 1700000123)
        output = " ".join(
            " ".join(str(part) for part in call.args)
            for call in log.call_args_list
        )
        self.assertIn("début_utc", output)
        self.assertIn("durée_s", output)
        self.assertIn("message_id", output)
        self.assertNotIn("sensitive-token", output)
        self.assertNotIn("sensitive-chat", output)

    def test_failed_send_keeps_item_in_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            item = {"text": "test", "link": "https://example.org", "pub": ""}
            state = {"vus": [], "file": [item], "feeds": [], "yt": {}, "v": 2}
            sender = Mock(return_value=False)
            sent = publication.process_queue(
                state, {}, "token", "chat", state_path=path, dry_run=False, sender=sender,
            )
            self.assertEqual(sent, 0)
            self.assertEqual(state["file"], [item])
            saved = json.loads(Path(path).read_text())
            self.assertEqual(saved["file"], [item])

    def test_successful_send_removes_only_confirmed_item(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            first = {"text": "one", "link": "https://example.org/1", "pub": ""}
            second = {"text": "two", "link": "https://example.org/2", "pub": ""}
            state = {"vus": [], "file": [first, second], "feeds": [], "yt": {}, "v": 2}
            sender = Mock(side_effect=[True, False])
            fake_clock = FakeClock(123)
            sent = publication.process_queue(
                state, {}, "token", "chat", state_path=path, dry_run=False,
                sender=sender, interval_seconds=1, sleep=fake_clock.sleep,
                max_per_run=5, clock=fake_clock, monotonic=fake_clock,
            )
            self.assertEqual(sent, 1)
            self.assertEqual(state["file"], [second])
            saved = json.loads(Path(path).read_text())
            self.assertEqual(saved["file"], [second])

    def test_successful_posts_obey_each_configured_interval(self):
        for interval in (30, 60, 90, 120):
            with self.subTest(interval=interval), tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "state.json")
                items = [
                    {"text": str(index), "link": f"https://example.org/{index}", "pub": ""}
                    for index in range(3)
                ]
                state = {"vus": [], "file": items.copy(), "feeds": [], "yt": {}, "v": 2}
                fake_clock = FakeClock(1000)
                send_times = []

                def sender(item, token, chat_id):
                    send_times.append(fake_clock())
                    return True

                sent = publication.process_queue(
                    state, {}, "token", "chat", state_path=path, dry_run=False,
                    sender=sender, interval_seconds=interval,
                    sleep=fake_clock.sleep, clock=fake_clock, monotonic=fake_clock,
                    time_limit=(2 * interval) + settings.MAX_TELEGRAM_SEND_BUDGET_SECONDS,
                )
                self.assertEqual(sent, 3)
                self.assertEqual(
                    [later - earlier for earlier, later in zip(send_times, send_times[1:])],
                    [interval, interval],
                )
                saved = json.loads(Path(path).read_text(encoding="utf-8"))
                self.assertEqual(saved["last_successful_send_at"], 1000 + (2 * interval))
                self.assertEqual(saved["file"], [])

    def test_default_cadence_allows_multiple_posts_within_workflow_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            items = [
                {"text": str(index), "link": f"https://example.org/{index}", "pub": ""}
                for index in range(10)
            ]
            state = {"vus": [], "file": items.copy(), "feeds": [], "yt": {}, "v": 2}
            fake_clock = FakeClock(1000)
            send_times = []

            def sender(item, token, chat_id):
                send_times.append(fake_clock())
                return True

            with patch.dict(os.environ, {}, clear=True):
                sent = publication.process_queue(
                    state, {}, "token", "chat", state_path=path, dry_run=False,
                    sender=sender, sleep=fake_clock.sleep,
                    clock=fake_clock, monotonic=fake_clock,
                    time_limit=settings.RUN_BUDGET,
                )

            self.assertEqual(settings.get_telegram_interval_seconds(), 30)
            self.assertEqual(sent, 10)
            self.assertEqual(len(state["file"]), 0)
            self.assertEqual(
                [later - earlier for earlier, later in zip(send_times, send_times[1:])],
                [30] * 9,
            )
            self.assertLessEqual(fake_clock() - 1000, settings.RUN_BUDGET)

    def test_remaining_interval_is_enforced_across_workflow_executions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            items = [
                {"text": "one", "link": "https://example.org/1", "pub": ""},
                {"text": "two", "link": "https://example.org/2", "pub": ""},
            ]
            state = {"vus": [], "file": items.copy(), "feeds": [], "yt": {}, "v": 2}
            fake_clock = FakeClock(500)
            first = Mock(return_value=True)
            publication.process_queue(
                state, {}, "token", "chat", state_path=path, dry_run=False,
                sender=first, interval_seconds=60, max_per_run=1,
                clock=fake_clock, monotonic=fake_clock,
            )
            persisted = persistence.load_state(path)
            self.assertEqual(persisted["last_successful_send_at"], 500)

            fake_clock.advance(59)
            second_send_times = []

            def second_sender(item, token, chat_id):
                second_send_times.append(fake_clock())
                return True

            sent = publication.process_queue(
                persisted, {}, "token", "chat", state_path=path, dry_run=False,
                sender=second_sender, interval_seconds=60,
                sleep=fake_clock.sleep, clock=fake_clock, monotonic=fake_clock,
                time_limit=60 + settings.MAX_TELEGRAM_SEND_BUDGET_SECONDS,
            )
            self.assertEqual(sent, 1)
            self.assertEqual(second_send_times, [560])
            self.assertEqual(persisted["file"], [])

    def test_interrupted_cadence_wait_resumes_from_persisted_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            item = {"text": "pending", "link": "https://example.org/1", "pub": ""}
            state = {
                "vus": [], "file": [item], "feeds": [], "yt": {}, "v": 2,
                "last_successful_send_at": 100,
            }
            fake_clock = FakeClock(100)

            def interrupt_wait(seconds):
                raise RuntimeError("simulated workflow interruption")

            with self.assertRaisesRegex(RuntimeError, "workflow interruption"):
                publication.process_queue(
                    state, {}, "token", "chat", state_path=path, dry_run=False,
                    sender=Mock(return_value=True), interval_seconds=120,
                    sleep=interrupt_wait, clock=fake_clock, monotonic=fake_clock,
                    time_limit=400,
                )

            resumed = persistence.load_state(path)
            self.assertEqual(resumed["file"], [item])
            self.assertEqual(resumed["last_successful_send_at"], 100)
            fake_clock.advance(120)
            sender = Mock(return_value=True)
            sent = publication.process_queue(
                resumed, {}, "token", "chat", state_path=path, dry_run=False,
                sender=sender, interval_seconds=120,
                sleep=fake_clock.sleep, clock=fake_clock, monotonic=fake_clock,
                time_limit=settings.MAX_TELEGRAM_SEND_BUDGET_SECONDS,
            )
            self.assertEqual(sent, 1)
            sender.assert_called_once()
            self.assertEqual(resumed["file"], [])

    def test_queue_wait_is_skipped_when_next_send_cannot_fit_run_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            item = {"text": "pending", "link": "https://example.org/1", "pub": ""}
            state = {
                "vus": [], "file": [item], "feeds": [], "yt": {}, "v": 2,
                "last_successful_send_at": 2000,
            }
            fake_clock = FakeClock(2000)
            sender = Mock(return_value=True)
            sent = publication.process_queue(
                state, {}, "token", "chat", state_path=path, dry_run=False,
                sender=sender, interval_seconds=120, sleep=fake_clock.sleep,
                clock=fake_clock, monotonic=fake_clock,
                time_limit=120 + settings.MAX_TELEGRAM_SEND_BUDGET_SECONDS - 1,
            )
            self.assertEqual(sent, 0)
            sender.assert_not_called()
            self.assertEqual(fake_clock(), 2000)
            self.assertEqual(state["file"], [item])
            saved = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(saved["file"], [item])

    def test_long_configured_interval_does_not_block_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            item = {"text": "pending", "link": "https://example.org/1", "pub": ""}
            state = {
                "vus": [], "file": [item], "feeds": [], "yt": {}, "v": 2,
                "last_successful_send_at": 3000,
            }
            fake_clock = FakeClock(3000)
            sender = Mock(return_value=True)
            sent = publication.process_queue(
                state, {}, "token", "chat", state_path=path, dry_run=False,
                sender=sender, interval_seconds=600, sleep=fake_clock.sleep,
                clock=fake_clock, monotonic=fake_clock,
            )
            self.assertEqual(sent, 0)
            sender.assert_not_called()
            self.assertEqual(fake_clock(), 3000)
            self.assertEqual(state["file"], [item])

    def test_confirmed_telegram_metadata_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            item = {"text": "one", "link": "https://example.org/1", "pub": ""}
            state = {"vus": [], "file": [item], "feeds": [], "yt": {}, "v": 2}
            fake_clock = FakeClock(1700001000.25)
            result = publication.TelegramSendResult(True, message_id=987, telegram_date=1700001000)
            with patch("builtins.print") as log:
                sent = publication.process_queue(
                    state, {}, "token", "chat", state_path=path, dry_run=False,
                    sender=Mock(return_value=result), interval_seconds=120,
                    clock=fake_clock, monotonic=fake_clock,
                )
            self.assertEqual(sent, 1)
            self.assertEqual(state["last_successful_send_at"], 1700001001)
            self.assertEqual(state["last_successful_message_id"], 987)
            saved = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(saved["last_successful_send_at"], 1700001001)
            self.assertEqual(saved["last_successful_message_id"], 987)
            self.assertIn("prochaine_publication_utc", str(log.call_args_list))


    def test_send_message_marks_permanent_client_errors(self):
        response = Mock(ok=False, status_code=400, text="chat not found")
        transport = Mock()
        transport.post.return_value = response
        with self.assertRaises(publication.PermanentTelegramError):
            publication.send_message(
                {"text": "test"}, "fake-token", "fake-chat", http=transport,
            )
        transport.post.assert_called_once()

    def test_permanent_error_is_quarantined_and_does_not_block_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            bad = {"text": "bad", "link": "https://example.org/bad", "pub": ""}
            good = {"text": "good", "link": "https://example.org/good", "pub": ""}
            state = {"vus": [], "file": [bad, good], "erreurs": [], "feeds": [], "yt": {}, "v": 2}
            def sender(item, token, chat_id):
                if item is bad:
                    raise publication.PermanentTelegramError("HTTP 400: chat not found")
                return True
            sent = publication.process_queue(
                state, {}, "token", "chat", state_path=path, dry_run=False,
                sender=sender, max_per_run=5, clock=lambda: 123,
            )
            self.assertEqual(sent, 1)
            self.assertEqual(state["file"], [])
            self.assertEqual(len(state["erreurs"]), 1)
            self.assertEqual(state["erreurs"][0]["item"], bad)
            self.assertEqual(state["erreurs"][0]["failed_at"], 123)
            saved = json.loads(Path(path).read_text())
            self.assertEqual(saved["erreurs"], state["erreurs"])


    def test_parse_feed_uses_bounded_http_timeout(self):
        response = Mock(content=b"<rss/>")
        response.raise_for_status.return_value = None
        transport = Mock()
        transport.get.return_value = response
        parser = Mock(return_value=Mock(entries=[{"id": "one"}]))
        parsed = collection.parse_feed(
            "https://example.org/feed", http=transport, parser=parser,
        )
        self.assertEqual(parsed.entries, [{"id": "one"}])
        transport.get.assert_called_once_with(
            "https://example.org/feed",
            headers={"User-Agent": collection.UA},
            timeout=(5, 20),
        )
        parser.assert_called_once_with(b"<rss/>")

    def test_parse_feed_raises_on_http_error(self):
        response = Mock()
        response.raise_for_status.side_effect = requests.HTTPError("503")
        transport = Mock()
        transport.get.return_value = response
        with self.assertRaises(requests.HTTPError):
            collection.parse_feed("https://example.org/feed", http=transport)


    def test_full_queue_uses_admission_buffer_without_losing_fresh_articles(self):
        feed = "https://example.org/feed"
        parser = Mock(return_value=Mock(entries=[
            {"id": "new-article", "title": "Nouvel article", "link": "https://example.org/new"},
        ]))
        state = {
            "vus": [], "file": [{"text": "existing", "link": "https://example.org/old"}] * admission.MAX_FILE,
            "feeds": [feed], "yt": {},
        }
        response = Mock(content=b"<rss/>")
        response.raise_for_status.return_value = None
        transport = Mock()
        transport.get.return_value = response
        seen = collect_with_fresh_fixtures([feed], state, http=transport, parser=parser)
        self.assertIn("new-article", seen)
        self.assertEqual(len(state["file"]), admission.MAX_FILE)
        self.assertEqual(len(state["admission_buffer"]), 1)
        self.assertEqual(state["admission_buffer"][0]["uid"], "new-article")
        self.assertEqual(state["admission_buffer"][0]["admitted_at"], TEST_RSS_NOW)

    def test_saturated_queue_cycles_eventually_admit_each_feed_without_duplicates(self):
        feeds = ["https://example.org/a", "https://example.org/b"]
        entries = {
            feeds[0]: [{"id": "a1", "title": "A", "link": "https://example.org/a1"}],
            feeds[1]: [{"id": "b1", "title": "B", "link": "https://example.org/b1"}],
        }
        transport = Mock()
        transport.get.side_effect = lambda url, **kwargs: Mock(
            content=url.encode(), raise_for_status=Mock(return_value=None),
        )
        parser = Mock(side_effect=lambda content: Mock(entries=entries[content.decode()]))
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(admission, "MAX_FILE", 2), \
                patch.object(admission, "MAX_ADMISSION_BUFFER", 0):
            path = str(Path(directory) / "state.json")
            state = {"vus": [], "file": [
                {"text": "existing", "link": "https://example.org/existing"}
            ], "feeds": feeds.copy(), "yt": {}, "init": True, "feed_offset": 0}
            seen = collect_with_fresh_fixtures(
                feeds, state, http=transport, parser=parser,
                checkpoint=lambda current, ids: persistence.save_state(current, ids, path),
            )
            self.assertIn("a1", seen)
            self.assertNotIn("b1", seen)
            restarted = persistence.load_state(path)
            restarted["file"].pop(0)  # L’ancien article est traité pendant ce cycle.
            collect_with_fresh_fixtures(
                feeds, restarted, http=transport, parser=parser,
                checkpoint=lambda current, ids: persistence.save_state(current, ids, path),
            )
            links = [item["link"] for item in restarted["file"]]
            self.assertCountEqual(links, ["https://example.org/a1", "https://example.org/b1"])
            self.assertEqual(len(links), len(set(links)))

    def test_feed_entries_keep_oldest_first_order_and_deduplicate_across_feeds(self):
        feeds = ["https://example.org/a", "https://example.net/b"]
        repeated = {"id": "same", "title": "Shared", "link": "https://example.com/shared"}
        entries = {
            feeds[0]: [
                {"id": "new", "title": "New", "link": "https://example.com/new"},
                repeated,
                {"id": "old", "title": "Old", "link": "https://example.com/old"},
            ],
            feeds[1]: [repeated],
        }
        transport = Mock()
        transport.get.side_effect = lambda url, **kwargs: Mock(
            content=url.encode(), raise_for_status=Mock(return_value=None),
        )
        parser = Mock(side_effect=lambda content: Mock(entries=entries[content.decode()]))
        state = {"vus": [], "file": [], "feeds": feeds.copy(), "yt": {}, "init": True}
        collect_with_fresh_fixtures(feeds, state, http=transport, parser=parser)
        self.assertEqual(
            [item["link"] for item in state["file"]],
            ["https://example.com/old", "https://example.com/shared", "https://example.com/new"],
        )

    def test_feed_cursor_prevents_republishing_entries_evicted_from_global_seen_window(self):
        feed = "https://example.org/feed"
        stale = time.gmtime(TEST_RSS_NOW - 48 * 3600)
        entries = [
            {"id": "latest", "title": "Latest", "link": "https://example.org/latest",
             "published_parsed": time.gmtime(TEST_RSS_NOW)},
            {"id": "older-2", "title": "Older 2", "link": "https://example.org/older-2",
             "published_parsed": stale},
            {"id": "older-1", "title": "Older 1", "link": "https://example.org/older-1",
             "published_parsed": stale},
        ]
        response = Mock(content=b"<rss/>")
        response.raise_for_status.return_value = None
        transport = Mock()
        transport.get.return_value = response
        parser = Mock(return_value=Mock(entries=entries))
        state = {
            "vus": [], "file": [], "feeds": [feed], "yt": {}, "init": True,
            "feed_health": {feed: {"consecutive_errors": 0, "latest_uid": "latest"}},
        }
        collect_with_fresh_fixtures([feed], state, http=transport, parser=parser)
        self.assertEqual(state["file"], [])
        self.assertEqual(state["feed_health"][feed]["latest_uid"], "latest")




    def test_feed_order_rotates_between_runs_and_deferrals_are_summarized(self):
        feeds = ["https://example.org/a", "https://example.org/b"]
        state = {
            "vus": [],
            "file": [{"text": "existing", "link": "https://example.org/old"}],
            "feeds": feeds.copy(), "yt": {}, "feed_offset": 0,
        }
        response = Mock(content=b"<rss/>")
        response.raise_for_status.return_value = None
        transport = Mock()
        transport.get.return_value = response
        parser = Mock(return_value=Mock(entries=[
            {"id": "new", "title": "New", "link": "https://example.org/new"},
        ]))
        with patch.object(admission, "MAX_FILE", 1), \
                patch.object(admission, "MAX_ADMISSION_BUFFER", 0), \
                patch("builtins.print") as log:
            first_seen = collect_with_fresh_fixtures(feeds, state, http=transport, parser=parser)
            second_seen = collect_with_fresh_fixtures(feeds, state, http=transport, parser=parser)
        requested = [call.args[0] for call in transport.get.call_args_list]
        self.assertEqual(requested, [feeds[0], feeds[1], feeds[1], feeds[0]])
        self.assertNotIn("new", first_seen)
        self.assertNotIn("new", second_seen)
        self.assertEqual(state["feed_offset"], 0)
        summaries = [
            call for call in log.call_args_list
            if call.args and isinstance(call.args[0], str)
            and "Collecte RSS" in call.args[0]
        ]
        self.assertEqual(len(summaries), 2)
        summary_text = [" ".join(str(part) for part in call.args) for call in summaries]
        self.assertTrue(all("reportés: 2" in summary for summary in summary_text))


    def test_global_telegram_errors_preserve_entire_queue(self):
        for status in (401, 403):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "state.json")
                items = [
                    {"text": "one", "link": "https://example.org/1", "pub": ""},
                    {"text": "two", "link": "https://example.org/2", "pub": ""},
                ]
                state = {"vus": [], "file": items.copy(), "erreurs": [], "feeds": [], "yt": {}, "v": 2}
                response = Mock(ok=False, status_code=status, text="global Telegram error")
                transport = Mock()
                transport.post.return_value = response
                metrics = {}
                def sender(item, token, chat):
                    return publication.send_message(item, token, chat, http=transport)
                sent = publication.process_queue(
                    state, {}, "token", "chat", state_path=path, dry_run=False,
                    sender=sender, metrics=metrics,
                )
                self.assertEqual(sent, 0)
                self.assertEqual(state["file"], items)
                self.assertEqual(state["erreurs"], [])
                self.assertEqual(metrics["telegram_send_operations"], 1)
                self.assertEqual(metrics["telegram_attempts"], 1)
                saved = json.loads(Path(path).read_text())
                self.assertEqual(saved["file"], items)
                self.assertEqual(saved["erreurs"], [])

    def test_feed_offset_is_persisted_between_real_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            feeds = ["https://example.org/a", "https://example.org/b"]
            state = {
                "vus": [], "file": [], "feeds": feeds.copy(), "yt": {},
                "erreurs": [], "v": 2, "feed_offset": 0,
            }
            response = Mock(content=b"<rss/>")
            response.raise_for_status.return_value = None
            transport = Mock()
            transport.get.return_value = response
            parser = Mock(return_value=Mock(entries=[]))

            for expected_offset in (1, 0, 1):
                seen = collect_with_fresh_fixtures(feeds, state, http=transport, parser=parser)
                publication.process_queue(
                    state, seen, "token", "chat", state_path=path, dry_run=False,
                    sender=Mock(return_value=False),
                )
                saved = json.loads(Path(path).read_text())
                self.assertEqual(saved["feed_offset"], expected_offset)
                state = persistence.load_state(path)

    def test_interrupted_collection_resumes_from_checkpointed_next_feed(self):
        feeds = ["https://example.org/a", "https://example.org/b", "https://example.org/c"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = {"vus": [], "file": [], "feeds": feeds.copy(), "yt": {},
                     "init": True, "feed_offset": 0}
            response = Mock(content=b"<rss/>")
            response.raise_for_status.return_value = None
            transport = Mock()
            transport.get.return_value = response
            parser = Mock(return_value=Mock(entries=[]))

            def save_then_interrupt(current, seen):
                persistence.save_state(current, seen, str(path))
                raise RuntimeError("simulated interruption")

            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                collect_with_fresh_fixtures(
                    feeds, state, http=transport, parser=parser,
                    checkpoint=save_then_interrupt, checkpoint_every=1,
                )
            resumed = persistence.load_state(path)
            self.assertEqual(resumed["feed_offset"], 1)
            transport.get.reset_mock()
            collect_with_fresh_fixtures(
                feeds, resumed, http=transport, parser=parser,
                checkpoint=lambda current, seen: persistence.save_state(current, seen, str(path)),
            )
            self.assertEqual(
                [call.args[0] for call in transport.get.call_args_list],
                feeds[1:] + feeds[:1],
            )

    def test_collection_checkpoints_in_bounded_batches_and_at_completion(self):
        feeds = [f"https://example.org/{index}" for index in range(11)]
        state = {"vus": [], "file": [], "feeds": feeds.copy(), "yt": {}, "init": True}
        response = Mock(content=b"<rss/>")
        response.raise_for_status.return_value = None
        transport = Mock()
        transport.get.return_value = response
        parser = Mock(return_value=Mock(entries=[]))
        cursors = []
        collect_with_fresh_fixtures(
            feeds, state, http=transport, parser=parser,
            checkpoint=lambda current, seen: cursors.append(current["feed_offset"]),
        )
        self.assertEqual(cursors, [5, 10, 1])

    def test_collection_budget_persists_cursor_for_unvisited_feeds(self):
        feeds = ["https://example.org/a", "https://example.org/b", "https://example.org/c"]
        state = {"vus": [], "file": [], "feeds": feeds.copy(), "yt": {}, "init": True}
        response = Mock(content=b"<rss/>")
        response.raise_for_status.return_value = None
        transport = Mock()
        transport.get.return_value = response
        parser = Mock(return_value=Mock(entries=[]))

        class StepClock:
            def __init__(self):
                self.value = -1
            def __call__(self):
                self.value += 1
                return float(self.value)

        collect_with_fresh_fixtures(
            feeds, state, http=transport, parser=parser, budget=3,
            clock=StepClock(), checkpoint=lambda current, seen: None,
        )
        self.assertEqual(transport.get.call_count, 1)
        self.assertEqual(state["feed_offset"], 1)

    def test_later_added_feed_baselines_archive_but_defers_its_latest_item(self):
        feed = "https://example.org/new-feed?secret=should-not-be-logged"
        entries = [
            {"id": "latest", "title": "Latest", "link": "https://example.org/3"},
            {"id": "archive-2", "title": "Old 2", "link": "https://example.org/2"},
            {"id": "archive-1", "title": "Old 1", "link": "https://example.org/1"},
        ]
        state = {"vus": [], "file": [
            {"text": "occupied", "link": "https://example.org/occupied"}
        ] * admission.MAX_FILE, "feeds": [], "yt": {}, "init": True}
        response = Mock(content=b"<rss/>")
        response.raise_for_status.return_value = None
        transport = Mock()
        transport.get.return_value = response
        parser = Mock(return_value=Mock(entries=entries))

        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.json")
            seen = collect_with_fresh_fixtures(
                [feed], state, http=transport, parser=parser,
                checkpoint=lambda current, ids: persistence.save_state(current, ids, state_path),
            )
            self.assertIn(feed, state["feeds"])
            self.assertIn("archive-1", seen)
            self.assertIn("archive-2", seen)
            self.assertIn("latest", seen)
            self.assertEqual(len(state["file"]), admission.MAX_FILE)
            self.assertEqual(
                [record["uid"] for record in state["admission_buffer"]],
                ["latest"],
            )

            restarted = persistence.load_state(state_path)
            restarted["file"].pop()
            collect_with_fresh_fixtures(
                [feed], restarted, http=transport, parser=parser,
                checkpoint=lambda current, ids: persistence.save_state(current, ids, state_path),
            )
            self.assertIn("https://example.org/3", [item["link"] for item in restarted["file"]])
            self.assertEqual(sum(item["link"].endswith("/1") for item in restarted["file"]), 0)

    def test_first_initialization_remains_silent_even_with_a_full_queue(self):
        feed = "https://example.org/feed"
        entry = {"id": "initial", "title": "Initial", "link": "https://example.org/1"}
        state = {"vus": [], "file": [{"text": "x", "link": "https://example.org/x"}] * admission.MAX_FILE,
                 "feeds": [], "yt": {}, "init": False}
        response = Mock(content=b"<rss/>")
        response.raise_for_status.return_value = None
        transport = Mock()
        transport.get.return_value = response
        parser = Mock(return_value=Mock(entries=[entry]))
        seen = collect_with_fresh_fixtures([feed], state, http=transport, parser=parser)
        self.assertIn("initial", seen)
        self.assertEqual(len(state["file"]), admission.MAX_FILE)
        self.assertTrue(state["init"])

    def test_feed_errors_are_tracked_without_blocking_other_feeds_or_logging_query(self):
        feeds = ["https://example.org/bad?secret=do-not-print", "https://example.net/good"]
        response = Mock(content=b"<rss/>")
        response.raise_for_status.return_value = None
        transport = Mock()
        transport.get.side_effect = lambda url, **kwargs: (
            Mock(raise_for_status=Mock(side_effect=requests.Timeout("timeout")))
            if url == feeds[0] else response
        )
        parser = Mock(return_value=Mock(entries=[]))
        state = {"vus": [], "file": [], "feeds": feeds.copy(), "yt": {}, "init": True}
        with patch("builtins.print") as log:
            for _ in range(3):
                collect_with_fresh_fixtures(feeds, state, http=transport, parser=parser)
        self.assertEqual(state["feed_health"][feeds[0]]["consecutive_errors"], 3)
        self.assertTrue(any("défaillances récurrentes" in str(c) for c in log.call_args_list))
        self.assertNotIn("do-not-print", " ".join(str(c) for c in log.call_args_list))
        self.assertEqual(transport.get.call_count, 6)

    def test_http_feed_errors_log_status_and_category_without_url(self):
        feed = "https://example.org/rss?token=private-value"
        response = Mock(headers={"Retry-After": "17"})
        error = requests.HTTPError(
            "HTTP 429 for https://example.org/rss?token=private-value",
            response=Mock(status_code=429, headers={"Retry-After": "17"}),
        )
        response.raise_for_status.side_effect = error
        transport = Mock()
        transport.get.return_value = response
        state = {"vus": [], "file": [], "feeds": [feed], "yt": {}, "init": True}

        with patch("builtins.print") as log:
            collect_with_fresh_fixtures([feed], state, http=transport, parser=Mock())

        output = " ".join(
            " ".join(str(part) for part in call.args)
            for call in log.call_args_list
        )
        self.assertIn("HTTP: 429", output)
        self.assertIn("catégorie: rate_limited", output)
        self.assertIn("retry_after_s: 17", output)
        self.assertNotIn("private-value", output)
        self.assertNotIn("/rss?", output)
        self.assertEqual(state["feed_health"][feed]["last_http_status"], 429)
        self.assertEqual(state["feed_health"][feed]["last_error_category"], "rate_limited")

    def test_send_accepted_before_state_save_is_retried_after_restart_at_least_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            item = {"text": "test", "link": "https://example.org", "pub": ""}
            state = {"vus": [], "file": [item], "erreurs": [], "feeds": [], "yt": {}, "v": 2}
            real_save = persistence.save_state
            calls = {"count": 0}

            def fail_after_send(current, seen, state_path):
                calls["count"] += 1
                if calls["count"] == 1:
                    return real_save(current, seen, state_path)
                raise OSError("simulated crash before post-send checkpoint")

            accepted = Mock(return_value=True)
            with patch.object(persistence, "save_state", side_effect=fail_after_send), \
                 patch("builtins.print") as log:
                with self.assertRaisesRegex(OSError, "simulated crash"):
                    publication.process_queue(
                        state, {}, "token", "chat", state_path=path, dry_run=False,
                        sender=accepted,
                    )
            self.assertIn("doublon_possible_après_reprise: oui", str(log.call_args_list))
            persisted = persistence.load_state(path)
            self.assertEqual(persisted["file"], [item])
            sender_after_restart = Mock(return_value=True)
            publication.process_queue(
                persisted, {}, "token", "chat", state_path=path, dry_run=False,
                sender=sender_after_restart,
            )
            accepted.assert_called_once()
            sender_after_restart.assert_called_once()


if __name__ == "__main__":
    unittest.main()
