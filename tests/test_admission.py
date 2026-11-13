"""Règles d'admission exercées directement, sans collecte ni transport."""
from collections import defaultdict
import time
import unittest
from unittest.mock import patch

from newsbot import admission, deduplication


NOW = 1_800_000_000
FEED = "https://example.org/feed"


def run_counts():
    counts = defaultdict(int)
    counts["age_distribution"] = defaultdict(int)
    return counts


def empty_state():
    return {
        "vus": [], "file": [], "admission_buffer": [],
        "date_quarantine": [], "url_seen": [], "feeds": [FEED],
        "feed_health": {FEED: {"latest_uid": "prior"}},
    }


class AdmissionRulesTests(unittest.TestCase):
    def test_entries_keep_order_deduplicate_and_defer_without_advancing_cursor(self):
        state = empty_state()
        entries = [
            {"id": uid, "link": link, "published_parsed": time.gmtime(NOW)}
            for uid, link in [
                ("overflow", "https://example.org/overflow"),
                ("buffered", "https://example.org/buffered"),
                ("second-id", "https://example.org/first#section"),
                ("first", "https://example.org/first"),
            ]
        ]
        seen, urls, active_urls = deduplication.prepare_seen_indexes(state)
        counts = run_counts()
        with patch.object(admission, "MAX_FILE", 1), \
                patch.object(admission, "MAX_ADMISSION_BUFFER", 1):
            maximum, added, deferred = admission.admit_feed_entries(
                entries, state, FEED, was_initialized=True, seen=seen,
                url_seen=urls, active_url_seen=active_urls, counts=counts,
                now_epoch=NOW, queue_clock=lambda: NOW,
                max_age_seconds=24 * 3600, queue_max=0,
            )
        self.assertEqual(state["file"][0]["link"], "https://example.org/first")
        self.assertEqual(state["admission_buffer"][0]["uid"], "buffered")
        self.assertEqual(list(seen), ["first", "second-id", "buffered"])
        self.assertEqual(state["feed_health"][FEED]["latest_uid"], "buffered")
        self.assertEqual((maximum, added, deferred), (1, 1, 1))
        self.assertEqual(counts["duplicate_id"], 1)

    def test_already_admitted_article_keeps_eligibility_after_age_limit(self):
        state = empty_state()
        old_admission = NOW - 2 * 86400
        state["admission_buffer"] = [{
            "uid": "older", "item": {"link": "https://example.org/older"},
            "first_seen_at": old_admission, "admitted_at": old_admission,
        }]
        state["date_quarantine"] = [{
            "uid": "admitted", "item": {"link": "https://example.org/admitted"},
            "first_seen_at": old_admission, "admitted_at": old_admission,
            "reason": "admission_deferred",
        }, {
            "uid": "expired", "item": {"link": "https://example.org/expired"},
            "first_seen_at": NOW - 86400, "reason": "unknown_date",
        }]
        seen, urls, active_urls = deduplication.prepare_seen_indexes(state)
        counts = run_counts()
        with patch.object(admission, "MAX_FILE", 1), \
                patch.object(admission, "MAX_ADMISSION_BUFFER", 1):
            maximum = admission.prepare_pending_articles(
                state, seen=seen, url_seen=urls, active_url_seen=active_urls,
                counts=counts, now_epoch=NOW, queue_clock=lambda: NOW,
                max_age_seconds=86400, queue_max=0,
            )
        self.assertEqual(maximum, 1)
        self.assertEqual(state["file"][0]["link"], "https://example.org/older")
        self.assertEqual(state["admission_buffer"][0]["uid"], "admitted")
        self.assertEqual(state["admission_buffer"][0]["admitted_at"], old_admission)
        self.assertEqual(state["date_quarantine"], [])
        self.assertEqual(counts["quarantine_expired"], 1)
        self.assertEqual(counts["quarantine_released"], 1)

    def test_due_future_date_can_be_released_from_saved_metadata(self):
        state = empty_state()
        state["date_quarantine"] = [{
            "uid": "future", "item": {"link": "https://example.org/future"},
            "first_seen_at": NOW - 60, "reason": "future_date",
            "published_at": NOW - 1,
        }]
        seen, urls, active_urls = deduplication.prepare_seen_indexes(state)
        counts = run_counts()
        maximum = admission.release_due_future_articles(
            state, seen=seen, url_seen=urls, active_url_seen=active_urls,
            counts=counts, now_epoch=NOW, queue_clock=lambda: NOW,
            max_age_seconds=86400, queue_max=0,
        )
        self.assertEqual(maximum, 1)
        self.assertEqual(state["date_quarantine"], [])
        self.assertEqual(state["file"][0]["first_seen_at"], NOW - 60)
        self.assertEqual(state["file"][0]["admitted_at"], NOW)
        self.assertEqual(counts["quarantine_released"], 1)


if __name__ == "__main__":
    unittest.main()
