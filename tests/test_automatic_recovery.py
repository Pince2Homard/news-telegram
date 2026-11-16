import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from newsbot import persistence, publication
from tools.persistence_health import probe, recovery_required, synchronize, STEP


class AutomaticRecoveryTests(unittest.TestCase):
    def test_strict_response_matrix(self):
        good = {"ok": True, "result": {"message_id": 7, "date": 1000, "chat": {"id": 42}}}
        bad = [None, {}, {"ok": False}, {"ok": True, "result": {}}, []]
        for field, value in (("message_id", True), ("message_id", 0),
                             ("date", float("inf"))):
            case = copy.deepcopy(good)
            case["result"][field] = value
            bad.append(case)
        for payload in [good, *bad]:
            with self.subTest(payload=payload), patch("builtins.print"):
                response = Mock(status_code=200)
                response.json.return_value = payload
                http = Mock()
                http.post.return_value = response
                result = publication.send_message({"text": "synthetic"}, "fake", "42",
                                                  http=http, clock=lambda: 1001)
                self.assertEqual(bool(result), payload == good)
                self.assertEqual(result.disposition, "confirmed" if payload == good else "uncertain")
                self.assertFalse(http.post.call_args.kwargs["allow_redirects"])
                http.post.assert_called_once()

    def test_truncated_and_timeout_are_uncertain(self):
        for error in (ValueError("truncated"), requests.Timeout("synthetic")):
            http = Mock()
            if isinstance(error, requests.Timeout):
                http.post.side_effect = error
            else:
                http.post.return_value.status_code = 200
                http.post.return_value.json.side_effect = error
            with patch("builtins.print"):
                result = publication.send_message({"text": "synthetic"}, "fake", "42", http=http)
            self.assertFalse(result)
            self.assertEqual(result.disposition, "uncertain")
            http.post.assert_called_once()

    def test_restart_delays_and_exhaustion_then_next_article(self):
        with tempfile.TemporaryDirectory() as tmp, patch("builtins.print"):
            path = Path(tmp) / "fixture.json"
            state = {"file": [{"text": "first", "link": "https://example.org/1"},
                              {"text": "second", "link": "https://example.org/2"}], "vus": []}
            sender = Mock(return_value=publication.TelegramSendResult(False))
            now = [1000]
            for count, expected in ((1, 1900), (2, 3700), (3, None)):
                publication.process_queue(state, {}, "fake", "42", state_path=path,
                                          dry_run=False, sender=sender, clock=lambda: now[0])
                self.assertEqual(sender.call_count, count)
                state = persistence.load_state(path)
                if expected:
                    self.assertEqual(state["file"][0]["delivery_retry"]["not_before"], expected)
                    publication.process_queue(state, {}, "fake", "42", state_path=path,
                                              dry_run=False, sender=sender, clock=lambda: expected-1)
                    self.assertEqual(sender.call_count, count)
                    now[0] = expected
            self.assertEqual(state["file"][0]["text"], "second")
            self.assertTrue(state["erreurs"][0]["possible_delivery"])
            self.assertEqual(state["erreurs"][0]["item"]["delivery_retry"]["uncertain_count"], 3)
            sender.return_value = True
            now[0] += 900
            publication.process_queue(state, {}, "fake", "42", state_path=path,
                                      dry_run=False, sender=sender, clock=lambda: now[0])
            self.assertEqual(state["file"], [])

    def test_save_retries_only_frozen_checkpoint(self):
        state = {"file": [{"text": "synthetic"}]}
        original = copy.deepcopy(state)
        sleep = Mock()
        with patch.object(persistence, "_write_state", side_effect=[OSError(), OSError(), None]) as write:
            persistence.save_state(state, {}, "unused", sleep=sleep)
        self.assertEqual(write.call_count, 3)
        self.assertEqual(sleep.call_args_list[0].args, (1,))
        self.assertEqual(state["file"], original["file"])
        with patch.object(persistence, "_write_state", side_effect=OSError()), self.assertRaises(OSError):
            persistence.save_state(original, {}, "unused", sleep=Mock())
        self.assertEqual(original, {"file": [{"text": "synthetic"}]})

    def test_state_invalid_and_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.json"
            with self.assertRaises(ValueError):
                persistence.load_state(path, allow_new=False)
            for state in ({"vus": ["active"], "init": True}, {"file": [], "init": True},
                          {"file": [], "v": 3}, {"file": [None]},
                          {"file": [], "vus": [{}]}):
                raw = json.dumps(state)
                path.write_text(raw)
                with self.assertRaises(ValueError):
                    persistence.load_state(path)
                self.assertEqual(path.read_text(), raw)
            path.write_text('{broken')
            with self.assertRaises(json.JSONDecodeError):
                persistence.load_state(path)
            old = {"file": [{"text": "old", "pub": "", "link": "https://example.org/old"}], "vus": ["old"], "v": 1}
            path.write_text(json.dumps(old))
            self.assertEqual(persistence.load_state(path)["file"], old["file"])

    def test_recovery_gate_success_failure_missing_and_rerun(self):
        previous = {"id": 1, "status": "completed", "conclusion": "success",
                    "updated_at": "2026-11-16T10:00:00Z", "run_attempt": 1}
        jobs = {"jobs": [{"conclusion": "success", "steps": [{"name": STEP, "conclusion": "success"}]}]}
        for conclusion in ("success", "failure", "cancelled"):
            previous["conclusion"] = conclusion
            get = Mock(side_effect=[{"workflow_runs": [previous]}, jobs])
            self.assertEqual(recovery_required("example/repo", 2, 1, get), conclusion != "success")
        self.assertTrue(recovery_required("example/repo", 2, 2, Mock()))
        self.assertTrue(recovery_required("example/repo", 2, 1, Mock(return_value={"workflow_runs": []})))
        self.assertTrue(recovery_required("example/repo", 2, 1, Mock(side_effect=requests.Timeout())))

    def test_probe_keeps_queue_and_becomes_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.json"
            state = {"file": [{"text": "synthetic", "link": "https://example.org"}], "vus": ["seen"]}
            path.write_text(json.dumps(state))
            probe(path, 12, 1)
            updated = persistence.load_state(path)
            self.assertEqual(updated["file"], state["file"])
            self.assertEqual(updated["persistence_check"], {"run_id": "12", "attempt": 1})

    def test_sync_conflict_never_merges(self):
        answers = ["base", "old", "candidate", "state.json", "", "", "", "remote", "other"]
        run = Mock(side_effect=answers)
        with patch("tools.persistence_health.load_state"), self.assertRaises(ValueError):
            synchronize(run)
        self.assertFalse(any("push" in c.args[0] or "rebase" in c.args[0] for c in run.call_args_list))

    def test_sync_failed_push_is_verified_after_fetch(self):
        answers = ["base", "old", "candidate", "state.json", "", "", "", "base", "old",
                   "candidate", "state.json", subprocess.CalledProcessError(1, "git"),
                   "", "remote", "candidate"]
        run = Mock(side_effect=answers)
        with patch("tools.persistence_health.load_state"):
            synchronize(run)
        self.assertEqual(sum("push" in c.args[0] for c in run.call_args_list), 1)

    def test_long_rate_limit_is_rejected_and_deferred_without_clamping(self):
        http = Mock()
        http.post.return_value = Mock(status_code=429)
        http.post.return_value.json.return_value = {
            "ok": False, "error_code": 429, "parameters": {"retry_after": 120}}
        sleeper = Mock()
        result = publication.send_message({"text": "synthetic"}, "fake", "42",
                                          http=http, sleep=sleeper)
        self.assertEqual(result.disposition, "rejected")
        self.assertEqual(result.retry_after, 120)
        http.post.assert_called_once()
        sleeper.assert_not_called()

    def test_unexpected_destination_stops_globally(self):
        http = Mock()
        http.post.return_value = Mock(status_code=200)
        http.post.return_value.json.return_value = {
            "ok": True, "result": {"message_id": 1, "date": 1000, "chat": {"id": 99}}}
        with self.assertRaises(publication.GlobalTelegramError):
            publication.send_message({"text": "synthetic"}, "fake", "42", http=http)

    def test_transient_post_send_save_failure_does_not_resend(self):
        with tempfile.TemporaryDirectory() as tmp, patch("builtins.print"):
            path = Path(tmp) / "fixture.json"
            state = {"file": [{"text": "synthetic", "link": "https://example.org"}], "vus": []}
            original = persistence._write_state
            calls = []

            def write(candidate, target):
                calls.append(copy.deepcopy(candidate))
                if len(calls) in (2, 3):
                    raise OSError("synthetic")
                original(candidate, target)

            sender = Mock(return_value=True)
            with patch.object(persistence, "_write_state", side_effect=write):
                publication.process_queue(
                    state, {}, "fake", "42", state_path=path, dry_run=False, sender=sender,
                    save=lambda s, seen, p: persistence.save_state(s, seen, p, sleep=Mock()))
            sender.assert_called_once()
            self.assertEqual(persistence.load_state(path)["file"], [])

    def test_dry_run_health_cli_never_reads_token_or_writes(self):
        from tools import persistence_health
        for mode in ("gate", "probe", "sync"):
            with patch.dict("os.environ", {"DRY_RUN": "1"}, clear=True), \
                    patch("sys.argv", ["helper", mode]), patch("builtins.print"), \
                    patch.object(persistence_health, "probe") as probe_call, \
                    patch.object(persistence_health, "synchronize") as sync_call:
                persistence_health.main()
                probe_call.assert_not_called()
                sync_call.assert_not_called()

    def test_failed_run_then_recovery_success_returns_to_normal(self):
        prior = {"id": 1, "status": "completed", "conclusion": "failure",
                 "updated_at": "2026-11-16T10:00:00Z", "run_attempt": 1}
        self.assertTrue(recovery_required("example/repo", 2, 1,
                                         Mock(return_value={"workflow_runs": [prior]})))
        prior.update(id=2, conclusion="success", updated_at="2026-11-16T10:15:00Z")
        jobs = {"jobs": [{"conclusion": "success", "steps": [{"name": STEP, "conclusion": "success"}]}]}
        self.assertFalse(recovery_required("example/repo", 3, 1,
                         Mock(side_effect=[{"workflow_runs": [prior]}, jobs])))


