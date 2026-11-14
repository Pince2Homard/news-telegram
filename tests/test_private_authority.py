"""C5 : autorité et effets simulés, sans socket, secret ou service distant."""
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from tools.windows_watcher import private_authority as authority, private_outbox as outbox


NOW=1800000000
TRIAL="11111111-1111-4111-8111-111111111111"
HOST="22222222-2222-4222-8222-222222222222"
OTHER="33333333-3333-4333-8333-333333333333"
OP="44444444-4444-4444-8444-444444444444"
CHECKPOINT="0"*64


def wait_file(path, timeout=5):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if path.is_file():return
        time.sleep(.01)
    raise AssertionError("fixture marker missing")


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix="phase823-c5-")
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.pc=self.root/"pc";self.pc.mkdir()
        self.directory=self.root/"authority"
        self.transport=outbox.ScriptedTransport()
        with self.session(create=True):pass

    def session(self, **kwargs):
        return authority.FixtureAuthority(self.directory,trial_id=TRIAL,host_id=HOST,
            now=kwargs.pop("now",NOW),transport=kwargs.pop("transport",self.transport),**kwargs)

    def operation(self, session, *, op_id=OP, action="response", message_id=1, **changes):
        status=session.status()
        values=dict(op_id=op_id,host_id=HOST,generation=status["generation"],
            revision=status["revision"],checkpoint=status["checkpoint"],action=action,
            message_id=message_id,text="fixture")
        values.update(changes)
        return authority.FixtureOperation(**values)

    def acknowledge(self, session, op_id=OP):
        status=session.status()
        session.acknowledge(op_id,status["revision"],uuid4().hex+uuid4().hex,NOW)

    def test_bootstrap_binding_and_confirmed_result_are_durable(self):
        with self.session() as session:
            status=session.status()
            self.assertEqual((status["generation"],status["revision"],status["checkpoint"]),(1,0,CHECKPOINT))
            result=session.execute(self.operation(session),NOW)
            self.assertEqual(result["classification"],"confirmed")
            self.assertEqual(result["op_id"],OP)
            self.assertTrue(session.status()["pending"])
        with self.session() as session:
            self.assertEqual(session.inspect(OP)["classification"],"confirmed")
            self.assertEqual(session.status()["responses"],1)
        self.assertEqual(self.transport.accepted,[1])

    def test_consumed_permission_and_logical_operation_cannot_be_replayed(self):
        with self.session() as session:
            operation=self.operation(session)
            session.execute(operation,NOW)
            with self.assertRaises(authority.AuthorityError):session.execute(operation,NOW)
            self.acknowledge(session)
            with self.assertRaises(authority.AuthorityError):
                session.execute(self.operation(session,op_id=str(uuid4())),NOW)
        self.assertEqual(self.transport.accepted,[1])

    def test_pending_operation_blocks_a_second_distinct_operation(self):
        with self.session() as session:
            session.execute(self.operation(session),NOW)
            with self.assertRaises(authority.AuthorityError):
                session.execute(self.operation(session,op_id=str(uuid4()),message_id=2),NOW)
            with self.assertRaises(authority.AuthorityError):session.rotate(OTHER,NOW)
        self.assertEqual(self.transport.accepted,[1])

    def test_same_generation_for_another_simulated_machine_is_refused(self):
        with self.session() as session:
            with self.assertRaises(authority.AuthorityError):
                session.execute(self.operation(session,host_id=OTHER),NOW)
            self.assertEqual(session.status()["responses"],0)
        self.assertEqual(self.transport.requests,[])

    def test_old_generation_is_refused_after_rotation(self):
        with self.session() as session:
            old=self.operation(session)
            session.rotate(HOST,NOW)
            self.assertEqual(session.status()["generation"],2)
            with self.assertRaises(authority.AuthorityError):session.execute(old,NOW)
        self.assertEqual(self.transport.requests,[])

    def test_restored_pc_checkpoint_and_revision_are_refused(self):
        with self.session() as session:
            snapshot=session.status()
            (self.pc/"checkpoint.json").write_text(json.dumps(snapshot))
            session.execute(self.operation(session),NOW)
            self.acknowledge(session)
            restored=json.loads((self.pc/"checkpoint.json").read_text())
            with self.assertRaises(authority.AuthorityError):session.execute(
                self.operation(session,op_id=str(uuid4()),message_id=2,
                    revision=restored["revision"],checkpoint=restored["checkpoint"]),NOW)
            with self.assertRaises(authority.AuthorityError):session.execute(
                self.operation(session,op_id=str(uuid4()),message_id=2,checkpoint=restored["checkpoint"]),NOW)
        self.assertEqual(self.transport.accepted,[1])

    def test_acknowledgement_mismatch_never_clears_pending(self):
        with self.session() as session:
            session.execute(self.operation(session),NOW)
            with self.assertRaises(authority.AuthorityError):session.acknowledge(
                OP,0,"1"*64,NOW)
            self.assertTrue(session.status()["pending"])
            with self.assertRaises(authority.AuthorityError):session.acknowledge(
                str(uuid4()),session.status()["revision"],"1"*64,NOW)
            self.assertTrue(session.status()["pending"])

    def test_lost_admission_response_is_consumed_without_operation_and_no_retry(self):
        def lose(stage):
            if stage=="after_consume_commit":raise OSError("fixture response lost")
        with self.session(fault=lose) as session:
            operation=self.operation(session)
            with self.assertRaises((OSError,authority.AuthorityError)):session.execute(operation,NOW)
        with self.session() as session:
            self.assertEqual(session.inspect(OP)["classification"],"consumed_without_operation")
            with self.assertRaises(authority.AuthorityError):session.execute(operation,NOW)
            self.acknowledge(session)
        self.assertEqual(self.transport.requests,[])

    def test_revoke_before_dispatch_is_consumed_without_operation(self):
        session=None
        def revoke(stage):
            if stage=="before_dispatch":session.revoke(NOW)
        with self.session(fault=revoke) as session:
            result=session.execute(self.operation(session),NOW)
            self.assertEqual(result["classification"],"consumed_without_operation")
        with self.session() as session:
            self.assertEqual(session.status()["state"],"revoked")
            with self.assertRaises(authority.AuthorityError):
                session.execute(self.operation(session,op_id=str(uuid4()),message_id=2),NOW)
        self.assertEqual(self.transport.requests,[])

    def test_revoke_after_possible_acceptance_is_uncertain_not_refused(self):
        session=None
        def revoke(stage):
            if stage=="after_effect":session.revoke(NOW)
        with self.session(fault=revoke) as session:
            result=session.execute(self.operation(session),NOW)
            self.assertEqual(result["classification"],"uncertain")
            with self.assertRaises(authority.AuthorityError):self.acknowledge(session)
        with self.session() as session:
            self.assertEqual(session.inspect(OP)["classification"],"uncertain")
            self.assertEqual(session.status()["state"],"revoked")
        self.assertEqual(self.transport.accepted,[1])

    def test_remote_acceptance_then_response_lost_is_uncertain_without_retry(self):
        transport=outbox.ScriptedTransport([outbox.SimulatedOutcome("timeout_after_accept")])
        with self.session(transport=transport) as session:
            operation=self.operation(session)
            result=session.execute(operation,NOW)
            self.assertEqual(result["classification"],"uncertain")
            with self.assertRaises(authority.AuthorityError):session.execute(operation,NOW)
            with self.assertRaises(authority.AuthorityError):self.acknowledge(session)
        self.assertEqual(transport.accepted,[1])
        self.assertEqual(len(transport.requests),1)

    def test_revocation_is_terminal_and_expiration_denies_new_operation(self):
        with self.session() as session:session.revoke(NOW)
        with self.session() as session:
            self.assertEqual(session.status()["state"],"revoked")
            with self.assertRaises(authority.AuthorityError):session.rotate(HOST,NOW)
        self.assertEqual(self.transport.requests,[])

    def test_expiration_while_paused_before_dispatch_is_consumed_without_operation(self):
        clock=[NOW]
        def expire(stage):
            if stage=="before_dispatch":clock[0]=NOW+301
        with self.session(clock=lambda:clock[0],fault=expire) as session:
            result=session.execute(self.operation(session),NOW)
            self.assertEqual(result["classification"],"consumed_without_operation")
            self.assertEqual(session.status()["state"],"expired")
        self.assertEqual(self.transport.requests,[])

    def test_clock_reversal_before_dispatch_persists_recovery_required(self):
        clock=[NOW]
        def reverse(stage):
            if stage=="before_dispatch":clock[0]=NOW-1
        with self.session(clock=lambda:clock[0],fault=reverse) as session:
            result=session.execute(self.operation(session),NOW)
            self.assertEqual(result["classification"],"consumed_without_operation")
            self.assertEqual(session.status()["state"],"recovery_required")
        with self.session() as session:
            self.assertEqual(session.status()["state"],"recovery_required")
        self.assertEqual(self.transport.requests,[])

    def test_inspect_before_dispatch_is_provisional_until_atomic_ack_aborts_effect(self):
        session=None
        observed=[]
        def acknowledge_before_effect(stage):
            if stage=="before_dispatch":
                observed.append(session.inspect(OP))
                self.acknowledge(session)
                observed.append(session.inspect(OP))
        with self.session(fault=acknowledge_before_effect) as session:
            result=session.execute(self.operation(session),NOW)
            self.assertEqual(result["classification"],"consumed_without_operation")
            self.assertTrue(result["terminal"])
            self.assertFalse(session.status()["pending"])
        self.assertFalse(observed[0]["terminal"])
        self.assertTrue(observed[1]["terminal"])
        self.assertEqual(self.transport.requests,[])

    def test_existing_operation_id_cannot_be_rebound_to_a_different_request(self):
        with self.session() as session:
            result=session.execute(self.operation(session),NOW)
            original=session.inspect(OP)
            self.acknowledge(session)
            with self.assertRaises(authority.AuthorityError):
                session.execute(self.operation(session,message_id=2,text="fixture changed"),NOW)
            self.assertEqual(session.inspect(OP),original)
            self.assertEqual(result["classification"],"confirmed")
        self.assertEqual(self.transport.accepted,[1])

    def test_expired_or_backward_clock_cannot_consume_permission(self):
        with self.session() as session:
            operation=self.operation(session)
            with self.assertRaises(authority.AuthorityError):session.execute(operation,NOW+300)
            self.assertEqual(session.status()["responses"],0)
        self.assertEqual(self.transport.requests,[])

    def test_external_quotas_cannot_be_reset_by_reopening_pc_state(self):
        with self.session() as session:
            for number in range(1,21):
                op_id=str(uuid4())
                session.execute(self.operation(session,op_id=op_id,action="read",message_id=number),NOW)
                self.acknowledge(session,op_id)
            for number in range(1,7):
                op_id=str(uuid4())
                session.execute(self.operation(session,op_id=op_id,message_id=number),NOW)
                self.acknowledge(session,op_id)
            self.assertEqual((session.status()["reads"],session.status()["responses"]),(20,6))
        (self.pc/"trial.json").write_text('{"reads":0,"responses":0}')
        with self.session() as session:
            for action,mid in (("read",21),("response",7)):
                with self.assertRaises(authority.AuthorityError):session.execute(
                    self.operation(session,op_id=str(uuid4()),action=action,message_id=mid),NOW)
            self.assertEqual((session.status()["reads"],session.status()["responses"]),(20,6))

    def test_authority_unavailable_or_corrupt_never_creates_a_fresh_database(self):
        path=self.directory/"authority.sqlite3"
        previous=path.read_bytes()
        path.unlink()
        with self.assertRaises(authority.AuthorityError):
            with self.session():pass
        self.assertFalse(path.exists())
        path.write_bytes(b"corrupt synthetic authority")
        with self.assertRaises(authority.AuthorityError):
            with self.session():pass
        self.assertEqual(path.read_bytes(),b"corrupt synthetic authority")
        self.assertTrue(previous)
        self.assertEqual(self.transport.requests,[])

    def test_no_socket_credential_or_real_transport_is_used(self):
        with ExitStack() as stack:
            for target in ("socket.socket","requests.Session.request",
                "tools.windows_watcher.telegram_once.read_external_token",
                "tools.windows_watcher.telegram_http.TelegramHTTP.__init__"):
                stack.enter_context(patch(target,side_effect=AssertionError("real I/O forbidden")))
            with self.session() as session:
                self.assertEqual(session.execute(self.operation(session),NOW)["classification"],"confirmed")

    def test_limitation_restoring_authority_itself_reenables_a_consumed_operation(self):
        path=self.directory/"authority.sqlite3"
        snapshot=path.read_bytes()
        with self.session() as session:
            operation=self.operation(session)
            self.assertEqual(session.execute(operation,NOW)["classification"],"confirmed")
        path.write_bytes(snapshot)
        with self.session() as session:
            self.assertEqual(session.execute(operation,NOW)["classification"],"confirmed")
        self.assertEqual(self.transport.accepted,[1,1])
        print("C5 LIMITATION: authority rollback replayed a simulated effect; anti-rollback anchor NOT demonstrated")

    def test_two_processes_racing_the_same_permission_have_one_confirmed_effect(self):
        script=r'''
import json,sys,time
from pathlib import Path
from tools.windows_watcher.private_authority import FixtureAuthority,FixtureOperation,AuthorityError
from tools.windows_watcher.private_outbox import ScriptedTransport
directory,pc,name=sys.argv[1:];pc=Path(pc)
transport=ScriptedTransport()
with FixtureAuthority(directory,trial_id='11111111-1111-4111-8111-111111111111',host_id='22222222-2222-4222-8222-222222222222',now=1800000000,transport=transport) as session:
    operation=FixtureOperation('44444444-4444-4444-8444-444444444444','22222222-2222-4222-8222-222222222222',1,0,'0'*64,'response',1)
    (pc/(name+'.ready')).write_text('fixture')
    deadline=time.monotonic()+5
    while not (pc/'go').exists():
        if time.monotonic()>deadline:raise RuntimeError('fixture barrier expired')
        time.sleep(.005)
    try:classification=session.execute(operation,1800000000)['classification']
    except AuthorityError as error:classification=error.classification
    (pc/(name+'.json')).write_text(json.dumps(dict(classification=classification,accepted=transport.accepted)))
'''
        processes=[]
        try:
            for name in ("first","second"):
                processes.append(subprocess.Popen([sys.executable,"-c",script,str(self.directory),str(self.pc),name],
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)))
            for name in ("first","second"):wait_file(self.pc/(name+".ready"))
            (self.pc/"go").write_text("fixture")
            for process in processes:
                _,errors=process.communicate(timeout=7)
                self.assertEqual(process.returncode,0,errors.decode(errors="replace"))
            results=[json.loads((self.pc/(name+".json")).read_text()) for name in ("first","second")]
            self.assertEqual(sorted(value["classification"] for value in results),["confirmed","permission_refused"])
            self.assertEqual(sum(len(value["accepted"]) for value in results),1)
            with self.session() as session:
                self.assertEqual(session.status()["responses"],1)
                self.assertEqual(session.inspect(OP)["classification"],"confirmed")
        finally:
            for process in processes:
                if process.poll() is None:process.kill();process.communicate(timeout=3)

    def test_real_authority_process_crashes_before_and_after_commits(self):
        script=r'''
import json,os,sys
from pathlib import Path
from tools.windows_watcher.private_authority import FixtureAuthority,FixtureOperation
from tools.windows_watcher.private_outbox import ScriptedTransport
directory,stage,evidence=sys.argv[1:]
transport=ScriptedTransport()
def crash(point):
    if point==stage:
        if point in ('after_effect','before_result_commit'):
            Path(evidence).write_text(json.dumps(dict(accepted=transport.accepted)))
        os._exit(66)
with FixtureAuthority(directory,trial_id='11111111-1111-4111-8111-111111111111',host_id='22222222-2222-4222-8222-222222222222',now=1800000000,transport=transport,fault=crash) as session:
    operation=FixtureOperation('44444444-4444-4444-8444-444444444444','22222222-2222-4222-8222-222222222222',1,0,'0'*64,'response',1)
    session.execute(operation,1800000000)
'''
        for stage,classification in (("before_consume_commit","permission_refused"),
                ("after_consume_commit","consumed_without_operation"),
                ("after_dispatch_commit","uncertain"),("after_effect","uncertain"),
                ("before_result_commit","uncertain")):
            with self.subTest(stage=stage):
                directory=self.root/("authority-"+stage)
                with authority.FixtureAuthority(directory,create=True,trial_id=TRIAL,host_id=HOST,now=NOW):pass
                evidence=self.pc/(stage+".json")
                process=subprocess.run([sys.executable,"-c",script,str(directory),stage,str(evidence)],
                    capture_output=True,timeout=7,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
                self.assertEqual(process.returncode,66,process.stderr.decode(errors="replace"))
                with authority.FixtureAuthority(directory,trial_id=TRIAL,host_id=HOST,now=NOW,transport=self.transport) as session:
                    if classification=="permission_refused":
                        self.assertEqual(session.status()["responses"],0)
                        self.assertIsNone(session.status()["pending"])
                        with self.assertRaises(authority.AuthorityError):session.inspect(OP)
                    else:
                        self.assertEqual(session.status()["responses"],1)
                        self.assertEqual(session.inspect(OP)["classification"],classification)
                        with self.assertRaises(authority.AuthorityError):session.execute(
                            authority.FixtureOperation(OP,HOST,1,0,CHECKPOINT,"response",1),NOW)
                    self.assertEqual(self.transport.requests,[])
                if stage in {"after_effect","before_result_commit"}:
                    self.assertEqual(json.loads(evidence.read_text())["accepted"],[1])
                else:self.assertFalse(evidence.exists())

    def test_limitation_old_live_worker_can_bypass_permissions_with_a_direct_capability(self):
        script=r'''
import json,sys,time
from pathlib import Path
from tools.windows_watcher.private_outbox import ScriptedTransport,SimulatedRequest
pc=Path(sys.argv[1]);(pc/'worker.ready').write_text('fixture')
deadline=time.monotonic()+5
while not (pc/'direct-go').exists():
    if time.monotonic()>deadline:raise RuntimeError('fixture barrier expired')
    time.sleep(.005)
transport=ScriptedTransport()
request=SimulatedRequest(1,12345,'11111111-1111-4111-8111-111111111111','fixture',1800000020)
transport.exchange(request)
(pc/'direct-effect.json').write_text(json.dumps(dict(accepted=transport.accepted)))
'''
        process=subprocess.Popen([sys.executable,"-c",script,str(self.pc)],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        try:
            wait_file(self.pc/"worker.ready")
            self.assertIsNone(process.poll())
            with self.session() as session:
                with self.assertRaises(authority.AuthorityError):session.execute(self.operation(session),NOW+301)
                session.revoke(NOW+301)
                self.assertEqual(session.status()["state"],"revoked")
            self.assertIsNone(process.poll())
            (self.pc/"direct-go").write_text("fixture")
            _,errors=process.communicate(timeout=7)
            self.assertEqual(process.returncode,0,errors.decode(errors="replace"))
            self.assertEqual(json.loads((self.pc/"direct-effect.json").read_text())["accepted"],[1])
            self.assertEqual(self.transport.requests,[])
            print("C5 LIMITATION: expired/revoked permissions do NOT fence a live holder of a direct simulated capability")
        finally:
            if process.poll() is None:process.kill();process.communicate(timeout=3)


if __name__=="__main__":unittest.main()
