"""C2 : journaux portables, intégration simulée, crashes et preuves OS Windows."""
from contextlib import ExitStack, closing
import ctypes
from html import unescape
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from tools.windows_watcher import private_contracts as contracts, private_fenced_job as native
from tools.windows_watcher import private_integration as integration, private_local_lab as lab
from tools.windows_watcher import private_outbox as outbox, private_qualification as q
from tools.windows_watcher import private_recovery as recovery, private_store as base
from tools.windows_watcher import watcher


NOW=1800000000
GEN="11111111-1111-4111-8111-111111111111"
SOURCE="33333333-3333-4333-8333-333333333333"
CONFIG=dict(mode="simulation",send_enabled=False,notifications_channel_id=None,
            owner_user_id=12345,arbitration_private_chat_id=12345)
BINDING=dict(simulation_only=True,bot_id=54321,owner_id=12345,chat_id=12345,generation=GEN)
PERMISSION=outbox.SimulationPermission(54321,12345,12345,GEN,True)


def update(uid=10,mid=1):
    return {"update_id":uid,"message":{"message_id":mid,"date":NOW,"text":"/status",
        "from":{"id":12345,"is_bot":False},"chat":{"id":12345,"type":"private"}}}


def raw(values):
    return json.dumps({"ok":True,"result":values}).encode()


def request(root,mode="tree",port=0):
    (root/"request.json").write_text(json.dumps(dict(mode=mode,port=port,delay=0,tls=False,size=1)))


def wait_file(path):
    until=time.monotonic()+4
    while time.monotonic()<until:
        try:return json.loads(path.read_text())
        except (OSError,ValueError):time.sleep(.01)
    raise AssertionError("fixture not ready")


def synthetic_acl(paths):
    user="S-1-5-21-111111111-222222222-333333333-1001"
    return {"user":user,"rows":[dict(path=str(p),owner=user,protected=True,null_dacl=False,reparse=False,
        rules=[dict(sid=user,allow=True,rights=2032127,inherit_only=False)]) for p in paths]}


class FixtureSetup:
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="phase823-c2-")
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.store=self.root/"store"
        with self.session(create=True):pass
        with self.fence(create=True):pass

    def session(self,**kwargs):
        return outbox.DeliveryStore(self.store,CONFIG,54321,kwargs.pop("now",NOW),generation=GEN,**kwargs)

    def fence(self,**kwargs):
        return recovery.FixtureFence(self.root/"local",self.root/"witness",self.store/"fixtures.sqlite3",
                                     BINDING,kwargs.pop("now",NOW),**kwargs)


