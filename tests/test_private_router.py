"""Fixtures synthétiques exclusivement ; ni token ni installation Windows."""
from contextlib import ExitStack
import copy
from datetime import datetime, timedelta, timezone
import html
from pathlib import Path
import unittest
from unittest.mock import patch

from tools.windows_watcher import private_router as router


class PrivateRouterTests(unittest.TestCase):
    def setUp(self):
        self.config = {"mode": "simulation", "send_enabled": False,
                       "notifications_channel_id": None,
                       "owner_user_id": 12345, "arbitration_private_chat_id": 12345}
        self.message = {"from": {"id": 12345, "is_bot": False},
                        "chat": {"id": 12345, "type": "private"}, "text": "/status"}
        self.now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
        self.snapshot = {"initialized": True, "last_poll_at": "2026-11-14T11:59:00Z",
                         "inbox": []}

    def route(self, text="/status", snapshot=None):
        return router.route_message({**self.message, "text": text}, self.config,
                                    self.snapshot if snapshot is None else snapshot, self.now)

    def test_six_commands_and_structured_simulation(self):
        for command in ("/start", "/help", "/status", "/pr", "/ci", "/rapports"):
            with self.subTest(command=command):
                result = self.route(command)
                self.assertTrue(result["authorized"])
                self.assertTrue(result["simulation"])
                self.assertFalse(result["sent"])
                self.assertEqual(result["command"], command)
                self.assertEqual(result["parse_mode"], "HTML")
                self.assertLessEqual(len(result["text"]), router.MAX_RESPONSE_LENGTH)
        help_text = self.route("/help")["text"]
        self.assertEqual({word for word in help_text.split() if word.startswith("/")},
                         {"/start", "/help", "/status", "/pr", "/ci", "/rapports"})
        self.assertIn("QG mobile Codex", self.route("/start")["text"])

    def test_identity_rejections_are_silent_and_indistinguishable(self):
        messages = [None, [], "secret", {}, {"text": "/start"}]
        for key in ("chat", "from"):
            for bad in (None, [], "secret", {}):
                messages.append({**self.message, key: bad})
        for container, field, bad_values in (
            ("from", "id", [None, True, "12345", 12345.0, 999, -1]),
            ("chat", "id", [None, True, "12345", 12345.0, 999, -1]),
            ("from", "is_bot", [None, True, 0, "false", []]),
            ("chat", "type", [None, "group", "supergroup", "channel", True, []]),
        ):
            missing = copy.deepcopy(self.message)
            del missing[container][field]
            messages.append(missing)
            for bad in bad_values:
                messages.append({**self.message, container: {**self.message[container], field: bad}})
        messages += [{**self.message, "from": {"username": "owner", "is_bot": False}},
                     {**self.message, "chat": {"username": "owner", "type": "private"}}]
        expected = router.route_message(None, None, None, None)
        for message in messages:
            for command in ("/start", "/help", "/status", "/pr", "/ci", "/rapports"):
                candidate = {**message, "text": command} if isinstance(message, dict) else message
                with self.subTest(message=candidate):
                    self.assertEqual(router.route_message(candidate, self.config,
                                                         self.snapshot, self.now), expected)
        self.assertIsNone(expected["text"])
        self.assertFalse(expected["authorized"])

    def test_all_identifiable_forward_and_intermediary_forms_denied(self):
        for field in router.PROVENANCE_FIELDS:
            for value in (None, False, {}, "secret"):
                with self.subTest(field=field, value=value):
                    result = router.route_message({**self.message, field: value}, self.config,
                                                  self.snapshot, self.now)
                    self.assertFalse(result["authorized"])
                    self.assertIsNone(result["text"])

    def test_fail_closed_configuration_never_enrolls_start(self):
        configs = [None, {}, [], {**self.config, "mode": "live"},
                   {**self.config, "send_enabled": True}, {**self.config, "token": "secret"}]
        for key in ("owner_user_id", "arbitration_private_chat_id"):
            for value in (None, 0, True, "owner", [], 2 ** 52):
                configs.append({**self.config, key: value})
            configs.append({k: v for k, v in self.config.items() if k != key})
        for config in configs:
            before = copy.deepcopy(config)
            self.assertFalse(router.route_message({**self.message, "text": "/start"},
                                                  config, self.snapshot, self.now)["authorized"])
            self.assertEqual(config, before)

    def test_empty_malformed_unknown_arguments_and_mentions(self):
        for value in (None, [], {}, 1, True, "", "x" * 10000):
            self.assertEqual(self.route(value)["code"], "invalid_message")
        missing = {k: v for k, v in self.message.items() if k != "text"}
        self.assertEqual(router.route_message(missing, self.config, None, None)["code"], "invalid_message")
        for value in ("/exec", "/help secret", "/start 12345", "/status@bot", "/STATUS",
                      " /status", "/status\n", "<b>secret</b>&", "/status\x00"):
            result = self.route(value)
            self.assertEqual(result["code"], "unsupported_command")
            self.assertNotIn("secret", result["text"])

    def test_snapshot_freshness_boundaries_timezone_and_no_live_claim(self):
        text = html.unescape(self.route()["text"])
        for value in ("11:59:00+00:00", "âge 60 s", "initialisé", "aucune vérification en temps réel",
                      "Activité actuelle du processus non vérifiée"):
            self.assertIn(value, text)
        for age, stale in ((3600, False), (3601, True), (86400, True)):
            snapshot = {**self.snapshot, "last_poll_at": (self.now - timedelta(seconds=age)).isoformat()}
            text = self.route(snapshot=snapshot)["text"]
            self.assertEqual("périmé" in text, stale)
            self.assertEqual("État actuel indisponible" in text, stale)
        snapshot = {**self.snapshot, "last_poll_at": "2026-11-14T12:59:00+01:00"}
        self.assertIn("âge 60 s", self.route(snapshot=snapshot)["text"])

    def test_missing_stale_invalid_and_contradictory_snapshots(self):
        snapshots = [None, [], {}, {**self.snapshot, "initialized": 1},
                     {**self.snapshot, "initialized": False},
                     {**self.snapshot, "last_poll_at": None},
                     {**self.snapshot, "last_poll_at": "2026-11-14T12:01:00Z"}]
        for bad in ([], "secret", 0, "2026-11-14T12:00:00", "9999-99-99", "x" * 10000):
            snapshots.append({**self.snapshot, "last_poll_at": bad})
        for bad in (None, {}, [None], [{"protocol_error": []}], [{}] * 1501):
            snapshots.append({**self.snapshot, "inbox": bad})
        for bad in (True, "secret", [], float("nan"), float("inf"), -1, 10 ** 1000):
            snapshots.append({**self.snapshot, "next_poll_at": bad})
        for snapshot in snapshots:
            with self.subTest(snapshot=str(snapshot)[:100]):
                result = router.route_message(self.message, self.config, snapshot, self.now)
                self.assertIn("État actuel indisponible", result["text"])
                self.assertNotIn("secret", result["text"])
        empty = {"initialized": False, "inbox": []}
        self.assertIn("non initialisé", self.route(snapshot=empty)["text"])
        for now in (None, "secret", 0, datetime(2026, 11, 14)):
            self.assertIn("référence temporelle invalide", router.route_message(
                self.message, self.config, self.snapshot, now)["text"])

    def test_existing_anomalies_are_summarized_without_free_text_or_secrets(self):
        snapshot = {**self.snapshot, "next_poll_at": self.now.timestamp() + 60,
                    "inbox": [{"protocol_error": "invalid_envelope", "body": "secret <b>&"},
                              {"protocol_error": "secret <b>&"}],
                    "ci_snapshots": {"secret": {"result": "success"}},
                    "token": "secret", "status": "running"}
        text = html.unescape(self.route(snapshot=snapshot)["text"])
        self.assertIn("enveloppe de coordination invalide", text)
        self.assertIn("anomalie de protocole non détaillée", text)
        self.assertIn("relevé différé", text)
        for secret in ("secret", "<b>", "success", "running"):
            self.assertNotIn(secret, text)
        stale = {**snapshot, "last_poll_at": "2026-11-13T12:00:00Z"}
        self.assertIn("enveloppe de coordination invalide", self.route(snapshot=stale)["text"])

    def test_html_escaping_and_size_limit(self):
        value = router._response(True, "ok", '<b>"&</b>')["text"]
        self.assertEqual(value, "&lt;b&gt;&quot;&amp;&lt;/b&gt;")
        self.assertLessEqual(len(router._response(True, "ok", "&" * 3000)["text"]),
                             router.MAX_RESPONSE_LENGTH)

    def test_no_io_process_environment_clock_or_input_mutation(self):
        before = copy.deepcopy((self.message, self.config, self.snapshot))
        with ExitStack() as stack:
            for target in ("builtins.open", "pathlib.Path.open", "pathlib.Path.write_text",
                           "socket.socket", "socket.create_connection", "urllib.request.urlopen",
                           "requests.sessions.Session.request", "subprocess.Popen", "os.getenv",
                           "os._Environ.__getitem__", "time.time"):
                stack.enter_context(patch(target, side_effect=AssertionError("I/O interdit")))
            for command in ("/start", "/help", "/status", "/pr", "/ci", "/rapports", "/exec", None):
                self.route(command)
            router.route_message(None, None, None, None)
        self.assertEqual((self.message, self.config, self.snapshot), before)

    def test_router_has_only_pure_imports_and_no_production_integration(self):
        import ast
        source = Path(router.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        imports.update(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                       for alias in node.names)
        self.assertEqual(imports, {"datetime", "html", "json", "math", "re", None})
        for name in ("watcher.py", "manage.py", "telegram_http.py", "telegram_once.py"):
            self.assertNotIn("private_router", Path(router.__file__).with_name(name).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
