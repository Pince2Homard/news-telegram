"""Retour de rapport : aucune écriture GitHub réelle dans les tests."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.windows_watcher import report_publication as publication, coordination, watcher

REPORT_ID = "c8a8c1b7-b572-496b-87c7-5a2be256e609"
TASK_ID = "11111111-1111-4111-8111-111111111111"
REPO = "Pince2Homard/news-telegram"


class GitHub:
    def __init__(self):
        self.comments = {7: [], 10: []}
        self.posts = 0
        self.author = "Pince2Homard"
        self.ambiguous = False
        self.accept = True
        self.offline = False

    def call(self, method, path, payload=None):
        if self.offline:
            raise watcher.WatcherError("offline")
        if path == "/user":
            return {"login": self.author}
        if path == f"/repos/{REPO}/issues/7":
            return {"number": 7, "html_url": f"https://github.com/{REPO}/issues/7", "state": "open", "locked": False}
        if method == "POST":
            self.posts += 1
            response = {"id": 123, "html_url": f"https://github.com/{REPO}/issues/7#issuecomment-123",
                        "issue_url": f"https://api.github.com/repos/{REPO}/issues/7",
                        "user": {"login": self.author}, "body": payload["body"]}
            if self.accept:
                self.comments[7].append(response)
            if self.ambiguous:
                raise watcher.WatcherError("connection lost")
            return response
        for number in (7, 10):
            if path.startswith(f"/repos/{REPO}/issues/{number}/comments?"):
                return self.comments[number]
        if path == f"/repos/{REPO}/issues/comments/123":
            return self.comments[7][0]
        raise AssertionError("unexpected route")


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "outbox" / "preview"
        self.report = {"envelope": {"schema_version": 1, "coordination_id": REPORT_ID,
            "repository": REPO, "pull_request": 10, "head_sha": "a" * 40, "kind": "report",
            "status": "completed", "requested_action": "review_only", "reply_to": TASK_ID,
            "expires_at": None, "evidence_urls": []}, "summary": "Analyse factice terminée."}
        self.validation = {"schema_valid": True, "ids_and_sha_valid": True,
                           "input_files_unchanged": True, "execution": {"launches": 1, "exit_code": 0, "error": None}}
        self.api = GitHub()

    def prepare(self):
        watcher.save_state(self.root / "result.json", self.report)
        watcher.save_state(self.root / "validation.json", self.validation)
        return publication.prepare(self.root / "result.json", self.root / "validation.json", self.directory,
                                   REPORT_ID, TASK_ID, "Pince2Homard")

    def test_prepare_offline_and_protocol_round_trip(self):
        with patch("subprocess.run", side_effect=AssertionError("no network")):
            manifest = self.prepare()
        body = (self.directory / "comment.md").read_text(encoding="utf-8")
        self.assertIn("ESSAI FACTICE", body)
        envelope, error = coordination.parse_envelope(body, REPO)
        self.assertIsNone(error)
        self.assertEqual(envelope, self.report["envelope"])
        self.assertEqual(manifest["issue"], 7)
        self.assertNotIn("validation_method", body)
        self.assertFalse((self.directory / "delivery.json").exists())

    def test_schema_ids_secrets_and_size_are_rejected(self):
        for case in ("extra", "id", "sha", "reply", "url", "secret", "size", "validation"):
            with self.subTest(case=case):
                original = copy.deepcopy(self.report)
                if case == "extra": self.report["runtime"] = {}
                if case == "id": self.report["envelope"]["coordination_id"] = TASK_ID
                if case == "sha": self.report["envelope"]["head_sha"] = "b" * 40
                if case == "reply": self.report["envelope"]["reply_to"] = REPORT_ID
                if case == "url": self.report["envelope"]["evidence_urls"] = ["https://example.invalid/private"]
                if case == "secret": self.report["summary"] = "github_pat_synthetic"
                if case == "size": self.report["summary"] = "é" * 8000
                if case == "validation": self.validation["schema_valid"] = False
                with self.assertRaises(watcher.WatcherError): self.prepare()
                self.report = original
        self.assertFalse(self.directory.exists())

    def test_explicit_digest_and_exact_identity_required(self):
        manifest = self.prepare()
        with self.assertRaises(watcher.WatcherError): publication.deliver(self.directory, self.api)
        with self.assertRaises(watcher.WatcherError): publication.deliver(self.directory, self.api, "wrong")
        self.api.author = "other"
        with self.assertRaises(watcher.WatcherError): publication.deliver(self.directory, self.api, manifest["approval_digest"])
        self.assertEqual(self.api.posts, 0)

    def test_changed_body_cannot_reuse_approval(self):
        manifest = self.prepare()
        (self.directory / "comment.md").write_text("changed", encoding="utf-8")
        with self.assertRaises(watcher.WatcherError): publication.deliver(self.directory, self.api, manifest["approval_digest"])
        self.assertEqual(self.api.posts, 0)

    def test_single_post_receipt_and_link_consultation(self):
        manifest = self.prepare()
        saved = publication.deliver(self.directory, self.api, manifest["approval_digest"])
        self.assertEqual(self.api.posts, 1)
        self.assertEqual(saved, publication.deliver(self.directory, self.api, manifest["approval_digest"]))
        self.assertEqual(self.api.posts, 1)
        remote = self.api.call("GET", f"/repos/{REPO}/issues/comments/{saved['comment_id']}")
        self.assertEqual(remote["html_url"], saved["url"])
        self.assertEqual(coordination.parse_envelope(remote["body"], REPO)[0]["coordination_id"], REPORT_ID)

    def test_existing_exact_comment_is_recovered_without_post(self):
        manifest = self.prepare()
        body = (self.directory / "comment.md").read_text(encoding="utf-8")
        self.api.call("POST", "unused", {"body": body})
        self.api.posts = 0
        saved = publication.deliver(self.directory, self.api, manifest["approval_digest"])
        self.assertEqual(saved["comment_id"], 123)
        self.assertEqual(self.api.posts, 0)

    def test_conflicting_or_cross_destination_duplicate_blocks(self):
        manifest = self.prepare()
        body = (self.directory / "comment.md").read_text(encoding="utf-8")
        response = self.api.call("POST", "unused", {"body": body})
        self.api.comments[7] = []
        self.api.comments[10] = [dict(response, html_url=f"https://github.com/{REPO}/pull/10#issuecomment-123")]
        self.api.posts = 0
        with self.assertRaises(watcher.WatcherError): publication.deliver(self.directory, self.api, manifest["approval_digest"])
        self.assertEqual(self.api.posts, 0)

    def test_ambiguous_send_blocks_retry_and_reconciles_get_only(self):
        manifest = self.prepare()
        self.api.ambiguous = True
        with self.assertRaises(watcher.WatcherError): publication.deliver(self.directory, self.api, manifest["approval_digest"])
        self.assertEqual(self.api.posts, 1)
        with self.assertRaises(watcher.WatcherError): publication.deliver(self.directory, self.api, manifest["approval_digest"])
        saved = publication.deliver(self.directory, self.api, reconcile=True)
        self.assertEqual(saved["status"], "confirmed")
        self.assertEqual(self.api.posts, 1)

    def test_absence_after_ambiguous_failure_does_not_authorize_resend(self):
        manifest = self.prepare()
        self.api.ambiguous, self.api.accept = True, False
        with self.assertRaises(watcher.WatcherError): publication.deliver(self.directory, self.api, manifest["approval_digest"])
        with self.assertRaises(watcher.WatcherError): publication.deliver(self.directory, self.api, reconcile=True)
        with self.assertRaises(watcher.WatcherError): publication.deliver(self.directory, self.api, manifest["approval_digest"])
        self.assertEqual(self.api.posts, 1)

    def test_receipt_save_failure_leaves_durable_intent(self):
        manifest = self.prepare()
        original = watcher.save_state
        def save(path, data):
            if path.name == "receipt.json": raise OSError("disk unavailable")
            return original(path, data)
        with patch.object(watcher, "save_state", side_effect=save):
            with self.assertRaises(watcher.WatcherError): publication.deliver(self.directory, self.api, manifest["approval_digest"])
        self.assertEqual(publication.load_json(self.directory / "delivery.json")["status"], "in_flight")
        with self.assertRaises(watcher.WatcherError): publication.deliver(self.directory, self.api, manifest["approval_digest"])
        self.assertEqual(publication.deliver(self.directory, self.api, reconcile=True)["status"], "confirmed")
        self.assertEqual(self.api.posts, 1)

    def test_cli_adapter_one_post_stdin_no_shell_no_raw_errors(self):
        with patch.object(publication.shutil, "which", return_value="gh.exe"), \
                patch.object(publication.subprocess, "run") as run:
            run.return_value.returncode = 1
            run.return_value.stderr = b"Bearer private-token"
            client = publication.GitHubCLI()
            with self.assertRaises(watcher.WatcherError) as error:
                client.call("POST", f"/repos/{REPO}/issues/7/comments", {"body": "synthetic"})
            self.assertNotIn("private-token", str(error.exception))
            run.assert_called_once()
            self.assertNotIn("shell", run.call_args.kwargs)
            self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"body": "synthetic"})
            with self.assertRaises(watcher.WatcherError): client.call("POST", f"/repos/{REPO}/issues/10/comments", {})
