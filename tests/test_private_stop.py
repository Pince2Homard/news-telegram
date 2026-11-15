"""Secours : noyau simulé portable et workers Windows sans socket.

Les incidents ACL sont injectés ; aucune permission du poste n'est changée.
"""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.windows_watcher import private_boundary as boundary, private_stop as stop
from tools.windows_watcher import private_fenced_job as native, private_recovery as recovery
from tests import test_private_f3 as f3
from tests.test_private_boundary import Filesystem


class Kernel:
    """Modèle explicite : aucune preuve native ni création de processus."""
    def __init__(self):
        self.calls = []
        self.inside, self.query_ok, self.root_status = True, True, 0
        self.counts = [1, 0]

    def OpenEventW(self, *args):self.calls.append("event");return 11
    def SetEvent(self, *args):self.calls.append("admission_closed");return 1
    def OpenJobObjectW(self, *args):self.calls.append("job");return 22
    def OpenProcess(self, *args):self.calls.append("root");return 33
    def IsProcessInJob(self, root, job, inside):inside._obj.value=self.inside;return 1
    def TerminateJobObject(self, *args):self.calls.append("terminate");return 1
    def QueryInformationJobObject(self, job, kind, counts, *args):
        self.calls.append("query")
        counts._obj.active = self.counts.pop(0) if self.counts else 0
        return int(self.query_ok)
    def WaitForSingleObject(self, *args):self.calls.append("reap_root");return self.root_status
    def CloseHandle(self, handle):self.calls.append(("closed", handle));return 1


class StopModels(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="private-stop-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "stop-ticket.json"
        self.ticket = dict(fixtures_only=True, session=f3.GEN,
            event="Local\\phase-f3-stop-"+f3.GEN, job="Local\\phase823-c2-"+f3.GEN,
            controller=dict(pid=1001, birth=7001), worker=dict(pid=1002, birth=7002))
        self.save(self.ticket)
        self.kernel = Kernel()
        self.fs = Filesystem()
        with self.fs.patches():self.guard = self.fs.boundary()

    def save(self, value):self.path.write_text(json.dumps(value), encoding="utf-8")

    def call(self, **kwargs):
        with patch.object(stop, "os", SimpleNamespace(name="nt")), \
                patch.object(stop, "_api", return_value=self.kernel), \
                patch.object(stop.native, "birth", return_value=7002):
            return stop.stop_fixture(self.path, **kwargs)

    def test_failed_boundary_cannot_block_stop_or_reap_but_blocks_normal_access(self):
        for failure in ("latched", "inspection_error"):
            with self.subTest(failure=failure):
                self.kernel = Kernel()  # worker simulé actif, avant la panne ACL.
                with self.fs.patches():self.guard = self.fs.boundary()
                if failure == "latched":self.guard.failed = True
                else:
                    with self.fs.patches(), patch.object(boundary.credential_acl, "inspect",
                            side_effect=OSError("synthetic ACL failure")):
                        with self.assertRaises(boundary.BoundaryError):
                            boundary.check(self.guard, control=self.fs.paths["control"])
                with self.assertRaises(boundary.BoundaryError):
                    boundary.check(self.guard, control=self.fs.paths["control"])
                with patch.object(boundary.credential_acl, "inspect", side_effect=AssertionError("no ACL in rescue")):
                    proof = self.call(boundary=self.guard, expected_ticket=self.ticket)
                self.assertTrue(proof["admission_closed"])
                self.assertTrue(proof["worker_stopped"])
                self.assertEqual(proof["active"], 0)
                self.assertFalse(proof["controller_closure"])
                self.assertTrue(self.guard.failed)
                calls = self.kernel.calls
                self.assertLess(calls.index("admission_closed"), calls.index("terminate"))
                self.assertEqual(calls.count("query"), 2)
                self.assertLess(calls.index("terminate"), calls.index("reap_root"))
                self.assertEqual(calls[-3:], [("closed", 33), ("closed", 22), ("closed", 11)])

    def test_guarded_rescue_requires_identity_captured_before_incident(self):
        self.guard.failed = True
        with self.assertRaisesRegex(stop.StopError, "STOP_TICKET_UNPROVEN"):
            self.call(boundary=self.guard)
        self.assertEqual(self.kernel.calls, [])

    def test_changed_ticket_is_not_authenticated_by_its_own_contents(self):
        for field in ("worker", "controller", "session"):
            changed = json.loads(json.dumps(self.ticket))
            if field == "session":
                changed["session"] = f3.E.host_id
                changed["event"] = "Local\\phase-f3-stop-"+changed["session"]
                changed["job"] = "Local\\phase823-c2-"+changed["session"]
            else:changed[field]["birth"] += 1
            self.save(changed)
            with self.subTest(field=field), self.assertRaisesRegex(stop.StopError, "STOP_TICKET_CHANGED"):
                self.call(boundary=self.guard, expected_ticket=self.ticket)
            self.assertEqual(self.kernel.calls, [])

    def test_unreadable_or_malformed_ticket_returns_unproven_without_guessing(self):
        self.path.write_text("{", encoding="utf-8")
        for failure in (None, PermissionError("synthetic ticket denied")):
            with self.subTest(failure=failure), patch.object(stop, "_api", side_effect=AssertionError("no guessed job")), \
                    patch.object(stop, "os", SimpleNamespace(name="nt")), \
                    patch.object(stop.sys, "argv", ["stop", "--fixture-stop", str(self.path)]):
                output = io.StringIO()
                with redirect_stdout(output):
                    if failure is None:code = stop.main()
                    else:
                        with patch.object(stop, "_ticket", side_effect=failure):code = stop.main()
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(output.getvalue()), dict(code="STOP_UNPROVEN", safe_stop=False))

    def test_incomplete_kernel_proof_never_reports_success(self):
        for failure in ("birth", "membership", "query", "root_wait"):
            self.kernel = Kernel()
            self.kernel.inside = failure != "membership"
            self.kernel.query_ok = failure != "query"
            self.kernel.root_status = 258 if failure == "root_wait" else 0
            with self.subTest(failure=failure), patch.object(stop, "os", SimpleNamespace(name="nt")), \
                    patch.object(stop, "_api", return_value=self.kernel), \
                    patch.object(stop.native, "birth", return_value=7003 if failure == "birth" else 7002):
                with self.assertRaises(stop.StopError):
                    stop.stop_fixture(self.path, boundary=self.guard, expected_ticket=self.ticket)
            self.assertIn("admission_closed", self.kernel.calls)
            self.assertIn("terminate", self.kernel.calls)
            self.assertEqual(self.kernel.calls[-3:], [("closed", 33), ("closed", 22), ("closed", 11)])

    def test_legacy_fixture_stop_still_requires_job_and_root_proof(self):
        self.assertTrue(self.call()["worker_stopped"])
        self.assertIn("reap_root", self.kernel.calls)


