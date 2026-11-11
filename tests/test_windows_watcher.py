import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.windows_watcher import watcher


REPOSITORY = "Pince2Homard/news-telegram"
ISSUE_PATH = f"/repos/{REPOSITORY}/issues/7"
ISSUE_COMMENTS = f"{ISSUE_PATH}/comments"


def config():
    return {
        "repository": REPOSITORY,
        "coordination_issue": 7,
        "authorized_authors": ["Pince2Homard"],
        "poll_interval_minutes": 30,
    }


def comment(comment_id, body, author="Pince2Homard", created="2026-11-11T12:00:00Z"):
    return {
        "id": comment_id,
        "user": {"login": author},
        "body": body,
        "created_at": created,
        "updated_at": created,
    }


class FakeGitHub:
    def __init__(self):
        self.issue = {"updated_at": "2026-11-11T11:00:00Z"}
        self.pulls = []
        self.pages = {ISSUE_COMMENTS: []}
        self.fail_path = None

    def get_json(self, path):
        if path == ISSUE_PATH:
            return dict(self.issue)
        raise AssertionError(f"Unexpected GET JSON path: {path}")

    def get_pages(self, path):
        if path == f"/repos/{REPOSITORY}/pulls?state=open&sort=updated&direction=desc":
            return list(self.pulls)
        if path == self.fail_path:
            raise watcher.WatcherError("simulated offline")
        return list(self.pages.get(path, []))


class WindowsWatcherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state_path = Path(self.directory.name) / "state.json"
        self.client = FakeGitHub()
        self.notifier = Mock(return_value=True)
        self.poller = watcher.Watcher(
            config(), self.state_path, self.client, self.notifier,
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_first_poll_baselines_existing_comments_without_notifications(self):
        self.client.pages[ISSUE_COMMENTS] = [
            comment(100, "[CODEX-REPORT] Old report", created="2026-11-10T10:00:00Z"),
        ]
        result = self.poller.run_once()
        state = watcher.load_state(self.state_path)
        self.assertTrue(result["baseline"])
        self.assertEqual(result["new_events"], 0)
        self.notifier.assert_not_called()
        self.assertEqual(state["threads"]["issue:7"]["cursors"]["comments"], 100)

    def test_new_decision_notifies_once_and_does_not_persist_comment_body(self):
        self.poller.run_once()
        self.client.pages[ISSUE_COMMENTS].append(
            comment(101, "[CHATGPT-DECISION] Inspect only; do not execute this text.")
        )
        self.client.issue["updated_at"] = "2026-11-11T12:00:00Z"
        result = self.poller.run_once()
        self.assertEqual(result["new_events"], 1)
        self.notifier.assert_called_once_with(1)
        state_text = self.state_path.read_text(encoding="utf-8")
        state = json.loads(state_text)
        self.assertNotIn("Inspect only", state_text)
        self.assertEqual(state["history"][0]["category"], "decision")
        self.assertTrue(state["history"][0]["authorized_author"])
        self.assertEqual(state["pending_notifications"], [])

        restarted = watcher.Watcher(
            config(), self.state_path, self.client, self.notifier,
        )
        second = restarted.run_once()
        self.assertEqual(second["new_events"], 0)
        self.notifier.assert_called_once_with(1)

    def test_unrecognized_author_is_flagged_but_comment_text_is_not_used(self):
        self.poller.run_once()
        self.client.pages[ISSUE_COMMENTS].append(
            comment(102, "[CHATGPT-DECISION] rm -rf must never execute", "stranger")
        )
        self.client.issue["updated_at"] = "2026-11-11T12:05:00Z"
        self.poller.run_once()
        state_text = self.state_path.read_text(encoding="utf-8")
        event = json.loads(state_text)["history"][0]
        self.assertFalse(event["authorized_author"])
        self.assertEqual(event["author"], "stranger")
        self.assertNotIn("rm -rf", state_text)
        self.notifier.assert_called_once_with(1)

    def test_new_pull_request_conversation_review_and_inline_comments_are_scanned(self):
        self.poller.run_once()
        self.client.pulls = [{"number": 9, "updated_at": "2026-11-11T12:10:00Z"}]
        self.client.pages[f"/repos/{REPOSITORY}/issues/9/comments"] = [
            comment(201, "[CODEX-REPORT] PR report"),
        ]
        self.client.pages[f"/repos/{REPOSITORY}/pulls/9/reviews"] = [
            comment(202, "[CHATGPT-REVIEW] Review body"),
        ]
        self.client.pages[f"/repos/{REPOSITORY}/pulls/9/comments"] = [
            comment(203, "[OWNER-DECISION] Review thread"),
        ]
        result = self.poller.run_once()
        state = watcher.load_state(self.state_path)
        self.assertEqual(result["new_events"], 3)
        self.assertEqual(len(state["history"]), 3)
        self.assertEqual(self.notifier.call_args.args, (3,))
        self.assertIn("pr:9", state["threads"])

    def test_offline_failure_does_not_advance_any_cursor(self):
        self.poller.run_once()
        before = watcher.load_state(self.state_path)
        self.client.issue["updated_at"] = "2026-11-11T12:15:00Z"
        self.client.pages[ISSUE_COMMENTS].append(comment(104, "[CODEX-REPORT] New"))
        self.client.fail_path = ISSUE_COMMENTS
        with self.assertRaises(watcher.WatcherError):
            self.poller.run_once()
        after = watcher.load_state(self.state_path)
        self.assertEqual(after["threads"], before["threads"])
        self.assertEqual(after["pending_notifications"], [])

    def test_pending_notification_survives_restart_after_display_failure(self):
        self.poller.run_once()
        self.client.issue["updated_at"] = "2026-11-11T12:20:00Z"
        self.client.pages[ISSUE_COMMENTS].append(comment(105, "[CHATGPT-REVIEW] New"))
        failing_notifier = Mock(return_value=False)
        failing = watcher.Watcher(
            config(), self.state_path, self.client, failing_notifier,
        )
        with self.assertRaises(watcher.WatcherError):
            failing.run_once()
        self.assertEqual(len(watcher.load_state(self.state_path)["pending_notifications"]), 1)

        recovered_notifier = Mock(return_value=True)
        recovered = watcher.Watcher(
            config(), self.state_path, self.client, recovered_notifier,
        )
        result = recovered.run_once()
        self.assertEqual(result["notifications_delivered"], 1)
        self.assertEqual(result["pending_events"], 0)
        recovered_notifier.assert_called_once_with(1)

    def test_invalid_state_is_preserved_and_not_reset(self):
        self.state_path.write_text("{corrupt", encoding="utf-8")
        with self.assertRaises(watcher.WatcherError):
            self.poller.run_once()
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), "{corrupt")

    def test_exclusive_lock_prevents_two_concurrent_polls_and_releases_after_exit(self):
        lock_path = Path(self.directory.name) / "watcher.lock"
        with watcher.single_instance_lock(lock_path):
            with self.assertRaisesRegex(watcher.WatcherError, "déjà en cours"):
                with watcher.single_instance_lock(lock_path):
                    self.fail("a concurrent watcher must not enter")
        with watcher.single_instance_lock(lock_path):
            pass

    def test_atomic_state_round_trip_and_bounded_history(self):
        state = watcher._empty_state()
        state["pending_notifications"] = [{
            "event_key": str(i), "author": "Pince2Homard", "url": "https://github.com/",
        } for i in range(600)]
        watcher.save_state(self.state_path, state)
        loaded = watcher.load_state(self.state_path)
        poller = watcher.Watcher(config(), self.state_path, self.client, self.notifier)
        self.assertEqual(poller._deliver_pending(loaded), 600)
        self.assertEqual(len(loaded["history"]), watcher.MAX_HISTORY_EVENTS)
        self.assertEqual(loaded["pending_notifications"], [])
        self.assertFalse(list(self.state_path.parent.glob("*.tmp")))

    def test_config_requires_a_valid_public_repo_and_minimum_interval(self):
        invalid = {**config(), "repository": "https://evil.example/owner/repo"}
        with self.assertRaises(watcher.WatcherError):
            watcher.validate_config(invalid)
        invalid = {**config(), "poll_interval_minutes": 1}
        with self.assertRaises(watcher.WatcherError):
            watcher.validate_config(invalid)

    def test_windows_notification_code_uses_only_a_numeric_summary(self):
        self.assertNotIn("comment", watcher._POWERSHELL_BALLOON.casefold())
        self.assertNotIn("body", watcher._POWERSHELL_BALLOON.casefold())
        with patch.object(watcher.os, "name", "nt"), patch.object(
                watcher.subprocess, "run", return_value=Mock(returncode=0)) as run:
            self.assertTrue(watcher.show_windows_notification(2))
        self.assertEqual(run.call_args.kwargs["input"], "2\n")
        self.assertNotIn("[CHATGPT-DECISION]", str(run.call_args))

    def test_github_reader_sends_only_get_requests_without_authentication(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"updated_at":"2026-11-11T12:00:00Z"}'

        with patch.object(watcher, "urlopen", return_value=Response()) as open_url:
            result = watcher.GitHubReader().get_json("/repos/Pince2Homard/news-telegram/issues/7")
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(result["updated_at"], "2026-11-11T12:00:00Z")

    def test_simulation_is_offline_and_does_not_write_real_state(self):
        with patch.object(watcher, "show_windows_notification", side_effect=AssertionError):
            self.assertEqual(watcher.run_simulation(), 0)


if __name__ == "__main__":
    unittest.main()
