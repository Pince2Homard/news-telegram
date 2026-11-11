import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import poster


class PosterSafetyTests(unittest.TestCase):
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
        item = poster.format_item({
            "title": "<script>alert(1)</script>",
            "link": "https://example.org/?a=1&b=2",
        })
        self.assertIn("&lt;script&gt;", item["text"])
        self.assertIn("&amp;", item["text"])
        self.assertIsNone(poster.format_item({"title": "bad", "link": "javascript:alert(1)"}))
        self.assertIsNone(poster.format_item({"title": "bad", "link": ""}))

    def test_dry_run_never_calls_sender_or_writes_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.json")
            state = {"vus": [], "file": [{"text": "test", "link": "https://example.org"}]}
            sender = Mock(side_effect=AssertionError("Telegram ne doit jamais être appelé"))
            sent = poster.process_queue(
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
            poster.save_state(state, {"one": None, "two": None}, str(path))
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["vus"], ["one", "two"])
            self.assertFalse(list(Path(directory).glob("*.tmp")))

    def test_corrupt_state_is_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                poster.load_state(str(path))

    def test_state_migration_preserves_pending_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            pending = [{"text": "<b>à publier</b>", "link": "https://example.org", "pub": ""}]
            path.write_text(json.dumps({"vus": ["old"], "file": pending, "feeds": ["feed"], "v": 1}))
            migrated = poster.load_state(str(path))
            self.assertEqual(migrated["v"], 2)
            self.assertEqual(migrated["file"], pending)
            self.assertEqual(migrated["feeds"], ["feed"])

    def test_legacy_active_state_without_init_does_not_silence_new_feed_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({
                "vus": ["old"],
                "file": [{"text": "pending", "link": "https://example.org/pending"}],
                "feeds": ["https://example.org/old-feed"], "v": 1,
            }), encoding="utf-8")
            state = poster.load_state(str(path))
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
            poster.collect_articles([feed], state, http=transport, parser=parser)
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
        seen = poster.collect_articles([feed], state, http=transport, parser=parser)
        transport.get.assert_called_once_with(
            feed, headers={"User-Agent": poster.UA}, timeout=(5, 20),
        )
        self.assertNotIn("bad-article", seen)
        self.assertIn("good-article", seen)
        self.assertEqual(len(state["file"]), 1)

    def test_send_message_does_not_retry_ambiguous_network_failure(self):
        transport = Mock()
        transport.post.side_effect = requests.Timeout("timeout")
        sleeper = Mock()
        with patch("builtins.print") as log:
            self.assertFalse(poster.send_message(
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
        self.assertTrue(poster.send_message(
            {"text": "test"}, "fake-token", "fake-chat", http=transport, sleep=sleeper,
        ))
        self.assertEqual(transport.post.call_count, 2)
        sleeper.assert_called_once_with(2)

    def test_failed_send_keeps_item_in_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            item = {"text": "test", "link": "https://example.org", "pub": ""}
            state = {"vus": [], "file": [item], "feeds": [], "yt": {}, "v": 2}
            sender = Mock(return_value=False)
            sent = poster.process_queue(
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
            sent = poster.process_queue(
                state, {}, "token", "chat", state_path=path, dry_run=False,
                sender=sender, delay=0, max_per_run=5,
            )
            self.assertEqual(sent, 1)
            self.assertEqual(state["file"], [second])
            saved = json.loads(Path(path).read_text())
            self.assertEqual(saved["file"], [second])


    def test_send_message_marks_permanent_client_errors(self):
        response = Mock(ok=False, status_code=400, text="chat not found")
        transport = Mock()
        transport.post.return_value = response
        with self.assertRaises(poster.PermanentTelegramError):
            poster.send_message(
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
                    raise poster.PermanentTelegramError("HTTP 400: chat not found")
                return True
            sent = poster.process_queue(
                state, {}, "token", "chat", state_path=path, dry_run=False,
                sender=sender, delay=0, max_per_run=5, clock=lambda: 123,
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
        parsed = poster.parse_feed(
            "https://example.org/feed", http=transport, parser=parser,
        )
        self.assertEqual(parsed.entries, [{"id": "one"}])
        transport.get.assert_called_once_with(
            "https://example.org/feed",
            headers={"User-Agent": poster.UA},
            timeout=(5, 20),
        )
        parser.assert_called_once_with(b"<rss/>")

    def test_parse_feed_raises_on_http_error(self):
        response = Mock()
        response.raise_for_status.side_effect = requests.HTTPError("503")
        transport = Mock()
        transport.get.return_value = response
        with self.assertRaises(requests.HTTPError):
            poster.parse_feed("https://example.org/feed", http=transport)


    def test_queue_limit_defers_articles_without_marking_them_seen(self):
        feed = "https://example.org/feed"
        parser = Mock(return_value=Mock(entries=[
            {"id": "new-article", "title": "Nouvel article", "link": "https://example.org/new"},
        ]))
        state = {
            "vus": [], "file": [{"text": "existing", "link": "https://example.org/old"}] * poster.MAX_FILE,
            "feeds": [feed], "yt": {},
        }
        response = Mock(content=b"<rss/>")
        response.raise_for_status.return_value = None
        transport = Mock()
        transport.get.return_value = response
        seen = poster.collect_articles([feed], state, http=transport, parser=parser)
        self.assertNotIn("new-article", seen)
        self.assertEqual(len(state["file"]), poster.MAX_FILE)

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
        with tempfile.TemporaryDirectory() as directory, patch.object(poster, "MAX_FILE", 2):
            path = str(Path(directory) / "state.json")
            state = {"vus": [], "file": [
                {"text": "existing", "link": "https://example.org/existing"}
            ], "feeds": feeds.copy(), "yt": {}, "init": True, "feed_offset": 0}
            seen = poster.collect_articles(
                feeds, state, http=transport, parser=parser,
                checkpoint=lambda current, ids: poster.save_state(current, ids, path),
            )
            self.assertIn("a1", seen)
            self.assertNotIn("b1", seen)
            restarted = poster.load_state(path)
            restarted["file"].pop(0)  # L’ancien article est traité pendant ce cycle.
            poster.collect_articles(
                feeds, restarted, http=transport, parser=parser,
                checkpoint=lambda current, ids: poster.save_state(current, ids, path),
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
        poster.collect_articles(feeds, state, http=transport, parser=parser)
        self.assertEqual(
            [item["link"] for item in state["file"]],
            ["https://example.com/old", "https://example.com/shared", "https://example.com/new"],
        )

    def test_feed_cursor_prevents_republishing_entries_evicted_from_global_seen_window(self):
        feed = "https://example.org/feed"
        entries = [
            {"id": "latest", "title": "Latest", "link": "https://example.org/latest"},
            {"id": "older-2", "title": "Older 2", "link": "https://example.org/older-2"},
            {"id": "older-1", "title": "Older 1", "link": "https://example.org/older-1"},
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
        poster.collect_articles([feed], state, http=transport, parser=parser)
        self.assertEqual(state["file"], [])
        self.assertEqual(state["feed_health"][feed]["latest_uid"], "latest")

    def test_youtube_legacy_feed_uses_username_feed_when_available(self):
        feed = "https://gdata.youtube.com/feeds/base/users/example/uploads"
        entries = [{"id": "yt-one", "link": "https://youtube.com/watch?v=one"}]
        response = Mock(content=b"<feed/>")
        response.raise_for_status.return_value = None
        transport = Mock()
        transport.get.return_value = response
        parser = Mock(return_value=Mock(entries=entries))
        state = {"yt": {}}
        result = poster.entries_for_feed(feed, state, http=transport, parser=parser)
        self.assertEqual(result, entries)
        transport.get.assert_called_once_with(
            "https://www.youtube.com/feeds/videos.xml?user=example",
            headers={"User-Agent": poster.UA}, timeout=(5, 20),
        )
        self.assertEqual(state["yt"], {})

    def test_youtube_legacy_feed_resolves_channel_from_page_as_fallback(self):
        feed = "https://gdata.youtube.com/feeds/base/users/example/uploads"
        channel_id = "UC1234567890123456789012"
        empty = Mock(content=b"<feed/>")
        empty.raise_for_status.return_value = None
        page = Mock(text='"externalId":"' + channel_id + '"')
        page.raise_for_status.return_value = None
        channel_feed = Mock(content=b"<feed/>")
        channel_feed.raise_for_status.return_value = None
        transport = Mock()
        transport.get.side_effect = [empty, page, channel_feed]
        entries = [{"id": "yt-two", "link": "https://youtube.com/watch?v=two"}]
        parser = Mock(side_effect=[Mock(entries=[]), Mock(entries=entries)])
        state = {"yt": {}}
        result = poster.entries_for_feed(feed, state, http=transport, parser=parser)
        self.assertEqual(result, entries)
        self.assertEqual(state["yt"]["example"], channel_id)
        self.assertEqual(transport.get.call_count, 3)
        self.assertEqual(
            transport.get.call_args_list[1].kwargs["timeout"], 30,
        )


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
        with patch.object(poster, "MAX_FILE", 1), patch("builtins.print") as log:
            first_seen = poster.collect_articles(feeds, state, http=transport, parser=parser)
            second_seen = poster.collect_articles(feeds, state, http=transport, parser=parser)
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
                def sender(item, token, chat):
                    return poster.send_message(item, token, chat, http=transport)
                sent = poster.process_queue(
                    state, {}, "token", "chat", state_path=path, dry_run=False,
                    sender=sender, delay=0,
                )
                self.assertEqual(sent, 0)
                self.assertEqual(state["file"], items)
                self.assertEqual(state["erreurs"], [])
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
                seen = poster.collect_articles(feeds, state, http=transport, parser=parser)
                poster.process_queue(
                    state, seen, "token", "chat", state_path=path, dry_run=False,
                    sender=Mock(return_value=False), delay=0,
                )
                saved = json.loads(Path(path).read_text())
                self.assertEqual(saved["feed_offset"], expected_offset)
                state = poster.load_state(path)

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
                poster.save_state(current, seen, str(path))
                raise RuntimeError("simulated interruption")

            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                poster.collect_articles(
                    feeds, state, http=transport, parser=parser,
                    checkpoint=save_then_interrupt, checkpoint_every=1,
                )
            resumed = poster.load_state(path)
            self.assertEqual(resumed["feed_offset"], 1)
            transport.get.reset_mock()
            poster.collect_articles(
                feeds, resumed, http=transport, parser=parser,
                checkpoint=lambda current, seen: poster.save_state(current, seen, str(path)),
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
        poster.collect_articles(
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

        poster.collect_articles(
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
        ] * poster.MAX_FILE, "feeds": [], "yt": {}, "init": True}
        response = Mock(content=b"<rss/>")
        response.raise_for_status.return_value = None
        transport = Mock()
        transport.get.return_value = response
        parser = Mock(return_value=Mock(entries=entries))

        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.json")
            seen = poster.collect_articles(
                [feed], state, http=transport, parser=parser,
                checkpoint=lambda current, ids: poster.save_state(current, ids, state_path),
            )
            self.assertIn(feed, state["feeds"])
            self.assertIn("archive-1", seen)
            self.assertIn("archive-2", seen)
            self.assertNotIn("latest", seen)
            self.assertEqual(len(state["file"]), poster.MAX_FILE)

            restarted = poster.load_state(state_path)
            restarted["file"].pop()
            poster.collect_articles(
                [feed], restarted, http=transport, parser=parser,
                checkpoint=lambda current, ids: poster.save_state(current, ids, state_path),
            )
            self.assertIn("https://example.org/3", [item["link"] for item in restarted["file"]])
            self.assertEqual(sum(item["link"].endswith("/1") for item in restarted["file"]), 0)

    def test_first_initialization_remains_silent_even_with_a_full_queue(self):
        feed = "https://example.org/feed"
        entry = {"id": "initial", "title": "Initial", "link": "https://example.org/1"}
        state = {"vus": [], "file": [{"text": "x", "link": "https://example.org/x"}] * poster.MAX_FILE,
                 "feeds": [], "yt": {}, "init": False}
        response = Mock(content=b"<rss/>")
        response.raise_for_status.return_value = None
        transport = Mock()
        transport.get.return_value = response
        parser = Mock(return_value=Mock(entries=[entry]))
        seen = poster.collect_articles([feed], state, http=transport, parser=parser)
        self.assertIn("initial", seen)
        self.assertEqual(len(state["file"]), poster.MAX_FILE)
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
                poster.collect_articles(feeds, state, http=transport, parser=parser)
        self.assertEqual(state["feed_health"][feeds[0]]["consecutive_errors"], 3)
        self.assertTrue(any("défaillances récurrentes" in str(c) for c in log.call_args_list))
        self.assertNotIn("do-not-print", " ".join(str(c) for c in log.call_args_list))
        self.assertEqual(transport.get.call_count, 6)

    def test_send_accepted_before_state_save_is_retried_after_restart_at_least_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            item = {"text": "test", "link": "https://example.org", "pub": ""}
            state = {"vus": [], "file": [item], "erreurs": [], "feeds": [], "yt": {}, "v": 2}
            real_save = poster.save_state
            calls = {"count": 0}

            def fail_after_send(current, seen, state_path):
                calls["count"] += 1
                if calls["count"] == 1:
                    return real_save(current, seen, state_path)
                raise OSError("simulated crash before post-send checkpoint")

            accepted = Mock(return_value=True)
            with patch.object(poster, "save_state", side_effect=fail_after_send):
                with self.assertRaisesRegex(OSError, "simulated crash"):
                    poster.process_queue(
                        state, {}, "token", "chat", state_path=path, dry_run=False,
                        sender=accepted, delay=0,
                    )
            persisted = poster.load_state(path)
            self.assertEqual(persisted["file"], [item])
            sender_after_restart = Mock(return_value=True)
            poster.process_queue(
                persisted, {}, "token", "chat", state_path=path, dry_run=False,
                sender=sender_after_restart, delay=0,
            )
            accepted.assert_called_once()
            sender_after_restart.assert_called_once()


if __name__ == "__main__":
    unittest.main()