class NativeGitCheckpointTests(unittest.TestCase):
    def test_real_local_git_push_and_lost_ack(self):
        # Both remotes and working copies are synthetic, owned by this fixture.
        with tempfile.TemporaryDirectory(prefix="newsbot-git-fixture-") as tmp:
            root = Path(tmp).resolve()
            bare = root / "remote.git"
            work = root / "work"

            def command(args, cwd=root):
                return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                                      check=True, timeout=15).stdout

            command(["git", "init", "--bare", "--initial-branch=main", str(bare)])
            command(["git", "clone", str(bare), str(work)])
            fixture = work / "state.json"
            fixture.write_text('{"file":[],"vus":[]}')
            command(["git", "add", "state.json"], work)
            identity = ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid"]
            command([*identity, "commit", "-m", "fixture"], work)
            command(["git", "push", "origin", "main"], work)
            for lost_ack in (False, True):
                fixture.write_text(json.dumps({"file": [], "vus": [], "probe": lost_ack}))
                pushes = []

                def run(args, timeout):
                    result = subprocess.run(args, cwd=work, capture_output=True, text=True,
                                            check=True, timeout=timeout)
                    if "push" in args:
                        pushes.append(args)
                        if lost_ack:
                            raise subprocess.CalledProcessError(1, "simulated lost acknowledgement")
                    return result.stdout

                with patch("tools.persistence_health.load_state"):
                    synchronize(run)
                remote_state = json.loads(command(["git", "--git-dir=" + str(bare),
                                                   "show", "main:state.json"]))
                self.assertEqual(remote_state["probe"], lost_ack)
                self.assertEqual(len(pushes), 1)
