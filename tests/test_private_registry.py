"""Consultation du registre du watcher : données et identités synthétiques."""
from contextlib import ExitStack
import copy
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.windows_watcher import coordination, private_router as router, watcher


SHA = "a" * 40
OTHER_SHA = "b" * 40
REPORT = "11111111-1111-4111-8111-111111111111"
UNKNOWN = "22222222-2222-4222-8222-222222222222"
ROOT = "https://github.com/Pince2Homard/news-telegram"


class PrivateRegistryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
        self.config = dict(mode="simulation", send_enabled=False, notifications_channel_id=None,
                           owner_user_id=12345, arbitration_private_chat_id=12345)
        self.message = {"from": {"id": 12345, "is_bot": False},
                        "chat": {"id": 12345, "type": "private"}}
        envelope = {"schema_version": 1, "coordination_id": REPORT,
                    "repository": router.REPOSITORY, "pull_request": 10, "head_sha": SHA,
                    "kind": "report", "status": "completed", "requested_action": "review_only",
                    "reply_to": None, "expires_at": None, "evidence_urls": []}
        parsed, error = coordination.parse_envelope("```json\n" + json.dumps(envelope) + "\n```",
                                                   router.REPOSITORY)
        self.assertIsNone(error)
        event = {"coordination": parsed, "authorized_author": True, "prefix": "CODEX-REPORT",
                 "protocol_error": None, "created_at": "2026-11-14T11:50:00Z",
                 "url": ROOT + "/issues/7#issuecomment-123", "body": "RAW_SECRET_DO_NOT_DISPLAY"}
        self.snapshot = {"initialized": True, "repository": router.REPOSITORY,
                         "last_poll_at": "2026-11-14T11:59:00Z", "inbox": [],
                         "pr_snapshots": {"10": {"head_sha": SHA, "closed": False}},
                         "threads": {"pr:10": {"updated_at": "2026-11-14T11:58:00Z", "cursors": {}}},
                         "ci_snapshots": {SHA: {"result": "success", "checked_at": self.now.timestamp() - 90}},
                         "coordination_registry": {REPORT: {"event": event, "conflict": False}}}

    def route(self, text, snapshot=None):
        result = router.route_message({**self.message, "text": text}, self.config,
                                      self.snapshot if snapshot is None else snapshot, self.now)
        self.assertLessEqual(len(result["text"] or ""), router.MAX_RESPONSE_LENGTH)
        self.assertFalse(result["sent"])
        return html.unescape(result["text"] or "")

    def test_pr_uses_watcher_format_without_inventing_title_merge_or_liveness(self):
        text = self.route("/pr")
        for value in ("PR #10", "titre indisponible", "ouverte", "11:58:00+00:00", ROOT + "/pull/10"):
            self.assertIn(value, text)
        self.assertIn("état actuel non vérifié", text)
        self.assertIn("Aucune aptitude à fusionner déduite", text)
        self.assertNotIn("fusionnable", text)
        self.assertEqual(text, self.route("/pr 10"))
        self.assertIn("inconnu", self.route("/pr 99"))
        self.snapshot["pr_snapshots"]["10"].update(closed=True)
        self.assertIn("fermée (fusion non renseignée)", self.route("/pr"))
        self.snapshot["pr_snapshots"]["10"]["merged"] = True
        self.assertIn("fusionnée", self.route("/pr"))

    def test_ci_states_association_and_unknown_queries(self):
        for state, expected in (("success", "réussite"), ("failure", "échec"),
                                ("pending", "en attente"), ("incomplete", "contrôles incomplets"),
                                ("unknown", "information indisponible")):
            self.snapshot["ci_snapshots"][SHA]["result"] = state
            text = self.route("/ci")
            self.assertIn(expected, text)
            self.assertIn(SHA, text)
            self.assertIn("PR : #10", text)
            self.assertIn(ROOT + "/commit/" + SHA + "/checks", text)
            self.assertEqual(text, self.route("/ci 10"))
            self.assertEqual(text, self.route("/ci " + SHA))
            if state != "success":
                self.assertNotIn("réussite", text)
        self.assertIn("PR inconnue", self.route("/ci 99"))
        self.assertIn("résultat absent", self.route("/ci " + OTHER_SHA))

    def test_reports_require_structured_report_and_verified_author(self):
        text = self.route("/rapports")
        for expected in (REPORT, "PR : #10", "Type : report", "completed", "11:50:00+00:00",
                         ROOT + "/issues/7#issuecomment-123"):
            self.assertIn(expected, text)
        self.assertEqual(text, self.route("/rapports " + REPORT))
        self.assertIn("inconnu", self.route("/rapports " + UNKNOWN))
        self.assertNotIn("RAW_SECRET", text)
        record = self.snapshot["coordination_registry"][REPORT]
        for field in ("conflict", "source_changed", "source_missing"):
            modified = copy.deepcopy(self.snapshot)
            modified["coordination_registry"][REPORT][field] = True
            self.assertIn("source contradictoire", self.route("/rapports", modified))
        for event in ({"body": "[CODEX-REPORT] commentaire ordinaire"},
                      {**record["event"], "authorized_author": False},
                      {**record["event"], "prefix": "CHATGPT-REVIEW"},
                      {**record["event"], "coordination": {**record["event"]["coordination"], "prompt": "SECRET"}}):
            modified = copy.deepcopy(self.snapshot)
            modified["coordination_registry"][REPORT]["event"] = event
            text = self.route("/rapports", modified)
            self.assertNotIn("Rapport " + REPORT, text)
            self.assertNotIn("SECRET", text)
        record["event"]["coordination"]["kind"] = "task"
        self.assertIn("Aucun élément", self.route("/rapports"))

    def test_empty_missing_and_wrong_repository_never_fabricate_records(self):
        empty = {key: value for key, value in self.snapshot.items()
                 if key not in ("pr_snapshots", "ci_snapshots", "coordination_registry")}
        for command in ("/pr", "/ci", "/rapports"):
            self.assertIn("Aucun élément", self.route(command, empty))
            for snapshot in ([], {}, {**empty, "repository": "other/repo"},
                             {**empty, "initialized": False}):
                self.assertIn("indisponible", self.route(command, snapshot))
            result = router.route_message({**self.message, "text": command}, self.config, None, self.now)
            self.assertIn("indisponible", result["text"])

    def test_authentication_precedes_any_snapshot_access_for_all_six_commands(self):
        class ForbiddenSnapshot(dict):
            def get(self, *args):
                raise AssertionError("consultation avant authentification")

        for command in ("/start", "/help", "/status", "/pr", "/ci", "/rapports"):
            for message, config in (({**self.message, "from": {"id": 999, "is_bot": False}}, self.config),
                                    ({**self.message, "forward_date": 123}, self.config),
                                    (self.message, None)):
                result = router.route_message({**message, "text": command}, config,
                                              ForbiddenSnapshot(), self.now)
                self.assertFalse(result["authorized"])
                self.assertIsNone(result["text"])

    def test_global_and_item_freshness_use_original_dates(self):
        for command in ("/pr", "/ci", "/rapports"):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot["last_poll_at"] = "2026-11-13T11:59:00Z"
            snapshot["threads"]["pr:10"]["updated_at"] = "2026-11-13T11:58:00Z"
            snapshot["ci_snapshots"][SHA]["checked_at"] = self.now.timestamp() - 86490
            snapshot["coordination_registry"][REPORT]["event"]["created_at"] = "2026-11-13T11:50:00Z"
            text = self.route(command, snapshot)
            self.assertIn("instantané périmé", text)
            self.assertIn("données historiques", text)
            self.assertIn("État actuel indisponible", text)
        self.snapshot["ci_snapshots"][SHA]["checked_at"] = self.now.timestamp() - 86490
        self.assertIn("périmé", self.route("/ci"))
        self.assertNotIn("instantané périmé", self.route("/ci"))
        self.snapshot["ci_snapshots"][SHA]["checked_at"] = self.now.timestamp() - 3600
        self.assertIn("récent", self.route("/ci"))
        self.snapshot["ci_snapshots"][SHA]["checked_at"] -= 0.5
        self.assertIn("périmé", self.route("/ci"))
        for date in ("2026-11-14T12:00:01Z", "2026-11-14T11:59:00", None, []):
            for command in ("/pr", "/ci", "/rapports"):
                self.assertIn("indisponible", self.route(command, {**self.snapshot, "last_poll_at": date}))

    def test_strict_github_url_routes_block_redirects_and_arbitrary_destinations(self):
        bad_urls = ("https://evil.test/x", ROOT + "/redirect", ROOT + "/pull/10?next=evil",
                    ROOT + "/pull/10#fragment", ROOT + "/pull/10/../20", ROOT + "/pull/%31%30",
                    "https://github.com.evil.test/Pince2Homard/news-telegram/pull/10",
                    "https://github.com@evil.test/Pince2Homard/news-telegram/pull/10",
                    "https://github.com:443/Pince2Homard/news-telegram/pull/10",
                    ROOT.replace("Pince2Homard", "other") + "/pull/10",
                    ROOT + "/pull/10\n", "javascript:alert(1)", [], None)
        for bad in bad_urls:
            with self.subTest(url=bad):
                snapshot = copy.deepcopy(self.snapshot)
                snapshot["pr_snapshots"]["10"]["html_url"] = bad
                snapshot["ci_snapshots"][SHA]["html_url"] = bad
                snapshot["coordination_registry"][REPORT]["event"]["url"] = bad
                for command in ("/pr", "/ci", "/rapports"):
                    self.assertIn("lien indisponible", self.route(command, snapshot))
        self.snapshot["ci_snapshots"][SHA]["html_url"] = ROOT + "/actions/runs/123"
        self.assertIn(ROOT + "/actions/runs/123", self.route("/ci"))

    def test_missing_invalid_contradictory_fields_and_ci_never_false_green(self):
        for data in (None, {}, {"closed": "false"}, {"closed": False, "state": "closed"},
                     {"closed": False, "merged": True}):
            self.snapshot["pr_snapshots"]["10"] = data
            self.assertIn("données invalides", self.route("/pr"))
        for data in (None, {}, {"result": []}, {"result": "green"},
                     {"result": "success", "checked_at": float("nan")},
                     {"result": "success", "checked_at": self.now.timestamp() + 1},
                     {"result": "success", "checked_at": self.now.timestamp() - 30},
                     {"result": "success", "check_runs": [{"conclusion": "failure"}]}):
            self.snapshot["ci_snapshots"][SHA] = data
            text = self.route("/ci")
            self.assertIn("données invalides", text)
            self.assertNotIn("réussite", text)
        for key in ("pr_snapshots", "ci_snapshots", "coordination_registry"):
            for bad in ([], None, "SECRET"):
                self.assertIn("indisponible", self.route(
                    {"pr_snapshots": "/pr", "ci_snapshots": "/ci", "coordination_registry": "/rapports"}[key],
                    {**self.snapshot, key: bad}))

    def test_bounded_html_preserves_complete_items_and_escapes_titles(self):
        self.snapshot["pr_snapshots"] = {str(i): {"closed": False, "title": '<b>"&' * 200,
                                                 "head_sha": SHA} for i in range(1, 101)}
        text = self.route("/pr")
        self.assertIn("Affichage limité", text)
        self.assertLessEqual(text.count("État observé"), 3)
        result = router.route_message({**self.message, "text": "/pr"}, self.config, self.snapshot, self.now)
        self.assertNotIn("<b>", result["text"])
        self.assertIn("&lt;b&gt;&quot;&amp;", result["text"])
        self.assertNotRegex(result["text"], r"&[^;\s]*$")
        self.assertIn("…", text)
        self.snapshot["pr_snapshots"]["1"]["title"] = "spoof\nPR #999"
        self.assertIn("titre indisponible", self.route("/pr 1"))

    def test_arguments_are_read_only_exact_and_bounded(self):
        for text in ("/pr 0", "/pr 01", "/pr -1", "/pr 1000000", "/pr 10 20", "/ci secret",
                     "/pr 10\n", "/pr@bot", "/rapports invalid", "/rapports " + SHA,
                     "/pr ", "/ci " + SHA.upper(), "/status 10", "/help 10"):
            result = router.route_message({**self.message, "text": text}, self.config, self.snapshot, self.now)
            self.assertEqual(result["code"], "unsupported_command", text)

    def test_no_side_effects_or_sensitive_access_and_no_mutation(self):
        before = copy.deepcopy((self.message, self.config, self.snapshot))
        with ExitStack() as stack:
            for target in ("builtins.open", "pathlib.Path.open", "pathlib.Path.write_text",
                           "socket.socket", "socket.create_connection", "urllib.request.urlopen",
                           "requests.sessions.Session.request", "subprocess.Popen", "os.getenv",
                           "os._Environ.__getitem__", "time.time"):
                stack.enter_context(patch(target, side_effect=AssertionError("accès interdit")))
            for command in ("/pr", "/ci", "/rapports", "/pr 10", "/ci " + SHA, "/rapports " + REPORT):
                self.assertNotIn("RAW_SECRET", self.route(command))
        self.assertEqual((self.message, self.config, self.snapshot), before)

    def test_snapshot_generated_by_existing_watcher_is_directly_compatible(self):
        # Le producteur existant est simulé ; aucun autre format de collecte.
        envelope = self.snapshot["coordination_registry"][REPORT]["event"]["coordination"]

        class GitHubFixture:
            def get_pages(self, path):
                if "/pulls?" in path:
                    return [{"number": 10, "head": {"sha": SHA},
                             "updated_at": "2026-11-14T11:58:00Z"}]
                if path.endswith("/issues/7/comments"):
                    return [{"id": 123, "user": {"login": "Pince2Homard"},
                             "body": "[CODEX-REPORT]\n```json\n" + json.dumps(envelope) + "\n```",
                             "created_at": "2026-11-14T11:50:00Z"}]
                return []

            def get_json(self, path):
                if path.endswith("/issues/7"):
                    return {"updated_at": "2026-11-14T11:58:00Z"}
                if "/check-runs?" in path:
                    return {"total_count": 1, "check_runs": [{"status": "in_progress", "conclusion": None}]}
                if "/status?" in path:
                    return {"total_count": 0, "statuses": []}
                raise AssertionError("appel inattendu")

        with tempfile.TemporaryDirectory(prefix="phase822-fixture-") as directory:
            path = Path(directory) / "watcher-fixture.json"
            poller = watcher.Watcher(watcher.default_config(), path, GitHubFixture(), lambda _: False)
            with patch.object(watcher.time, "time", return_value=self.now.timestamp() - 90):
                poller.run_once(notify=False)
            state = watcher.load_state(path)
            # La date réelle de run_once est injectée explicitement pour la simulation.
            state["last_poll_at"] = "2026-11-14T11:59:00Z"
            self.assertIn("PR #10", self.route("/pr", state))
            self.assertIn("en attente", self.route("/ci", state))
            self.assertIn("Rapport " + REPORT, self.route("/rapports", state))


if __name__ == "__main__":
    unittest.main()
