import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from newsbot import persistence, publication


class PersistencePublicationTests(unittest.TestCase):
    def test_malformed_recovery_records_are_rejected_without_overwriting_state(self):
        # Les structures de reprise doivent échouer explicitement, pas disparaître.
        cases = [
            None,
            {"item": []},
            {"item": {}, "uid": 42},
            {"item": {}, "url_hash": "invalid"},
            {"item": {}, "first_seen_at": None},
            {"item": {}, "admitted_at": -1},
            {"item": {}, "published_at": "invalid"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            for buffer in ("admission_buffer", "date_quarantine"):
                for record in cases:
                    with self.subTest(buffer=buffer, record=record):
                        original = json.dumps({"file": [{"text": "pending"}], buffer: [record]})
                        path.write_text(original, encoding="utf-8")
                        with self.assertRaises(ValueError):
                            persistence.load_state(path)
                        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_failed_atomic_replace_preserves_previous_state_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            previous = b'{"file":[{"text":"previous"}],"vus":["previous"]}'
            path.write_bytes(previous)
            with patch.object(persistence.os, "replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    persistence.save_state({"file": [{"text": "new"}]}, {"new": None}, path)
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_injected_save_failure_before_send_prevents_any_publication(self):
        item = {"text": "pending", "link": "https://example.org/pending"}
        state = {"file": [item], "erreurs": []}
        sender = Mock()
        save = Mock(side_effect=OSError("disk failure"))
        with self.assertRaises(OSError), patch("builtins.print"):
            publication.process_queue(
                state, {"pending": None}, "token", "chat", dry_run=False,
                sender=sender, save=save, clock=lambda: 1000,
            )
        sender.assert_not_called()
        self.assertEqual(state["file"], [item])

    def test_dry_run_does_not_invoke_injected_save(self):
        state = {"file": [{"text": "pending", "link": "https://example.org/pending"}]}
        before = copy.deepcopy(state)
        save = Mock()
        sender = Mock()
        with patch("builtins.print"):
            self.assertEqual(publication.process_queue(
                state, {}, "token", "chat", dry_run=True, save=save, sender=sender,
            ), 0)
        save.assert_not_called()
        sender.assert_not_called()
        self.assertEqual(state, before)

    def test_confirmed_send_followed_by_save_failure_stops_and_retains_durable_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            pending = [
                {"text": str(i), "link": f"https://example.org/{i}"} for i in range(2)
            ]
            state = {"file": copy.deepcopy(pending), "erreurs": []}
            calls = []

            def save(current, seen, destination):
                calls.append(copy.deepcopy(current["file"]))
                if len(calls) == 2:
                    raise OSError("disk failure")
                persistence.save_state(current, seen, destination)

            sender = Mock(return_value=publication.TelegramSendResult(True, message_id=42))
            with self.assertRaises(OSError), patch("builtins.print") as logs:
                publication.process_queue(
                    state, {}, "token", "chat", state_path=path, dry_run=False,
                    sender=sender, save=save, clock=lambda: 1000,
                    monotonic=lambda: 0,
                )
            sender.assert_called_once()
            self.assertEqual(calls, [pending, pending[1:]])
            self.assertEqual(json.loads(path.read_text())["file"], pending)
            self.assertEqual(state["file"], pending[1:])
            self.assertTrue(any("ALERTE PERSISTANCE" in str(call) for call in logs.call_args_list))


if __name__ == "__main__":
    unittest.main()