@unittest.skipUnless(os.name == "nt", "Windows Job Object rescue with synthetic ACL failure")
class NativeStopFailure(f3.Setup, unittest.TestCase):
    def rescue(self, failure, *, wrong_birth=False):
        fs = Filesystem()
        with fs.patches():guard = fs.boundary()
        with self.fence() as fence:
            fence.begin();fence.touch_store()
            root = self.network()
            (root / "request.json").write_text(json.dumps(dict(mode="dns_block", port=0,
                delay=0, tls=False, size=1)), encoding="utf-8")
            gate = stop.NativeStopGate(self.network(), fence.state["session"])
            job = native.FencedFixtureJob(root, 5, fence)
            try:
                job.start()
                gate.publish(job)
                until = time.monotonic()+2
                while not (root / "phase.json").is_file():
                    if time.monotonic() >= until:self.fail("synthetic worker did not start")
                    time.sleep(.01)
                self.assertEqual(json.loads((root / "phase.json").read_text())["phase"], "dns_simulated")
                expected = json.loads(json.dumps(gate.data))
                if wrong_birth:
                    expected["worker"]["birth"] += 1
                    gate.path.write_text(json.dumps(expected), encoding="utf-8")
                if failure == "latched":guard.failed = True
                else:
                    with fs.patches(), patch.object(boundary.credential_acl, "inspect",
                            side_effect=OSError("synthetic ACL inspection failure")):
                        with self.assertRaises(boundary.BoundaryError):
                            boundary.check(guard, control=fs.paths["control"])
                gate.boundary = guard
                with self.assertRaises(boundary.BoundaryError):gate.check()
                with patch.object(stop, "_api", return_value=gate.api), \
                        patch.object(gate.api, "SetEvent", wraps=gate.api.SetEvent) as signal, \
                        patch.object(gate.api, "TerminateJobObject", wraps=gate.api.TerminateJobObject) as terminate, \
                        patch.object(boundary.credential_acl, "inspect", side_effect=AssertionError("no ACL in rescue")):
                    if wrong_birth:
                        with self.assertRaisesRegex(stop.StopError, "ROOT_STOP_UNPROVEN"):
                            stop.stop_fixture(gate.path, boundary=guard, expected_ticket=expected)
                    else:
                        proof = stop.stop_fixture(gate.path, boundary=guard, expected_ticket=expected)
                        self.assertTrue(proof["admission_closed"])
                        self.assertTrue(proof["worker_stopped"])
                        self.assertEqual(proof["active"], 0)
                        self.assertFalse(proof["controller_closure"])
                    signal.assert_called_once()
                    terminate.assert_called_once()
                self.assertTrue(gate.requested)
                self.assertEqual(job.api.WaitForSingleObject(job.process.process, 1000), 0)
                self.assertFalse(job.safe)  # Le secours ne ferme pas les handles du contrôleur.
                self.assertIsNotNone(job.lock)
                job.wait()  # Récolte du contrôleur, sans socket ni résultat tardif accepté.
                self.assertTrue(job.safe)
                self.assertEqual(job.final_counts["active"], 0)
                self.assertLess(job.trace.index("reaped_zero"), job.trace.index("lock_released"))
                with self.assertRaises(boundary.BoundaryError):gate.check()
            finally:
                if not job.safe:job.stop()
                gate.close()
        # La fermeture du contexte ne scelle ni ne réarme une session dirty.
        with self.fence() as reopened:
            with self.assertRaisesRegex(recovery.FenceError, "PREVIOUS_SESSION_UNPROVEN"):reopened.begin()

    def test_latched_boundary_after_launch_does_not_block_native_rescue(self):self.rescue("latched")
    def test_acl_inspection_error_after_launch_does_not_block_native_rescue(self):self.rescue("inspection_error")
    def test_wrong_birth_terminates_but_cannot_prove_stop_or_reopen(self):self.rescue("latched", wrong_birth=True)
