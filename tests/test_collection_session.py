"""Ressources RSS, reprise et isolation du transport Telegram, sans réseau."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, PropertyMock, patch

import requests

from newsbot import collection, persistence, publication

NOW = 1_800_000_000
FEEDS = [f"https://example.invalid/feed/{i}" for i in range(3)]


def state():
    return {"init": True, "feeds": FEEDS.copy(), "file": [], "vus": []}


def entry(uid):
    return {"id": uid, "title": uid, "link": f"https://example.invalid/{uid}",
            "published_parsed": time.gmtime(NOW - 60)}


class CollectionSessionTests(unittest.TestCase):
    def collect(self, current, **options):
        with redirect_stdout(io.StringIO()):
            return collection.collect_articles(
                FEEDS, current, queue_clock=lambda: NOW, **options,
            )

    def test_one_owned_session_reuses_transport_without_retaining_feed_cookies(self):
        session = requests.Session()
        responses = []

        def get(url, **kwargs):
            self.assertFalse(session.cookies)
            session.cookies.set("synthetic", "feed-specific")
            response = Mock(content=url.encode())
            responses.append(response)
            return response

        def parse(content):
            responses[-1].close.assert_called_once_with()
            return Mock(entries=[entry(content.decode().rsplit("/", 1)[-1])])

        with patch.object(collection.requests, "Session", return_value=session) as factory, \
                patch.object(session, "get", side_effect=get) as fetch, \
                patch.object(session, "close", wraps=session.close) as close:
            current = state()
            self.collect(current, parser=parse)
        factory.assert_called_once_with()
        close.assert_called_once_with()
        self.assertEqual([call.args[0] for call in fetch.call_args_list], FEEDS)
        for call in fetch.call_args_list:
            self.assertEqual(call.kwargs, {"headers": {"User-Agent": collection.UA},
                                           "timeout": (5, 20)})
        self.assertEqual([item["link"] for item in current["file"]],
                         [f"https://example.invalid/{i}" for i in range(3)])
        self.assertEqual(session.get_adapter("https://").max_retries.total, 0)
        self.assertFalse(session.get_adapter("https://").max_retries.read)

    def test_injected_transport_is_used_without_creating_or_closing_a_session(self):
        response = Mock(content=b"rss")
        transport = Mock(get=Mock(return_value=response))
        with patch.object(collection.requests, "Session") as factory:
            self.collect(state(), http=transport, parser=Mock(return_value=Mock(entries=[])))
        factory.assert_not_called()
        transport.close.assert_not_called()
        transport.cookies.clear.assert_not_called()
        self.assertEqual(response.close.call_count, 3)

    def test_response_is_closed_on_http_or_parser_error_and_timeout_is_preserved(self):
        for failure in ("http", "body", "parser"):
            with self.subTest(failure=failure):
                response = Mock(content=b"rss")
                parser = Mock(return_value=Mock(entries=[]))
                if failure == "http":
                    response.raise_for_status.side_effect = requests.HTTPError("synthetic")
                elif failure == "body":
                    type(response).content = PropertyMock(side_effect=requests.ConnectionError("synthetic"))
                else:
                    parser.side_effect = ValueError("synthetic")
                transport = Mock(get=Mock(return_value=response))
                with self.assertRaises((requests.RequestException, ValueError)):
                    collection.parse_feed(FEEDS[0], http=transport, parser=parser,
                                          timeout=(2, 7))
                response.close.assert_called_once_with()
                self.assertEqual(transport.get.call_args.kwargs["timeout"], (2, 7))
                if failure != "parser":
                    parser.assert_not_called()

    def test_session_closes_after_budget_stop_or_caught_parser_errors(self):
        for stage in ("budget", "parser"):
            with self.subTest(stage=stage):
                session = requests.Session()
                response = Mock(content=b"rss")
                metrics = {}
                with patch.object(collection.requests, "Session", return_value=session), \
                        patch.object(session, "get", return_value=response) as fetch, \
                        patch.object(session, "close", wraps=session.close) as close:
                    self.collect(state(), parser=Mock(side_effect=ValueError("synthetic")),
                                 budget=0 if stage == "budget" else 180, metrics=metrics)
                close.assert_called_once_with()
                self.assertEqual(fetch.call_count, 0 if stage == "budget" else 3)
                self.assertEqual(metrics["rss_errors"], 0 if stage == "budget" else 3)

    def test_owned_session_closes_on_admission_error_or_checkpoint_interruption(self):
        for stage in ("admission", "checkpoint"):
            with self.subTest(stage=stage):
                session = requests.Session()
                response = Mock(content=b"rss")
                with patch.object(collection.requests, "Session", return_value=session), \
                        patch.object(session, "get", return_value=response), \
                        patch.object(session, "close", wraps=session.close) as close, \
                        patch.object(collection, "admit_feed_entries") as admit:
                    if stage == "admission":
                        admit.side_effect = RuntimeError("synthetic interruption")
                    else:
                        admit.return_value = (0, 0, 0)
                    checkpoint = Mock(side_effect=RuntimeError("synthetic interruption"))
                    with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
                        self.collect(state(), parser=Mock(return_value=Mock(entries=[])),
                                     checkpoint=checkpoint, checkpoint_every=1)
                close.assert_called_once_with()
                response.close.assert_called_once_with()

    def test_network_error_checkpoint_preserves_fifo_and_resumes_next_feed(self):
        for failure in (requests.ConnectTimeout("synthetic"),
                        requests.ConnectionError("synthetic")):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.json"
                current = state()
                current["file"] = [{"text": "pending", "link": "https://example.invalid/pending"}]
                session = requests.Session()
                response = Mock(content=b"rss")

                def save_then_interrupt(current, seen):
                    persistence.save_state(current, seen, path)
                    if current["feed_offset"] == 2:
                        raise InterruptedError("after error checkpoint")

                with patch.object(collection.requests, "Session", return_value=session), \
                        patch.object(session, "get", side_effect=[response, failure]) as fetch, \
                        patch.object(session, "close", wraps=session.close) as close:
                    with self.assertRaises(InterruptedError):
                        self.collect(current, parser=Mock(return_value=Mock(entries=[entry("a")])),
                                     checkpoint=save_then_interrupt, checkpoint_every=1)
                close.assert_called_once_with()
                self.assertEqual(fetch.call_count, 2)  # aucun réessai
                resumed = persistence.load_state(path)
                self.assertEqual(resumed["feed_offset"], 2)
                self.assertEqual(resumed["feed_health"][FEEDS[0]]["latest_uid"], "a")
                self.assertEqual([row["link"] for row in resumed["file"]],
                                 ["https://example.invalid/pending", "https://example.invalid/a"])
                saved_before_resume = path.read_bytes()
                transport = Mock(get=Mock(return_value=Mock(content=b"rss")))
                self.collect(resumed, http=transport, parser=Mock(return_value=Mock(entries=[])))
                self.assertEqual([call.args[0] for call in transport.get.call_args_list],
                                 FEEDS[2:] + FEEDS[:2])
                self.assertEqual(path.read_bytes(), saved_before_resume)

    def test_pooling_keeps_rotation_oldest_first_order_deduplication_and_admission(self):
        current = state()
        current["feed_offset"] = 1
        entries = [[entry("new"), entry("shared"), entry("old")],
                   [entry("shared"), entry("other")], []]
        parser = Mock(side_effect=[Mock(entries=rows) for rows in entries])
        session = requests.Session()
        metrics = {}
        with patch.object(collection.requests, "Session", return_value=session), \
                patch.object(session, "get", return_value=Mock(content=b"rss")) as fetch:
            seen = self.collect(current, parser=parser, metrics=metrics)
        self.assertEqual([call.args[0] for call in fetch.call_args_list], FEEDS[1:] + FEEDS[:1])
        self.assertEqual([row["link"].rsplit("/", 1)[-1] for row in current["file"]],
                         ["old", "shared", "new", "other"])
        self.assertEqual(set(seen), {"old", "shared", "new", "other"})
        self.assertEqual(metrics["articles_admitted"], 4)
        self.assertEqual(metrics["articles_skipped_by_seen_id"], 1)
        self.assertEqual(current["feed_offset"], 2)

    def test_phase_timings_are_separate_and_do_not_log_sensitive_url(self):
        transport = Mock(get=Mock(return_value=Mock(content=b"rss")))
        timings = {}
        collection.parse_feed(FEEDS[0], http=transport, parser=Mock(), timings=timings,
                              timing_clock=iter([0, 2, 2, 2.5]).__next__)
        self.assertEqual(timings, {"network": 2, "parsing": 0.5})
        output = io.StringIO()
        with redirect_stdout(output):
            collection._log_feed_timings("https://example.invalid/rss?token=private", timings, 0.25)
        self.assertNotIn("private", output.getvalue())
        self.assertNotIn("token", output.getvalue())
        self.assertIn("admission_s: 0.25", output.getvalue())

    def test_telegram_still_uses_its_own_post_transport_after_rss_session_closes(self):
        session = requests.Session()
        with patch.object(collection.requests, "Session", return_value=session) as factory, \
                patch.object(session, "get", return_value=Mock(content=b"rss")), \
                patch.object(session, "close", wraps=session.close) as close, \
                patch.object(session, "post") as rss_post, \
                patch.object(publication.requests, "post", return_value=Mock(
                    ok=True, status_code=200, json=Mock(return_value={"result": {"message_id": 1}}),
                )) as telegram_post, redirect_stdout(io.StringIO()):
            self.collect(state(), parser=Mock(return_value=Mock(entries=[])))
            close.assert_called_once_with()
            result = publication.send_message({"text": "synthetic"}, "synthetic", "synthetic")
        self.assertTrue(result.success)
        factory.assert_called_once_with()
        rss_post.assert_not_called()
        telegram_post.assert_called_once()