class FixtureTests(FixtureSetup,unittest.TestCase):
    def test_clean_restart_changes_session_and_rejects_old_results(self):
        with self.fence() as fence:
            old=fence.begin()
            fence.seal()
        with self.fence() as fence:
            self.assertNotEqual(fence.begin(),old)
            with self.assertRaisesRegex(recovery.FenceError,"STALE_GENERATION"):
                recovery.accept_result(fence,old,{"generation":old})
            fence.seal()

    def test_unclosed_session_is_durable_even_when_lock_is_free(self):
        with self.fence() as fence:fence.begin()
        with watcher.single_instance_lock(self.root/"local/fence.lock"):pass
        with self.fence() as fence:
            with self.assertRaisesRegex(recovery.FenceError,"PREVIOUS_SESSION_UNPROVEN"):fence.begin()
            with self.assertRaisesRegex(recovery.FenceError,"OBSERVATION_MISMATCH"):fence.recover_observed({"pid":12345})

    def test_rejected_minimal_receipts_are_compatible_and_atomic_with_cursor(self):
        outsider={"update_id":12,"message":{"text":"fixture-not-retained","from":{"id":99999}}}
        projected=contracts.validate_updates(raw([update(),outsider]),CONFIG)
        self.assertEqual(projected[1],{"update_id":12,"disposition":"rejected"})
        with self.fence() as fence:
            fence.begin();fence.touch_store()
            with self.session() as store:
                result=integration.receive_fixture(fence,store,raw([update(),outsider]),0,NOW)
                self.assertEqual((result["stored_offset"],result["receipts"],result["messages"]),(13,2,1))
                self.assertEqual(tuple(store.connection.execute("SELECT message_id,digest,disposition FROM receipts WHERE update_id=12").fetchone()),(None,None,"rejected"))
                self.assertNotIn("fixture-not-retained",str(list(store.connection.iterdump())))
                denied=Mock(side_effect=AssertionError("snapshot read before identity"))
                self.assertIsNone(integration.prepare_fixture(fence,store,999,denied,NOW))
                response=integration.prepare_fixture(fence,store,1,lambda:None,NOW)
                same=integration.prepare_fixture(fence,store,1,denied,NOW)
                self.assertEqual(response,same)
                self.assertEqual(store.connection.execute("SELECT count(*) FROM outbox").fetchone()[0],1)
                self.assertEqual(integration.receive_fixture(fence,store,raw([update(),outsider]),13,NOW)["new_receipts"],0)
            fence.seal()

    def test_accepted_then_lost_response_never_retries_across_restart_or_epoch(self):
        with self.fence() as fence:
            first=fence.begin();fence.touch_store()
            with self.session() as store:
                integration.receive_fixture(fence,store,raw([update()]),0,NOW)
                integration.prepare_fixture(fence,store,1,lambda:None,NOW)
                transport=outbox.ScriptedTransport([outbox.SimulatedOutcome("timeout_after_accept")])
                self.assertEqual(store.deliver(1,transport,NOW,permission=PERMISSION),"uncertain")
                self.assertEqual(transport.accepted,[1])
            fence.seal()
        with self.fence(now=NOW+8*86400) as fence:
            self.assertNotEqual(fence.begin(),first);fence.touch_store()
            with self.session(now=NOW+8*86400) as store:
                store.recover_epoch(contracts.validate_updates(raw([update(1)]),CONFIG),NOW+8*86400,
                                    approval="reviewed-simulation-recovery")
                self.assertEqual(store.connection.execute("SELECT count(*) FROM messages").fetchone()[0],1)
                self.assertEqual(store.connection.execute("SELECT count(*) FROM outbox").fetchone()[0],1)
                self.assertEqual(store.inspect_outbox(1)["status"],"uncertain")
                transport=outbox.ScriptedTransport()
                self.assertEqual(store.deliver(1,transport,NOW+8*86400,permission=PERMISSION),"uncertain")
                self.assertEqual(transport.requests,[])
            fence.seal()

    def test_malformed_contract_never_progresses_cursor(self):
        with self.fence() as fence:
            fence.begin();fence.touch_store()
            with self.session() as store:
                for values in ([update(11),update(10)],[{"update_id":True}],"bad"):
                    with self.assertRaises(contracts.ResponseRefused):
                        integration.receive_fixture(fence,store,raw(values),0,NOW)
                    self.assertEqual(store.status()["stored_offset"],0)
                for invalid in (True,None,-1):
                    with self.assertRaises(recovery.FenceError):integration.prepare_fixture(fence,store,invalid,Mock(),NOW)
            fence.seal()

    def test_failed_transaction_preserves_receipts_and_offset(self):
        def fail(stage):
            if stage=="after_receipt":raise OSError("synthetic full disk")
        with self.fence() as fence:
            fence.begin();fence.touch_store()
            with self.session(fault=fail) as store:
                with self.assertRaises(base.StoreError):integration.receive_fixture(fence,store,raw([update()]),0,NOW)
            with self.session() as store:
                self.assertEqual((store.status()["stored_offset"],store.status()["receipts"]),(0,0))
            fence.seal()

    def test_quota_and_clock_errors_do_not_expose_snapshot_or_advance(self):
        with self.fence() as fence:
            fence.begin();fence.touch_store()
            with self.session(receipt_limit=1) as store:
                with self.assertRaises(base.StoreError):integration.receive_fixture(fence,store,raw([update(),update(11,2)]),0,NOW)
            with self.session() as store:self.assertEqual(store.status()["stored_offset"],0)
            fence.seal()
        with self.assertRaisesRegex(recovery.FenceError,"CLOCK_REVERSED"):
            with self.fence(now=NOW-1):pass

    def test_stale_command_expires_without_erasing_receipt(self):
        with self.fence() as fence:
            fence.begin();fence.touch_store()
            with self.session() as store:integration.receive_fixture(fence,store,raw([update()]),0,NOW)
            fence.seal()
        with self.fence(now=NOW+121) as fence:
            fence.begin();fence.touch_store()
            with self.session(now=NOW+121) as store:
                self.assertIsNone(integration.prepare_fixture(fence,store,1,Mock(side_effect=AssertionError()),NOW+121))
                self.assertEqual((store.status()["receipts"],store.status()["stored_offset"]),(1,11))
            fence.seal()

    def test_store_restore_and_path_clone_are_refused(self):
        old=(self.store/"fixtures.sqlite3").read_bytes()
        with self.fence() as fence:
            fence.begin();fence.touch_store()
            with self.session() as store:integration.receive_fixture(fence,store,raw([update()]),0,NOW)
            fence.seal()
        (self.store/"fixtures.sqlite3").write_bytes(old)
        with self.fence() as fence:
            with self.assertRaisesRegex(recovery.FenceError,"STORE_RESTORED_OR_CHANGED"):fence.begin()
        clone=self.root/"clone";clone.mkdir()
        shutil.copytree(self.root/"local",clone/"local")
        shutil.copytree(self.root/"witness",clone/"witness")
        with self.assertRaisesRegex(recovery.FenceError,"PROOF_IDENTITY"):
            with recovery.FixtureFence(clone/"local",clone/"witness",self.store/"fixtures.sqlite3",BINDING,NOW):pass

    def test_partial_proof_commit_is_not_silently_repaired(self):
        def fail(stage):
            if stage=="after_proof_commit_0":raise OSError("fixture crash")
        with self.fence(fault=fail) as fence:
            with self.assertRaises(OSError):fence.begin()
            self.assertTrue(fence.poisoned)
        with self.assertRaisesRegex(recovery.FenceError,"WITNESS_MISMATCH"):
            with self.fence():pass

    def test_restored_or_missing_witness_and_corruption_block_without_reset(self):
        path=self.root/"witness/fence.sqlite3"
        previous=path.read_bytes()
        with self.fence() as fence:fence.begin();fence.seal()
        path.write_bytes(previous)
        with self.assertRaisesRegex(recovery.FenceError,"WITNESS_MISMATCH"):
            with self.fence():pass
        path.write_bytes(b"corrupt fixture")
        with self.assertRaises(recovery.FenceError):
            with self.fence():pass
        self.assertEqual(path.read_bytes(),b"corrupt fixture")
        path.unlink()
        with self.assertRaises(recovery.FenceError):
            with self.fence():pass
        self.assertFalse(path.exists())

    def test_schema_and_fence_quota_fail_closed(self):
        with self.fence() as fence:
            with patch.object(recovery,"MAX_EVENTS",fence.seq):
                with self.assertRaisesRegex(recovery.FenceError,"PROOF_QUOTA"):fence.begin()
        with closing(sqlite3.connect(self.root/"local/fence.sqlite3")) as db:db.execute("PRAGMA user_version=99")
        with self.assertRaisesRegex(recovery.FenceError,"PROOF_INVALID"):
            with self.fence():pass

    def test_simultaneous_fences_cannot_share_witness(self):
        with self.fence():
            with self.assertRaises(recovery.FenceError):
                with self.fence():pass

    def test_integration_requires_prior_durable_store_intent(self):
        with self.fence() as fence:
            fence.begin()
            with self.session() as store:
                with self.assertRaisesRegex(recovery.FenceError,"STORE_NOT_BOUND"):
                    integration.receive_fixture(fence,store,raw([]),0,NOW)
            # L'ouverture du magasin a écrit ; ce mauvais appel n'est pas scellé.

    def test_no_credential_or_existing_transport_consumed_by_integration(self):
        with ExitStack() as stack:
            for target in ("tools.windows_watcher.telegram_once.read_external_token",
                           "tools.windows_watcher.telegram_http.TelegramHTTP.__init__",
                           "requests.Session.request","socket.socket"):
                stack.enter_context(patch(target,side_effect=AssertionError("real I/O forbidden")))
            with self.fence() as fence:
                fence.begin();fence.touch_store()
                with self.session() as store:
                    integration.receive_fixture(fence,store,raw([update()]),0,NOW)
                    integration.prepare_fixture(fence,store,1,lambda:None,NOW)
                    self.assertEqual(store.deliver(1,outbox.ScriptedTransport(),NOW,permission=PERMISSION),"confirmed")
                fence.seal()

    def test_receipt_identity_collision_and_clock_reversal_remain_closed(self):
        rejected={"update_id":10,"message":{"text":"synthetic outsider"}}
        with self.fence() as fence:
            fence.begin();fence.touch_store()
            with self.session() as store:
                integration.receive_fixture(fence,store,raw([rejected]),0,NOW)
                with self.assertRaises(base.StoreError):integration.receive_fixture(fence,store,raw([update()]),11,NOW)
            with self.session() as store:
                self.assertEqual((store.status()["stored_offset"],store.status()["messages"]),(11,0))
            fence.seal()
        # Le magasin B1 gère lui-même le recul de l'horloge, sans lecture de vue.
        with self.session(now=NOW-1) as store:
            self.assertEqual(store.status()["state"],"recovery_required")

    def test_corrupt_store_is_preserved_and_not_reinitialized(self):
        path=self.store/"fixtures.sqlite3"
        path.write_bytes(b"corrupt synthetic store")
        with self.fence() as fence:
            with self.assertRaises(recovery.FenceError):fence.begin()
        with self.assertRaises(base.StoreError):
            with self.session():pass
        self.assertEqual(path.read_bytes(),b"corrupt synthetic store")

    def test_functional_qualification_backup_restore_remains_disarmed(self):
        # ACL simulées : aucune mutation d'ACL, aucune preuve Windows revendiquée.
        with ExitStack() as stack:
            if os.name=="nt":
                stack.enter_context(patch.object(q,"_powershell"))
                stack.enter_context(patch.object(q.credential_acl,"inspect",side_effect=synthetic_acl))
            with q.QualificationArea(parent=self.root) as area:
                source=area.reserve("source")
                with q.QualifiedStore(source,CONFIG,54321,NOW,area=area,store_id=SOURCE,generation=GEN,create=True):pass
                with recovery.FixtureFence(self.root/"qualified-fence",self.root/"qualified-witness",
                        source/"fixtures.sqlite3",BINDING,NOW,create=True) as fence:
                    fence.begin();fence.touch_store()
                    with q.QualifiedStore(source,CONFIG,54321,NOW,area=area,store_id=SOURCE,generation=GEN) as store:
                        integration.receive_fixture(fence,store,raw([update()]),0,NOW)
                        response=integration.prepare_fixture(fence,store,1,lambda:None,NOW)
                        transport=outbox.ScriptedTransport()
                        self.assertEqual(store.deliver(1,transport,NOW,permission=PERMISSION),"confirmed")
                        reply={"ok":True,"result":{"message_id":1000001,
                            "chat":{"id":12345,"type":"private"},"text":unescape(response["text"])}}
                        self.assertEqual(contracts.validate_message(json.dumps(reply).encode(),200,12345,response["text"])["status"],"confirmed")
                        self.assertEqual(len(transport.requests),1)
                        store.backup_fixture(area.reserve("backup"),NOW)
                    fence.seal()
                destination=area.reserve("restored")
                q.restore_fixture(area,area.root/"backup",destination,CONFIG,54321,NOW,generation=GEN,
                    source_id=SOURCE,store_id="44444444-4444-4444-8444-444444444444")
                with q.QualifiedStore(destination,CONFIG,54321,NOW,area=area,
                        store_id="44444444-4444-4444-8444-444444444444",generation=GEN) as store:
                    self.assertEqual(store.inspect_outbox(1)["status"],"confirmed")
                    with self.assertRaises(base.StoreError):store.deliver(1,outbox.ScriptedTransport(),NOW,permission=PERMISSION)


