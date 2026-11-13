import json
from contextlib import redirect_stdout
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError
from xml.etree import ElementTree as ET

from tools.windows_watcher import manage, watcher


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

    def add_event(self):
        self.poller.run_once()
        self.client.pages[ISSUE_COMMENTS] = [comment(101, "[CODEX-REPORT] private body")]
        self.client.issue["updated_at"] = "2026-11-11T12:00:00Z"
        self.poller.run_once()
        return self.poller.events()[0]["event_id"]

    def test_same_second_edit_and_prefix_addition_are_detected_once(self):
        self.poller.run_once()
        item = comment(101, "ordinary comment")
        self.client.pages[ISSUE_COMMENTS] = [item]
        self.poller.run_once()
        item["body"] = "[CODEX-REPORT] changed without metadata update"
        self.assertEqual(self.poller.run_once()["new_events"], 1)
        item["body"] = "[CODEX-REPORT] second edit, same timestamp"
        self.assertEqual(self.poller.run_once()["new_events"], 1)
        self.assertEqual(self.poller.run_once()["new_events"], 0)
        self.assertEqual(len(self.poller.events()), 2)

    def test_notification_is_not_acknowledgment_and_ack_survives_restart(self):
        identity = self.add_event()
        self.assertEqual(self.poller.events()[0]["status"], "new")
        self.poller.acknowledge(identity, "acknowledged")
        self.poller.acknowledge(identity, "done")
        self.poller.run_once()
        self.assertEqual(self.poller.events()[0]["status"], "done")
        self.assertNotIn("private body", self.state_path.read_text(encoding="utf-8"))
        with self.assertRaises(watcher.WatcherError):
            self.poller.acknowledge(identity, "authorized")
        with self.assertRaises(watcher.WatcherError):
            self.poller.acknowledge("unknown", "done")

    def test_empty_list_and_legacy_history_are_supported(self):
        self.assertEqual(self.poller.events(), [])
        self.add_event()
        state = watcher.load_state(self.state_path)
        state.pop("inbox")
        watcher.save_state(self.state_path, state)
        self.assertEqual(len(self.poller.events()), 1)
        self.assertEqual(self.poller.events()[0]["status"], "new")

    def test_list_and_ack_commands_are_local_and_export_links(self):
        identity = self.add_event()
        config_path = self.state_path.with_name("config.json")
        watcher.save_state(config_path, config())
        output = io.StringIO()
        with patch.object(watcher, "urlopen", side_effect=AssertionError("no network")), \
                patch.object(watcher, "_configure_logging"), redirect_stdout(output):
            self.assertEqual(watcher.main(["--config", str(config_path), "--list", "--json"]), 0)
            events = json.loads(output.getvalue())
            self.assertEqual(events[0]["event_id"], identity)
            self.assertIn("github.com/Pince2Homard/news-telegram/issues/7", events[0]["url"])
            self.assertNotIn("private body", output.getvalue())
            self.assertEqual(watcher.main(["--config", str(config_path), "--ack", identity,
                                           "--status", "done"]), 0)
        self.assertEqual(self.poller.events()[0]["status"], "done")

    def test_unprocessed_inbox_full_stops_without_advancing_cursors(self):
        self.add_event()
        before = self.state_path.read_bytes()
        self.client.pages[ISSUE_COMMENTS].append(comment(102, "[CODEX-REPORT] second"))
        with patch.object(watcher, "MAX_PENDING_EVENTS", 1):
            with self.assertRaises(watcher.WatcherError):
                self.poller.run_once()
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_rate_limit_persists_deadline_without_partial_cursors(self):
        self.poller.run_once()
        before = watcher.load_state(self.state_path)["threads"]
        with patch.object(self.client, "get_pages", side_effect=watcher.RateLimitError(160)), \
                patch.object(watcher.time, "time", return_value=100):
            with self.assertRaises(watcher.RateLimitError):
                self.poller.run_once()
        self.assertEqual(watcher.load_state(self.state_path)["threads"], before)
        with patch.object(self.client, "get_json") as read, \
                patch.object(watcher.time, "time", return_value=150):
            with self.assertRaises(watcher.RateLimitError):
                self.poller.run_once()
            read.assert_not_called()
        with patch.object(watcher.time, "time", return_value=161):
            self.poller.run_once()
        self.assertNotIn("next_poll_at", watcher.load_state(self.state_path))

    def test_http_limits_honor_retry_after_reset_and_do_not_retry(self):
        reader = watcher.GitHubReader(clock=lambda: 100)
        for code in (403, 429):
            error = HTTPError("https://api.github.com/", code, "private", {
                "Retry-After": "120", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "500",
            }, None)
            with patch.object(watcher, "urlopen", side_effect=error) as read:
                with self.assertRaises(watcher.RateLimitError) as raised:
                    reader.get_json(ISSUE_PATH)
                self.assertEqual(raised.exception.retry_at, 501)
                self.assertNotIn("private", str(raised.exception))
                read.assert_called_once()
        self.assertEqual(watcher._retry_at({}, 100), 160)
        self.assertEqual(watcher._retry_at({"Retry-After": "Thu, 01 Jan 1970 00:05:00 GMT"}, 100), 300)

    def test_success_exhausting_quota_blocks_next_request(self):
        reader = watcher.GitHubReader(clock=lambda: 100)
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "500"}
        response.read.return_value = b'{}'
        with patch.object(watcher, "urlopen", return_value=response) as read:
            reader.get_json(ISSUE_PATH)
            with self.assertRaises(watcher.RateLimitError):
                reader.get_json(ISSUE_PATH)
            read.assert_called_once()

    def test_closed_pull_gets_one_final_scan(self):
        self.client.pulls = [{"number": 9, "updated_at": "2026-11-11T12:00:00Z"}]
        self.poller.run_once()
        self.client.pulls = []
        self.client.pages[f"/repos/{REPOSITORY}/issues/9/comments"] = [comment(200, "[CODEX-REPORT] final")]
        original = self.client.get_json
        with patch.object(self.client, "get_json", side_effect=lambda path: (
            {"updated_at": "2026-11-11T13:00:00Z"} if path.endswith("/pulls/9") else original(path)
        )):
            self.assertEqual(self.poller.run_once()["new_events"], 1)
        self.assertEqual(self.poller.run_once()["new_events"], 0)

    def test_management_install_check_prepare_and_uninstall_are_offline(self):
        root = Path(self.directory.name) / "NewsTelegramWatcher"
        with patch.object(manage.subprocess, "run", side_effect=AssertionError("no task command")), \
                patch.object(watcher, "urlopen", side_effect=AssertionError("no network")):
            manage.install(root)
            self.assertTrue(manage.diagnose(root)["ok"])
            config_bytes = (root / "config.json").read_bytes()
            watcher.save_state(root / "state.json", watcher._empty_state())
            state_bytes = (root / "state.json").read_bytes()
            manage.install(root)
            with patch.dict("os.environ", {"USERNAME": "synthetic", "USERDOMAIN": "local"}):
                result = manage.prepare_task(root)
            self.assertFalse(result["task_registered"])
            xml = ET.parse(root / "task.disabled.xml")
            ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
            self.assertEqual([e.text for e in xml.findall(".//t:Enabled", ns)], ["false", "false"])
            self.assertEqual(xml.find(".//t:RunLevel", ns).text, "LeastPrivilege")
            self.assertIn("--once", xml.find(".//t:Arguments", ns).text)
            unknown = root / "app" / "keep.txt"
            unknown.write_text("keep", encoding="utf-8")
            manage.uninstall(root)
            self.assertTrue(unknown.exists())
            self.assertEqual((root / "config.json").read_bytes(), config_bytes)
            self.assertEqual((root / "state.json").read_bytes(), state_bytes)

    def test_management_rejects_unsafe_root_and_preserves_corrupt_config(self):
        with self.assertRaises(watcher.WatcherError):
            manage.uninstall(Path(self.directory.name))
        root = Path(self.directory.name) / "NewsTelegramWatcher"
        manage.install(root)
        path = root / "config.json"
        path.write_text("{broken", encoding="utf-8")
        self.assertFalse(manage.diagnose(root)["ok"])
        with self.assertRaises(watcher.WatcherError):
            manage.install(root)
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")


if __name__ == "__main__":
    unittest.main()
