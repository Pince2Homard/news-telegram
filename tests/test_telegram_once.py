"""Transport entierement simule : aucun appel Telegram ni credential reel."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.windows_watcher import telegram_once as once, watcher


class TelegramOnceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        self.responses = {
            "getMe": {"ok": True, "result": {"id": 12345, "is_bot": True, "username": once.BOT}},
            "getChat": {"ok": True, "result": {"id": once.CHANNEL, "type": "channel", "title": "Codex"}},
            "getChatMember": {"ok": True, "result": {"user": {"id": 12345, "is_bot": True},
                "status": "administrator", "can_post_messages": True}},
            "sendMessage": {"ok": True, "result": {"message_id": 1, "text": once.MESSAGE,
                "chat": {"id": once.CHANNEL, "type": "channel"}}}}

    def transport(self, method, payload):
        self.calls.append((method, payload))
        result = self.responses[method]
        if isinstance(result, Exception): raise result
        return copy.deepcopy(result)

    def send(self):
        return once.send_once(self.transport, self.root, once.approval_digest())

    def test_success_checks_identity_rights_and_exact_destination_once(self):
        self.assertEqual(self.send()["status"], "confirmed")
        calls = copy.deepcopy(self.calls)
        self.assertTrue(self.send()["already_attempted"])
        self.assertEqual(self.calls, calls)
        self.assertEqual([m for m, _ in calls], ["getMe", "getChat", "getChatMember", "sendMessage"])
        self.assertEqual(calls[-1][1], {"chat_id": once.CHANNEL, "text": once.MESSAGE})

    def test_missing_approval_never_calls_transport(self):
        with self.assertRaises(once.Refused): once.send_once(self.transport, self.root, "wrong")
        self.assertEqual(self.calls, [])

    def test_bad_bot_channel_public_chat_or_rights_prevents_send(self):
        for method, field, value in (("getMe", "username", "other"), ("getMe", "is_bot", False),
                ("getChat", "id", -1001), ("getChat", "title", "other"),
                ("getChat", "type", "supergroup"), ("getChat", "username", "public"),
                ("getChatMember", "can_post_messages", False), ("getChatMember", "status", "member"),
                ("getChatMember", "user", {"id": 999, "is_bot": True})):
            with self.subTest(field=field, value=value):
                original = copy.deepcopy(self.responses)
                self.responses[method]["result"][field] = value
                with self.assertRaises(once.Refused): self.send()
                self.assertNotIn("sendMessage", [m for m, _ in self.calls])
                self.responses = original
                self.calls.clear()

    def test_preflight_exception_never_leaks_raw_error(self):
        self.responses["getMe"] = RuntimeError("sensitive fixture must not appear")
        with self.assertRaises(once.Refused) as error: self.send()
        self.assertNotIn("sensitive", str(error.exception))
        self.assertFalse((self.root / "telegram-once.json").exists())

    def test_ambiguous_result_and_restart_never_resend(self):
        self.responses["sendMessage"] = TimeoutError("sensitive fixture")
        self.assertEqual(self.send()["status"], "uncertain")
        self.assertTrue(self.send()["already_attempted"])
        self.assertEqual([m for m, _ in self.calls].count("sendMessage"), 1)
        self.assertNotIn("sensitive", (self.root / "telegram-once.json").read_text())

    def test_malformed_success_or_wrong_destination_is_uncertain(self):
        self.responses["sendMessage"]["result"]["chat"]["id"] = 123
        self.assertEqual(self.send()["status"], "uncertain")
        self.send()
        self.assertEqual([m for m, _ in self.calls].count("sendMessage"), 1)

    def test_429_records_delay_without_sleep_or_retry(self):
        self.responses["sendMessage"] = {"ok": False, "error_code": 429, "parameters": {"retry_after": 60}}
        result = self.send()
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["retry_after_seconds"], 60)
        self.send()
        self.assertEqual([m for m, _ in self.calls].count("sendMessage"), 1)

    def test_disk_failure_before_send_blocks_and_after_send_leaves_intent(self):
        with patch.object(watcher, "save_state", side_effect=OSError("disk")):
            with self.assertRaises(OSError): self.send()
        self.assertNotIn("sendMessage", [m for m, _ in self.calls])
        original = watcher.save_state
        def fail_after(path, data):
            if data["status"] != "in_flight": raise OSError("disk")
            original(path, data)
        with patch.object(watcher, "save_state", side_effect=fail_after):
            with self.assertRaises(OSError): self.send()
        self.assertEqual(self.send()["status"], "in_flight")
        self.assertEqual([m for m, _ in self.calls].count("sendMessage"), 1)

    def test_corrupt_journal_and_concurrent_run_block_without_network(self):
        with watcher.single_instance_lock(self.root / "telegram-once.lock"):
            with self.assertRaises(watcher.WatcherError): self.send()
        (self.root / "telegram-once.json").write_text("broken", encoding="utf-8")
        with self.assertRaises(once.Refused): self.send()
        self.assertEqual(self.calls, [])

    def test_new_journal_acl_must_pass_before_post(self):
        with self.assertRaises(ValueError):
            once.send_once(self.transport, self.root, once.approval_digest(),
                           journal_guard=MockGuard())
        self.assertNotIn('sendMessage', [m for m, _ in self.calls])
        self.assertEqual(self.send()['status'], 'in_flight')

    def test_token_file_in_repository_rejected_no_production_fallback(self):
        token = self.root / "credential"
        token.write_text("fixture-not-a-real-token", encoding="utf-8")
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": "unused"}):
            with self.assertRaises(once.Refused): once.read_external_token(token, self.root)
        token.chmod(0o600)
        if os.name == "nt":
            with patch('tools.windows_watcher.credential_acl.verify', side_effect=ValueError('unsafe')):
                with self.assertRaises(once.Refused): once.read_external_token(token, self.root / "repo")
        else:
            self.assertEqual(once.read_external_token(token, self.root / "repo"), "fixture-not-a-real-token")

    def test_offline_cli_simulation_never_opens_network_or_reads_token(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("no network")), \
                patch.object(once, "read_external_token", side_effect=AssertionError("no token")):
            result = once.simulate()
        self.assertEqual(result["real_messages_sent"], 0)
        self.assertTrue(result["second_call_blocked"])
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(json.loads(json.dumps(result)), result)


class MockGuard:
    def __call__(self, path):
        raise ValueError('unsafe inherited permissions')