CONTROLLER=r'''
import json,os,sys,time
from pathlib import Path
from tools.windows_watcher.private_recovery import FixtureFence
from tools.windows_watcher.private_fenced_job import FencedFixtureJob
root,stage,dirty=sys.argv[1:]
root=Path(root)
binding=dict(simulation_only=True,bot_id=54321,owner_id=12345,chat_id=12345,generation='11111111-1111-4111-8111-111111111111')
with FixtureFence(root/'local',root/'witness',root/'store/fixtures.sqlite3',binding,1800000000) as fence:
    fence.begin()
    if dirty=='yes':fence.touch_store()
    def crash(point):
        if point==stage:
            (root/'ready.json').write_text(json.dumps(dict(identity=fence.identity,state=fence.state)))
            while not (root/'go.json').exists():time.sleep(.01)
            os._exit(79)
    job=FencedFixtureJob(root/'network',8,fence,fault=crash).start()
    while not (root/'network/descendant.json').exists():time.sleep(.01)
    crash('during_request')
    job.stop()
'''


INTEGRATED_CONTROLLER=r'''
import json,os,sys,time
from pathlib import Path
from tools.windows_watcher.private_recovery import FixtureFence
from tools.windows_watcher.private_fenced_job import FencedFixtureJob
from tools.windows_watcher.private_outbox import DeliveryStore,ScriptedTransport,SimulationPermission
from tools.windows_watcher.private_integration import receive_fixture,prepare_fixture
root=Path(sys.argv[1])
generation='11111111-1111-4111-8111-111111111111'
config=dict(mode='simulation',send_enabled=False,notifications_channel_id=None,owner_user_id=12345,arbitration_private_chat_id=12345)
binding=dict(simulation_only=True,bot_id=54321,owner_id=12345,chat_id=12345,generation=generation)
with FixtureFence(root/'local',root/'witness',root/'store/fixtures.sqlite3',binding,1800000000) as fence:
    fence.begin();fence.touch_store()
    def crash(stage):
        if stage=='after_intent_commit':
            job=FencedFixtureJob(root/'network',8,fence).start()
            (root/'ready.json').write_text(json.dumps(dict(identity=fence.identity,state=fence.state)))
            while not (root/'go.json').exists():time.sleep(.01)
            os._exit(80)
    with DeliveryStore(root/'store',config,54321,1800000000,generation=generation,fault=crash) as store:
        message=dict(message_id=1,date=1800000000,text='/status',chat=dict(id=12345,type='private'),**{'from':dict(id=12345,is_bot=False)})
        receive_fixture(fence,store,json.dumps(dict(ok=True,result=[dict(update_id=10,message=message)])).encode(),0,1800000000)
        prepare_fixture(fence,store,1,lambda:None,1800000000)
        store.deliver(1,ScriptedTransport(),1800000000,permission=SimulationPermission(54321,12345,12345,generation,True))
'''


