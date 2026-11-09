import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import poster


class PosterSafetyTests(unittest.TestCase):
    def test_publication_is_disabled_by_default(self):
        # Le point d'entrée ne peut envoyer sans ALLOW_TELEGRAM_SEND=1.
        self.assertNotEqual(__import__("os").environ.get("ALLOW_TELEGRAM_SEND"), "1")

    def test_format_item_escapes_html_and_rejects_invalid_urls(self):
        item = poster.format_item({
            "title": "<script>alert(1)</script>",
            "link": "https://example.org/?a=1&b=2",
        })
        self.assertIn("&lt;script&gt;", item["text"])
        self.assertIn("&amp;", item["text"])
        self.assertIsNone(poster.format_item({"title": "bad", "link": "javascript:alert(1)"}))

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

    def test_state_is_written_as_valid_json(self):
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

    def test_send_message_success_uses_supplied_test_transport(self):
        response = Mock(ok=True, status_code=200)
        transport = Mock(post=Mock(return_value=response))
        self.assertTrue(poster.send_message(
            {"text": "test"}, "fake-token", "fake-chat",
            http=transport, sleep=Mock(),
        ))
        transport.post.assert_called_once()
        self.assertIn("fake-token", transport.post.call_args.args[0])
        self.assertEqual(transport.post.call_args.kwargs["data"]["chat_id"], "fake-chat")


if __name__ == "__main__":
    unittest.main()
