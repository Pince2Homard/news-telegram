"""Preparation Phase 8 : fixtures uniquement, sans Telegram ou etat reel."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tools.windows_watcher import supervision


class SupervisionTests(unittest.TestCase):
    def test_codex_channel_configuration_is_simulation_only_and_not_an_identity(self):
        config = json.loads((Path(__file__).resolve().parents[1] / "tools" /
            "windows_watcher" / "supervision.example.json").read_text(encoding="utf-8"))
        self.assertEqual(config["notifications_channel_id"], -1003833675690)
        self.assertIsNone(config["arbitration_private_chat_id"])
        self.assertIsNone(config["owner_user_id"])
        with patch("urllib.request.urlopen", side_effect=AssertionError("no network")):
            result = supervision.simulate_notification(config, 1)
            identity = supervision.simulate_private_identity(config, {
                "chat": {"type": "channel", "id": config["notifications_channel_id"]},
                "from": {"id": 12345, "is_bot": False}})
        self.assertFalse(result["sent"])
        self.assertTrue(result["destination_configured"])
        self.assertFalse(identity["identity_matches"])
        self.assertFalse(identity["decision_authorized"])

    def setUp(self):
        self.config = json.loads((Path(__file__).resolve().parents[1] / "tools" /
            "windows_watcher" / "supervision.example.json").read_text(encoding="utf-8"))
        # Identifiants purement synthetiques, jamais utilises sur le reseau.
        self.config.update(notifications_channel_id=-1001234567890,
                           arbitration_private_chat_id=12345, owner_user_id=12345)
        self.message = {"chat": {"type": "private", "id": 12345},
                        "from": {"id": 12345, "is_bot": False}}

    def test_notification_never_sends_or_reads_production_configuration(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("no network")), \
                patch.dict("os.environ", {"TELEGRAM_CHAT": "production", "TELEGRAM_TOKEN": "unused"}):
            result = supervision.simulate_notification(self.config, 2)
        self.assertEqual(result, {"simulation": True, "sent": False,
                                 "destination_configured": True, "new_events": 2})
        self.config["notifications_channel_id"] = None
        self.assertFalse(supervision.simulate_notification(self.config, 1)["destination_configured"])

    def test_live_mode_unknown_settings_and_public_names_are_rejected(self):
        for key, value in (("send_enabled", True), ("mode", "live"),
                           ("notifications_channel_id", "@old_name"), ("owner_user_id", True),
                           ("arbitration_private_chat_id", -1001234567890), ("token", "unused")):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    supervision.validate_config({**self.config, key: value})

    def test_private_identity_is_separate_from_channel_and_never_authorizes_action(self):
        original = copy.deepcopy(self.config)
        result = supervision.simulate_private_identity(self.config, self.message)
        self.assertTrue(result["identity_matches"])
        self.assertFalse(result["decision_authorized"])
        self.assertEqual(self.config, original)

    def test_missing_identity_wrong_sender_chat_forward_or_bot_is_denied(self):
        candidates = [None, {}, {**self.message, "sender_chat": {}},
                      {**self.message, "forward_origin": {}}, {**self.message, "via_bot": {}},
                      {**self.message, "chat": {"type": "channel", "id": -1001234567890}},
                      {**self.message, "chat": {"type": "private", "id": 999}},
                      {**self.message, "from": {"id": 999, "is_bot": False}},
                      {**self.message, "from": {"id": 12345, "is_bot": True}}]
        for message in candidates:
            self.assertFalse(supervision.simulate_private_identity(self.config, message)["identity_matches"])
        for key in ("owner_user_id", "arbitration_private_chat_id"):
            self.assertFalse(supervision.simulate_private_identity(
                {**self.config, key: None}, self.message)["identity_matches"])