@unittest.skipUnless(os.name=="nt","C2 native Windows fencing qualification")
class NativeRecoveryTests(FixtureSetup,unittest.TestCase):
    def test_context_exit_reaps_and_unsafe_exit_retains_both_fences(self):
        root=self.root/"network";root.mkdir();request(root)
        fence=self.fence().__enter__()
        fence.begin()
        job=native.FencedFixtureJob(root,2,fence).start()
        try:
            with patch.object(job.api,"TerminateJobObject",return_value=0):
                with self.assertRaises(native.jobs.UnsafeStop):fence.__exit__(None,None,None)
            self.assertFalse(job.safe)
            self.assertEqual(len(fence.connections),2)
            with self.assertRaises(watcher.WatcherError):
                with watcher.single_instance_lock(self.root/"witness/fence.lock"):pass
        finally:
            fence.__exit__(None,None,None)
        self.assertTrue(job.safe)
        self.assertEqual(job.final_counts["active"],0)
        with self.fence() as newer:
            with self.assertRaises(recovery.FenceError):newer.begin()

    def test_controller_death_after_local_acceptance_keeps_uncertain_and_retired(self):
        root=self.root/"network";root.mkdir()
        observation=None
        with lab.LoopbackLab(root,mode="headers_block") as server:
            request(root,"request",server.port)
            controller=subprocess.Popen([sys.executable,"-c",INTEGRATED_CONTROLLER,str(self.root)],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                ready=wait_file(self.root/"ready.json")
                self.assertTrue(server.request_received.wait(2))
                observation=native.WindowsObservation(**ready)
                (self.root/"go.json").write_text("{}")
                controller.communicate(timeout=4)
                self.assertEqual(controller.returncode,80)
                with self.fence() as fence:
                    with self.assertRaises(recovery.FenceError):fence.begin()
                    fence.recover_observed(observation)
                    self.assertEqual(fence.state["phase"],"retired")
                    with self.assertRaises(recovery.FenceError):fence.begin()
                # Réconciliation de la fixture intacte après preuve OS, pas réarmement.
                with self.session() as store:
                    self.assertEqual((store.status()["receipts"],store.status()["stored_offset"]),(1,11))
                    self.assertEqual(store.inspect_outbox(1)["status"],"uncertain")
                    self.assertEqual(store.connection.execute("SELECT count(*) FROM attempts").fetchone()[0],1)
                    transport=outbox.ScriptedTransport()
                    self.assertEqual(store.deliver(1,transport,NOW,permission=PERMISSION),"uncertain")
                    self.assertEqual(transport.requests,[])
                with self.fence() as fence:
                    with self.assertRaises(recovery.FenceError):fence.begin()
            finally:
                if controller.poll() is None:controller.kill();controller.communicate(timeout=4)
                if observation:observation.terminate_and_prove();observation.close()

    def test_stop_still_reaps_if_durable_witness_fails(self):
        root=self.root/"network";root.mkdir();request(root)
        with self.fence() as fence:
            fence.begin()
            job=native.FencedFixtureJob(root,2,fence).start()
            with patch.object(fence,"stopping",side_effect=OSError("synthetic disk failure")):
                with self.assertRaisesRegex(recovery.FenceError,"STOP_PROOF_WRITE_FAILED"):job.stop()
            self.assertTrue(job.safe)
            self.assertEqual(job.final_counts["active"],0)
            self.assertTrue(fence.poisoned)
            with self.assertRaises(recovery.FenceError):fence.seal()
        with self.fence() as fence:
            with self.assertRaises(recovery.FenceError):fence.begin()

    def test_pid_birth_mismatch_and_missing_job_handle_are_not_proof(self):
        root=self.root/"network";root.mkdir();request(root)
        with self.fence() as fence:
            fence.begin()
            job=native.FencedFixtureJob(root,2,fence).start()
            try:
                wrong=json.loads(json.dumps(fence.state));wrong["owner"]["birth"]+=1
                with self.assertRaisesRegex(recovery.FenceError,"CONTROLLER_NOT_CAPTURED_ALIVE"):
                    native.WindowsObservation(fence.identity,wrong)
            finally:job.stop()
            with self.assertRaisesRegex(recovery.FenceError,"JOB_NOT_CAPTURED"):
                native.WindowsObservation(fence.identity,fence.state)
            fence.seal()

    def test_atomic_creation_in_job_and_generation_bound_result(self):
        root=self.root/"network";root.mkdir()
        with self.fence() as fence:
            session=fence.begin()
            with lab.LoopbackLab(root) as server:
                request(root,"request",server.port)
                job=native.FencedFixtureJob(root,2,fence)
                job.api.AssignProcessToJobObject=lambda *args: (_ for _ in ()).throw(AssertionError("non atomic assignment"))
                job.start()
                self.addCleanup(job.stop)
                inside=native.w.BOOL()
                self.assertTrue(job.api.IsProcessInJob(job.process.process,job.job,ctypes.byref(inside)))
                self.assertTrue(inside.value)
                result=job.wait()
                self.assertTrue(result["safe_stop"])
                self.assertEqual(result["result"]["generation"],session)
                recovery.accept_result(fence,session,result["result"])
            fence.seal()

    def test_controller_crashes_and_surviving_descendants_require_retained_os_evidence(self):
        for stage in ("before_job","after_job","after_process","during_request","during_stop"):
            with self.subTest(stage=stage),tempfile.TemporaryDirectory(prefix="phase823-c2-") as temp:
                root=Path(temp);store=root/"store"
                with outbox.DeliveryStore(store,CONFIG,54321,NOW,generation=GEN,create=True):pass
                def fence():return recovery.FixtureFence(root/"local",root/"witness",store/"fixtures.sqlite3",BINDING,NOW)
                with recovery.FixtureFence(root/"local",root/"witness",store/"fixtures.sqlite3",BINDING,NOW,create=True):pass
                network=root/"network";network.mkdir();request(network)
                controller=subprocess.Popen([sys.executable,"-c",CONTROLLER,str(root),stage,"no"],
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
                observation=None
                try:
                    ready=wait_file(root/"ready.json")
                    if stage=="before_job":
                        with self.assertRaises(recovery.FenceError):native.WindowsObservation(**ready)
                    else:
                        observation=native.WindowsObservation(**ready)
                        with self.assertRaisesRegex(recovery.FenceError,"CONTROLLER_STILL_ALIVE"):
                            observation.terminate_and_prove()
                    (root/"go.json").write_text("{}")
                    controller.communicate(timeout=4)
                    self.assertEqual(controller.returncode,79)
                    with fence() as session:
                        with self.assertRaisesRegex(recovery.FenceError,"PREVIOUS_SESSION_UNPROVEN"):session.begin()
                        if observation:
                            if stage=="during_request":
                                counts=native.jobs.Accounting()
                                self.assertTrue(observation.api.QueryInformationJobObject(observation.job,1,ctypes.byref(counts),ctypes.sizeof(counts),None))
                                self.assertGreater(counts.active,0)  # Le témoin conserve le job.
                            session.recover_observed(observation)
                            self.assertTrue(observation.proven)
                            fresh=session.begin()
                            self.assertNotEqual(fresh,ready["state"]["session"])
                            with self.assertRaises(recovery.FenceError):recovery.accept_result(session,ready["state"]["session"],{})
                            session.seal()
                finally:
                    if controller.poll() is None:controller.kill();controller.communicate(timeout=4)
                    if observation:
                        observation.terminate_and_prove();observation.close()

    def test_dirty_crash_even_with_job_proof_remains_retired_and_disarmed(self):
        root=self.root/"network";root.mkdir();request(root)
        controller=subprocess.Popen([sys.executable,"-c",CONTROLLER,str(self.root),"during_request","yes"],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
        observation=None
        try:
            ready=wait_file(self.root/"ready.json")
            observation=native.WindowsObservation(**ready)
            (self.root/"go.json").write_text("{}")
            controller.communicate(timeout=4)
            with self.fence() as fence:
                fence.recover_observed(observation)
                self.assertEqual(fence.state["phase"],"retired")
                with self.assertRaises(recovery.FenceError):fence.begin()
        finally:
            if controller.poll() is None:controller.kill();controller.communicate(timeout=4)
            if observation:observation.terminate_and_prove();observation.close()

    def test_real_loopback_acceptance_lost_after_intent_preserves_uncertain(self):
        network=self.root/"network";network.mkdir()
        with self.fence() as fence:
            fence.begin();fence.touch_store()
            with lab.LoopbackLab(network,mode="headers_block") as server:
                request(network,"request",server.port)
                def fail(stage):
                    if stage=="after_intent_commit":
                        job=native.FencedFixtureJob(network,.8,fence).start()
                        result=job.wait()
                        self.assertTrue(result["safe_stop"])
                        self.assertTrue(server.request_received.is_set())
                        raise OSError("fixture response lost")
                with self.session(fault=fail) as store:
                    integration.receive_fixture(fence,store,raw([update()]),0,NOW)
                    integration.prepare_fixture(fence,store,1,lambda:None,NOW)
                    with self.assertRaises(base.StoreError):store.deliver(1,outbox.ScriptedTransport(),NOW,permission=PERMISSION)
                with self.session() as store:self.assertEqual(store.inspect_outbox(1)["status"],"uncertain")
            fence.seal()
        with self.fence() as fence:
            fence.begin();fence.touch_store()
            with self.session() as store:
                transport=outbox.ScriptedTransport()
                self.assertEqual(store.inspect_outbox(1)["status"],"uncertain")
                # L'échec disque simulé a aussi désarmé B1. Aucun réarmement implicite.
                with self.assertRaises(base.StoreError):store.deliver(1,transport,NOW,permission=PERMISSION)
                self.assertEqual(transport.requests,[])
            fence.seal()


if __name__=="__main__":unittest.main()
