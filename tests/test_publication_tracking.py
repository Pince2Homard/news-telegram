"""Suivi du reçu : GET seulement, sans publication distante ni authentification."""
import copy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.windows_watcher import publication_tracking as tracking, report_publication as publication, watcher, coordination

REPO = "Pince2Homard/news-telegram"
REPORT = "c8a8c1b7-b572-496b-87c7-5a2be256e609"
TASK = "11111111-1111-4111-8111-111111111111"
NOW = 1_800_000_000
URL = f"https://github.com/{REPO}/issues/7#issuecomment-123"


class Reader:
    def __init__(self, comment):
        self.comment = comment
        self.offline = False
        self.limited = False
        self.calls = []

    def get_json(self, path):
        self.calls.append(path)
        if self.limited:
            raise watcher.RateLimitError(NOW + 60)
        if self.offline:
            raise watcher.WatcherError("simulated network failure")
        if path.endswith("/issues/7"):
            return {"updated_at": "2027-01-15T08:00:00Z"}
        if path.endswith("/issues/comments/123"):
            return copy.deepcopy(self.comment)
        if path.endswith("/check-runs?per_page=100"):
            return {"check_runs": [{"status": "completed", "conclusion": "success"}], "total_count": 1}
        if path.endswith("/status?per_page=100"):
            return {"statuses": [], "total_count": 0}
        raise AssertionError("unexpected GET")

    def get_pages(self, path):
        self.calls.append(path)
        if self.offline:
            raise watcher.WatcherError("simulated network failure")
        if "/pulls?state=open" in path:
            return [{"number": 10, "head": {"sha": "b" * 40}}]
        if path.endswith("/issues/7/comments"):
            return [copy.deepcopy(self.comment)]
        return []


class ReceiptTrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.root = root
        report = {"envelope": {"schema_version": 1, "coordination_id": REPORT,
            "repository": REPO, "pull_request": 10, "head_sha": "a" * 40, "kind": "report",
            "status": "completed", "requested_action": "review_only", "reply_to": TASK,
            "expires_at": None, "evidence_urls": []}, "summary": "Essai factice, aucune action autorisée."}
        validation = {"schema_valid": True, "ids_and_sha_valid": True, "input_files_unchanged": True,
                      "execution": {"launches": 1, "exit_code": 0, "error": None}}
        watcher.save_state(root / "result.json", report)
        watcher.save_state(root / "validation.json", validation)
        folder = root / "outbox" / "report"
        self.manifest = publication.prepare(root / "result.json", root / "validation.json", folder, REPORT, TASK, "Pince2Homard")
        self.body = (folder / "comment.md").read_text(encoding="utf-8")
        self.remote = {"id": 123, "html_url": URL, "body": self.body, "user": {"login": "Pince2Homard"},
                       "issue_url": f"https://api.github.com/repos/{REPO}/issues/7"}
        self.path = folder / "receipt.json"
        watcher.save_state(self.path, publication.receipt(self.remote, self.manifest, self.body))
        event = watcher._event_from_comment(watcher.validate_config(watcher.default_config()),
            {"key": "issue:7", "type": "issue", "number": 7}, "comments", self.remote)
        self.state = watcher._empty_state()
        self.state["repository"] = REPO
        coordination.register(self.state, event)
        self.reader = Reader(self.remote)
        self.entry = tracking.anchor(self.path, REPORT, URL, self.manifest["approval_digest"], REPO)
        self.clock = patch.object(watcher.time, "time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def assess(self):
        return tracking.assess(self.entry, self.state, REPO, self.reader, NOW)

    def test_confirmed_import_and_cached_view_are_get_only(self):
        with patch.object(publication, "deliver", side_effect=AssertionError("never publish")), \
                patch("subprocess.run", side_effect=AssertionError("no CLI process")):
            proof = self.assess()
            self.state["publication_receipts"] = {REPORT: proof}
            self.assertEqual(tracking.view(self.state, REPORT, NOW)["status"], "confirmed")
        self.assertEqual(self.reader.calls, [f"/repos/{REPO}/issues/comments/123"])

    def test_modified_receipt_uuid_destination_or_content_is_never_valid(self):
        original = self.path.read_bytes()
        for case in ("uuid", "url", "digest", "body", "receipt_state"):
            with self.subTest(case=case):
                receipt = json.loads(original)
                if case == "uuid": receipt["report_id"] = TASK
                if case == "url": receipt["url"] = URL.replace("issues/7", "issues/10")
                if case == "digest": receipt["approval_digest"] = "0" * 64
                if case == "receipt_state": receipt["status"] = "in_flight"
                watcher.save_state(self.path, receipt)
                if case == "body":
                    self.path.with_name("comment.md").write_text("changed", encoding="utf-8")
                self.assertEqual(self.assess()["status"], "unverifiable")
                self.path.with_name("comment.md").write_bytes(self.body.encode())
                self.path.write_bytes(original)

    def test_remote_edit_wrong_author_or_wrong_issue_is_unverifiable(self):
        for field, value in (("body", "edited"), ("user", {"login": "other"}), ("issue_url", "other")):
            original = copy.deepcopy(self.reader.comment)
            self.reader.comment[field] = value
            self.assertEqual(self.assess()["reason"], "remote_divergence")
            self.reader.comment = original

    def test_duplicate_and_missing_registry_report_are_not_confirmed(self):
        self.state["coordination_registry"][REPORT]["references"].append(URL.replace("123", "456"))
        self.assertEqual(self.assess()["reason"], "registry_conflict_or_duplicate")
        self.state["coordination_registry"] = {}
        self.assertEqual(self.assess()["reason"], "report_not_in_registry")

    def test_network_ambiguity_and_expired_confirmation_never_stay_green(self):
        self.reader.offline = True
        self.assertEqual(self.assess()["status"], "uncertain")
        self.reader.offline = False
        self.state["publication_receipts"] = {REPORT: self.assess()}
        self.assertEqual(tracking.view(self.state, REPORT, NOW + 901)["reason"], "confirmation_expired")
        self.path.write_text("{}", encoding="utf-8")
        self.assertEqual(tracking.view(self.state, REPORT, NOW)["reason"], "local_receipt_changed")

    def test_local_report_without_receipt_and_invalid_anchor(self):
        self.assertEqual(tracking.view(self.state, REPORT, NOW)["status"], "local")
        for url in ("https://example.invalid/token", URL.replace("issues/7", "pull/10")):
            with self.assertRaises(ValueError):
                tracking.anchor(self.path, REPORT, url, self.manifest["approval_digest"], REPO)

    def test_watcher_import_refresh_preserves_cursors_and_local_ack(self):
        path = self.root / "watcher" / "state.json"
        poller = watcher.Watcher(watcher.default_config(), path, self.reader, lambda _: True)
        proof = poller.import_receipt(self.path, REPORT, URL, self.manifest["approval_digest"])
        self.assertEqual(proof["status"], "confirmed")
        state = watcher.load_state(path)
        retained = Path(state["publication_receipts"][REPORT]["receipt_path"])
        self.assertNotEqual(retained, self.path)
        self.assertEqual(retained.read_bytes(), self.path.read_bytes())
        # Le rapport factice est obsolète pour le code, mais sa publication existe.
        self.assertEqual(coordination.rows(state, REPO, NOW)[0]["condition"], "stale_sha")
        identity = state["coordination_registry"][REPORT]["event"]["event_id"]
        poller.acknowledge(identity, "done")
        cursors = copy.deepcopy(state["threads"])
        poller.run_once(notify=False)
        state = watcher.load_state(path)
        self.assertEqual(state["threads"], cursors)
        self.assertEqual(state["inbox"][0]["status"], "done")
        page = poller.write_dashboard().read_text(encoding="utf-8")
        self.assertIn("publication confirmée", page)
        self.assertIn(URL, page)
        self.assertIn("stale_sha", page)
        self.reader.offline = True
        with self.assertRaises(watcher.WatcherError): poller.run_once(notify=False)
        state = watcher.load_state(path)
        self.assertEqual(state["threads"], cursors)
        self.assertEqual(state["publication_receipts"][REPORT]["status"], "uncertain")
        self.reader.offline = False
        poller.run_once(notify=False)
        self.assertEqual(watcher.load_state(path)["publication_receipts"][REPORT]["status"], "confirmed")

    def test_rate_after_import_propagates_without_retry(self):
        self.reader.limited = True
        with self.assertRaises(watcher.RateLimitError): self.assess()
        self.assertEqual(len(self.reader.calls), 1)

    def writer(self):
        owner = self

        class Writer:
            posts = 0
            ambiguous = False
            published = False

            def call(self, method, path, payload=None):
                if method == "POST":
                    self.posts += 1
                    self.published = True
                    if self.ambiguous:
                        raise watcher.WatcherError("ambiguous")
                    return copy.deepcopy(owner.remote)
                if path == "/user":
                    return {"login": "Pince2Homard"}
                if path.endswith("/issues/7"):
                    return {"number": 7, "html_url": f"https://github.com/{REPO}/issues/7", "state": "open"}
                if "/issues/7/comments?" in path:
                    return [copy.deepcopy(owner.remote)] if self.published else []
                if "/issues/10/comments?" in path:
                    return []
                raise AssertionError("unexpected publication route")

        return Writer()

    def installation(self):
        root = self.root / "NewsTelegramWatcher"
        watcher.save_state(root / "config.json", watcher.default_config())
        watcher.save_state(root / "state.json", {**watcher._empty_state(), "initialized": True})
        return root

    def test_approved_publication_automatically_attaches_and_restart_is_get_only(self):
        self.path.unlink()
        writer, root = self.writer(), self.installation()
        result = publication.publish_and_track(self.path.parent, writer, root,
            self.manifest["approval_digest"], reader=self.reader)
        self.assertEqual(result["tracking"], "confirmed")
        self.assertEqual(writer.posts, 1)
        durable = root / "publication-receipts" / REPORT
        self.assertTrue((durable / "attachment.json").exists())
        poller = watcher.Watcher(watcher.default_config(), root / "state.json", self.reader)
        state = watcher.load_state(root / "state.json")
        poller.acknowledge(state["inbox"][0]["event_id"], "done")
        cursors = copy.deepcopy(state["threads"])
        # Disparition de l'outbox temporaire : la reprise n'en dépend plus.
        for path in self.path.parent.iterdir(): path.unlink()
        with patch.object(publication, "deliver", side_effect=AssertionError("no publication")):
            poller.run_once(notify=False)
            page = poller.write_dashboard().read_text(encoding="utf-8")
        self.assertIn("publication confirmée", page)
        self.assertIn(URL, page)
        state = watcher.load_state(root / "state.json")
        self.assertEqual(state["threads"], cursors)
        self.assertEqual(state["inbox"][0]["status"], "done")
        self.assertEqual(writer.posts, 1)

    def test_ambiguous_publication_requires_get_reconcile_before_attachment(self):
        self.path.unlink()
        writer, root = self.writer(), self.installation()
        writer.ambiguous = True
        with self.assertRaises(watcher.WatcherError):
            publication.publish_and_track(self.path.parent, writer, root, self.manifest["approval_digest"], reader=self.reader)
        self.assertFalse((root / "publication-receipts").exists())
        with self.assertRaises(watcher.WatcherError):
            publication.publish_and_track(self.path.parent, writer, root, self.manifest["approval_digest"], reader=self.reader)
        result = publication.publish_and_track(self.path.parent, writer, root, reconcile=True, reader=self.reader)
        self.assertEqual(result["tracking"], "confirmed")
        self.assertEqual(writer.posts, 1)

    def test_existing_receipt_is_idempotent_and_offline_attachment_resumes(self):
        writer, root = self.writer(), self.installation()
        self.reader.offline = True
        result = publication.publish_and_track(self.path.parent, writer, root, self.manifest["approval_digest"], reader=self.reader)
        self.assertEqual(result["publication"]["status"], "confirmed")
        self.assertEqual(result["tracking"], "pending")
        self.assertTrue((root / "publication-receipts" / REPORT / "attachment.json").exists())
        self.reader.offline = False
        result = publication.publish_and_track(self.path.parent, writer, root, reconcile=True, reader=self.reader)
        state = watcher.load_state(root / "state.json")
        self.assertEqual(result["tracking"], "confirmed")
        self.assertEqual(len(state["publication_receipts"]), 1)
        self.assertEqual(writer.posts, 0)

    def test_changed_or_contradictory_durable_bundle_cannot_replace_state(self):
        writer, root = self.writer(), self.installation()
        publication.publish_and_track(self.path.parent, writer, root, self.manifest["approval_digest"], reader=self.reader)
        poller = watcher.Watcher(watcher.default_config(), root / "state.json", self.reader)
        original = watcher.load_state(root / "state.json")
        metadata = root / "publication-receipts" / REPORT / "attachment.json"
        data = json.loads(metadata.read_text())
        data["approval_digest"] = "0" * 64
        watcher.save_state(metadata, data)
        with self.assertRaises(watcher.WatcherError): poller.run_once(notify=False)
        state = watcher.load_state(root / "state.json")
        self.assertEqual(state["threads"], original["threads"])
        self.assertEqual(state["publication_receipts"][REPORT]["approval_digest"], self.manifest["approval_digest"])
        data["approval_digest"] = self.manifest["approval_digest"]
        watcher.save_state(metadata, data)
        (metadata.parent / "comment.md").write_text("altered", encoding="utf-8")
        with self.assertRaises(watcher.WatcherError): poller.run_once(notify=False)
        self.assertEqual(writer.posts, 0)

    def test_missing_or_wrong_installation_blocks_before_post(self):
        self.path.unlink()
        writer, root = self.writer(), self.installation()
        config = watcher.default_config()
        config["repository"] = "other/repository"
        watcher.save_state(root / "config.json", config)
        with self.assertRaises(watcher.WatcherError):
            publication.publish_and_track(self.path.parent, writer, root, self.manifest["approval_digest"], reader=self.reader)
        self.assertEqual(writer.posts, 0)

    def test_cli_uses_automatic_attachment_without_manual_identifiers(self):
        self.path.unlink()
        writer, root = self.writer(), self.installation()
        with patch.object(publication, "GitHubCLI", return_value=writer), \
                patch.object(watcher, "GitHubReader", return_value=self.reader), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = publication.main(["publish", "--prepared", str(self.path.parent),
                "--approve-digest", self.manifest["approval_digest"], "--watcher-root", str(root)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["tracking"], "confirmed")
        self.assertEqual(writer.posts, 1)

    def test_local_storage_failure_never_disguises_confirmed_post_or_retries(self):
        self.path.unlink()
        writer, root = self.writer(), self.installation()
        with patch.object(tracking, "stage_confirmed", side_effect=OSError("disk unavailable")):
            result = publication.publish_and_track(self.path.parent, writer, root,
                self.manifest["approval_digest"], reader=self.reader)
        self.assertEqual(result["publication"]["status"], "confirmed")
        self.assertFalse(result["receipt_durable"])
        self.assertEqual(result["tracking"], "pending")
        result = publication.publish_and_track(self.path.parent, writer, root, reconcile=True, reader=self.reader)
        self.assertEqual(result["tracking"], "confirmed")
        self.assertEqual(writer.posts, 1)
