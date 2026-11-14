"""C3 : quotas portables et secret synthétique / pipe / intégration Windows."""
from contextlib import closing
from html import unescape
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from tools.windows_watcher import private_contracts as contracts, private_credential as secret
from tools.windows_watcher import private_fenced_job as native, private_integration as integration
from tools.windows_watcher import private_local_lab as lab, private_outbox as outbox
from tools.windows_watcher import private_recovery as recovery, private_store as base, private_trial as trial


GEN="11111111-1111-4111-8111-111111111111"
HOST="22222222-2222-4222-8222-222222222222"
TRIAL="33333333-3333-4333-8333-333333333333"
NOW=1800000000
CONFIG=dict(mode="simulation",send_enabled=False,notifications_channel_id=None,
            owner_user_id=12345,arbitration_private_chat_id=12345)
BINDING=dict(simulation_only=True,bot_id=54321,owner_id=12345,chat_id=12345,generation=GEN)
ENROLLMENT=trial.ManualFixtureEnrollment(54321,12345,12345,GEN,HOST,TRIAL)
PLAN=trial.FixturePlan(fixture_enabled=True)
PERMISSION=outbox.SimulationPermission(54321,12345,12345,GEN,True)


class Clock:
    def __init__(self):self.wall,self.mono=NOW,1000
    def advance(self,seconds):self.wall+=seconds;self.mono+=seconds


def request(root,port=0,tls=False):
    (root/"request.json").write_text(json.dumps(dict(mode="request",port=port,delay=0,tls=tls,size=1)))


def update():
    return dict(update_id=10,message=dict(message_id=1,date=NOW,text="/status",
        chat=dict(id=12345,type="private"),**{"from":dict(id=12345,is_bot=False)}))


def wait_file(path):
    until=time.monotonic()+4
    while time.monotonic()<until:
        try:return json.loads(path.read_text())
        except (OSError,ValueError):time.sleep(.01)
    raise AssertionError("fixture not ready")


class TrialSetup:
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="phase823-c3-")
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.clock=Clock()

    def ledger(self,name="trial",create=True,plan=PLAN,enrollment=ENROLLMENT):
        return trial.FixtureTrial(self.root/name,enrollment,plan,create=create,
                                  wall=lambda:self.clock.wall,monotonic=lambda:self.clock.mono)

    @staticmethod
    def authorize(ledger):
        ledger.authorize_fixture(approval="reviewed-synthetic-trial",enrollment=ENROLLMENT)


class TrialTests(TrialSetup,unittest.TestCase):
    def test_default_disabled_and_no_token_can_grant_authorization(self):
        with self.ledger(plan=trial.FixturePlan()) as ledger:
            with self.assertRaises(trial.TrialError):self.authorize(ledger)
            with self.assertRaises(trial.TrialError):ledger.reserve("read")
            self.assertEqual(ledger.status(),dict(state="disabled",reads=0,responses=0))

    def test_plan_rejects_network_and_limits_above_human_proposal(self):
        for changes in (dict(telegram_enabled=True),dict(telegram_enabled=0),dict(duration_seconds=301),
                        dict(read_limit=21),dict(response_limit=7),dict(read_limit=True),dict(duration_seconds=0)):
            with self.assertRaises(trial.TrialError):trial.FixturePlan(**changes)

    def test_manual_numeric_enrollment_does_not_learn_from_incoming_message(self):
        with self.assertRaises(trial.TrialError):
            trial.FixtureTrial(self.root/"bad",update())
        for value in (True,"12345",-1,None):
            with self.assertRaises(trial.TrialError):trial.ManualFixtureEnrollment(54321,value,12345,GEN,HOST,TRIAL)
        with self.ledger() as ledger:
            for approval,enrollment in (("",ENROLLMENT),("reviewed-synthetic-trial",update()),
                ("reviewed-synthetic-trial",trial.ManualFixtureEnrollment(99999,12345,12345,GEN,HOST,TRIAL))):
                with self.assertRaises(trial.TrialError):ledger.authorize_fixture(approval=approval,enrollment=enrollment)
            self.assertEqual(ledger.status()["state"],"disabled")

    def test_exact_20_reads_6_responses_and_no_refund_on_reopen(self):
        with self.ledger() as ledger:
            self.authorize(ledger)
            for _ in range(6):ledger.reserve("response")
            for _ in range(20):ledger.reserve("read")
            with self.assertRaisesRegex(trial.TrialError,"TRIAL_QUOTA"):ledger.reserve("read")
            self.assertEqual(ledger.status(),dict(state="exhausted",reads=20,responses=6))
        with self.ledger(create=False) as ledger:
            self.assertEqual(ledger.status(),dict(state="exhausted",reads=20,responses=6))
            with self.assertRaises(trial.TrialError):self.authorize(ledger)

    def test_seventh_response_is_refused_even_when_reads_remain(self):
        with self.ledger() as ledger:
            self.authorize(ledger)
            for _ in range(6):ledger.reserve("response")
            with self.assertRaisesRegex(trial.TrialError,"TRIAL_QUOTA"):ledger.reserve("response")
            self.assertEqual(ledger.status()["responses"],6)

    def test_300_second_deadline_and_both_clocks(self):
        for name,wall,mono,reason in (("expiry",300,300,"TRIAL_EXPIRED"),
                ("wall-back",-1,1,"CLOCK_REVERSED"),("mono-back",1,-1,"CLOCK_REVERSED"),
                ("wall-forward",301,1,"TRIAL_EXPIRED")):
            self.clock=Clock()
            with self.subTest(name=name),self.ledger(name) as ledger:
                self.authorize(ledger)
                self.clock.wall+=wall;self.clock.mono+=mono
                with self.assertRaisesRegex(trial.TrialError,reason):ledger.reserve("read")
                self.assertEqual(ledger.status()["reads"],0)

    def test_close_and_revocation_are_terminal_not_boot_authorizations(self):
        with self.ledger() as ledger:
            self.authorize(ledger);ledger.reserve("read")
        with self.ledger(create=False) as ledger:
            self.assertEqual(ledger.status()["state"],"closed")
            with self.assertRaises(trial.TrialError):self.authorize(ledger)
        with self.ledger("revoked") as ledger:
            self.authorize(ledger);ledger.emergency_stop()
        with self.ledger("revoked",create=False) as ledger:
            self.assertEqual(ledger.status()["state"],"revoked")
            with self.assertRaises(trial.TrialError):ledger.reserve("read")
        with self.ledger("revoked-before-approval") as ledger:ledger.emergency_stop()
        with self.ledger("revoked-before-approval",create=False) as ledger:
            self.assertEqual(ledger.status()["state"],"revoked")

    def test_real_process_crash_then_simulated_reboot_never_resumes(self):
        with self.ledger():pass
        script=r'''
import os,sys
from tools.windows_watcher.private_trial import FixtureTrial,FixturePlan,ManualFixtureEnrollment
e=ManualFixtureEnrollment(54321,12345,12345,'11111111-1111-4111-8111-111111111111','22222222-2222-4222-8222-222222222222','33333333-3333-4333-8333-333333333333')
with FixtureTrial(sys.argv[1],e,FixturePlan(fixture_enabled=True)) as trial:
    trial.authorize_fixture(approval='reviewed-synthetic-trial',enrollment=e)
    trial.reserve('response')
    os._exit(88)
'''
        result=subprocess.run([sys.executable,"-c",script,str(self.root/"trial")],capture_output=True,timeout=5,
                              creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        self.assertEqual(result.returncode,88)
        with self.assertRaisesRegex(trial.TrialError,"REBOOT_OR_CRASH"):
            with self.ledger(create=False):pass
        with self.ledger(create=False) as ledger:
            self.assertEqual(ledger.status(),dict(state="recovery_required",reads=0,responses=1))
            with self.assertRaises(trial.TrialError):self.authorize(ledger)

    def test_corruption_missing_checkpoint_and_schema_never_reset(self):
        for name in ("corrupt","missing","schema"):
            with self.ledger(name):pass
            path=self.root/name/"trial.sqlite3"
            if name=="corrupt":path.write_bytes(b"synthetic corruption")
            elif name=="missing":path.unlink()
            else:
                with closing(sqlite3.connect(path)) as db:db.execute("PRAGMA user_version=99")
            previous=path.read_bytes() if path.exists() else None
            with self.assertRaises(trial.TrialError):
                with self.ledger(name,create=False):pass
            self.assertEqual(path.read_bytes() if path.exists() else None,previous)

    def test_failed_update_does_not_consume_permission_or_open_socket(self):
        with self.ledger() as ledger:
            self.authorize(ledger)
            ledger.db.set_authorizer(lambda action,*args: sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_UPDATE else sqlite3.SQLITE_OK)
            with patch("socket.socket",side_effect=AssertionError("network forbidden")):
                with self.assertRaises(sqlite3.DatabaseError):ledger.reserve("response")
            self.assertEqual(ledger.status()["responses"],0)
        with self.assertRaises(trial.TrialError):
            with self.ledger(create=False):pass

    def test_old_host_generation_trial_and_concurrent_reader_refused(self):
        with self.ledger():
            with self.assertRaises(trial.TrialError):
                with self.ledger(create=False):pass
        for enrollment in (trial.ManualFixtureEnrollment(54321,12345,12345,str(uuid4()),HOST,TRIAL),
                           trial.ManualFixtureEnrollment(54321,12345,12345,GEN,str(uuid4()),TRIAL)):
            with self.assertRaises(trial.TrialError):
                with self.ledger(create=False,enrollment=enrollment):pass

    def test_synthetic_interface_rejects_every_other_format_before_dpapi(self):
        for value in (b"not-synthetic",bytearray(b"not-synthetic"),"not-synthetic",bytearray(b"C3-SYNTHETIC-")):
            with patch.object(secret,"_dpapi",side_effect=AssertionError("no credential call")):
                with self.assertRaisesRegex(secret.CredentialError,"SYNTHETIC_ONLY"):secret.SyntheticVault(value)


@unittest.skipUnless(os.name=="nt","C3 native DPAPI, handle inheritance and Job Object")
class NativeTrialTests(TrialSetup,unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store=self.root/"store"
        with self.session(create=True):pass
        with self.fence(create=True):pass

    def session(self,**kwargs):
        return outbox.DeliveryStore(self.store,CONFIG,54321,NOW,generation=GEN,**kwargs)

    def fence(self,**kwargs):
        return recovery.FixtureFence(self.root/"fence",self.root/"witness",self.store/"fixtures.sqlite3",BINDING,NOW,**kwargs)

    def vault(self):
        value=bytearray(b"C3-SYNTHETIC-")+bytearray(uuid4().hex,"ascii")
        return secret.SyntheticVault(value)

    def test_native_dpapi_integrity_revocation_and_buffer_cleanup(self):
        value=bytearray(b"C3-SYNTHETIC-")+bytearray(uuid4().hex,"ascii")
        saved=bytearray(value)
        vault=secret.SyntheticVault(value)
        self.assertFalse(any(value))
        observed=[]
        vault.use(lambda plaintext:observed.append(plaintext))
        self.assertFalse(any(observed[0]))
        self.assertNotIn(saved,vault.blob)
        self.assertNotIn(saved.decode(),repr(vault))
        vault.blob[-1]^=1
        with self.assertRaises(secret.CredentialError):vault.use(lambda _:None)
        vault.close()
        self.assertFalse(any(vault.blob))
        with self.assertRaises(secret.CredentialError):vault.use(lambda _:None)
        secret.wipe(saved)

    def test_malformed_pipe_secret_is_refused_before_any_local_connection(self):
        network=self.root/"network";network.mkdir()
        with self.ledger() as ledger,self.fence() as fence:
            self.authorize(ledger);fence.begin()
            with lab.LoopbackLab(network) as server:
                request(network,server.port)
                vault=self.vault()
                job=secret.CredentialFixtureJob(network,2,fence,ledger,vault)
                with patch.object(vault,"use",side_effect=lambda consumer:consumer(bytearray(b"invalid-fixture"))):
                    with self.assertRaises(recovery.FenceError):job.start().wait()
                self.assertTrue(job.safe)
                self.assertTrue(vault.closed)
                self.assertEqual(server.peers,[])
            fence.seal()

    def test_controller_crash_with_secret_channel_leaves_uncertain_and_no_reboot_resume(self):
        with self.ledger():pass
        network=self.root/"network";network.mkdir()
        script=r'''
import json,os,sys,time
from pathlib import Path
from uuid import uuid4
from tools.windows_watcher.private_trial import FixtureTrial,FixturePlan,ManualFixtureEnrollment
from tools.windows_watcher.private_credential import SyntheticVault,CredentialFixtureJob
from tools.windows_watcher.private_recovery import FixtureFence
from tools.windows_watcher.private_outbox import DeliveryStore,ScriptedTransport,SimulationPermission
from tools.windows_watcher.private_integration import receive_fixture,prepare_fixture
root=Path(sys.argv[1]);generation='11111111-1111-4111-8111-111111111111'
e=ManualFixtureEnrollment(54321,12345,12345,generation,'22222222-2222-4222-8222-222222222222','33333333-3333-4333-8333-333333333333')
config=dict(mode='simulation',send_enabled=False,notifications_channel_id=None,owner_user_id=12345,arbitration_private_chat_id=12345)
binding=dict(simulation_only=True,bot_id=54321,owner_id=12345,chat_id=12345,generation=generation)
with FixtureTrial(root/'trial',e,FixturePlan(fixture_enabled=True)) as ledger,FixtureFence(root/'fence',root/'witness',root/'store/fixtures.sqlite3',binding,1800000000) as fence:
    ledger.authorize_fixture(approval='reviewed-synthetic-trial',enrollment=e)
    fence.begin();fence.touch_store()
    def interrupt(stage):
        if stage=='after_intent_commit':
            value=bytearray(b'C3-SYNTHETIC-')+bytearray(uuid4().hex,'ascii')
            vault=SyntheticVault(value)
            job=CredentialFixtureJob(root/'network',8,fence,ledger,vault,action='response').start()
            (root/'ready.json').write_text(json.dumps(dict(identity=fence.identity,state=fence.state)))
            while not (root/'go.json').exists():time.sleep(.01)
            os._exit(89)
    with DeliveryStore(root/'store',config,54321,1800000000,generation=generation,fault=interrupt) as store:
        message=dict(message_id=1,date=1800000000,text='/status',chat=dict(id=12345,type='private'),**{'from':dict(id=12345,is_bot=False)})
        receive_fixture(fence,store,json.dumps(dict(ok=True,result=[dict(update_id=10,message=message)])).encode(),0,1800000000)
        prepare_fixture(fence,store,1,lambda:None,1800000000)
        store.deliver(1,ScriptedTransport(),1800000000,permission=SimulationPermission(54321,12345,12345,generation,True))
'''
        observation=None
        with lab.LoopbackLab(network,mode="headers_block") as server:
            request(network,server.port)
            controller=subprocess.Popen(
                [sys.executable,"-c",script,str(self.root)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                ready=wait_file(self.root/"ready.json")
                self.assertTrue(server.request_received.wait(2))
                observation=native.WindowsObservation(**ready)
                (self.root/"go.json").write_text("{}")
                controller.communicate(timeout=4)
                self.assertEqual(controller.returncode,89)
                with self.fence() as fence:
                    fence.recover_observed(observation)
                    self.assertEqual(fence.state["phase"],"retired")
                    with self.assertRaises(recovery.FenceError):fence.begin()
                with self.assertRaisesRegex(trial.TrialError,"REBOOT_OR_CRASH"):
                    with self.ledger(create=False):pass
                with self.ledger(create=False) as ledger:
                    self.assertEqual(ledger.status(),dict(state="recovery_required",reads=0,responses=1))
                    with self.assertRaises(trial.TrialError):self.authorize(ledger)
                with self.session() as store:
                    self.assertEqual(store.inspect_outbox(1)["status"],"uncertain")
                    self.assertEqual(store.connection.execute("SELECT count(*) FROM attempts").fetchone()[0],1)
            finally:
                if controller.poll() is None:controller.kill();controller.communicate(timeout=4)
                if observation:observation.terminate_and_prove();observation.close()

    def test_secret_presence_without_explicit_approval_never_creates_a_worker(self):
        network=self.root/"network";network.mkdir();request(network)
        vault=self.vault()
        try:
            with self.ledger() as ledger,self.fence() as fence:
                fence.begin()
                with self.assertRaises(trial.TrialError):secret.CredentialFixtureJob(network,2,fence,ledger,vault)
                self.assertIsNone(fence.local_job)
                fence.seal()
        finally:vault.close()

    def test_native_pipe_tls_no_plaintext_in_command_environment_or_files(self):
        network=self.root/"network";network.mkdir()
        plaintext=bytearray(b"C3-SYNTHETIC-")+bytearray(uuid4().hex,"ascii")
        marker=bytes(plaintext)  # Uniquement la valeur factice à rechercher dans les preuves.
        vault=secret.SyntheticVault(plaintext)
        with self.ledger() as ledger,self.fence() as fence:
            self.authorize(ledger);fence.begin()
            with lab.LoopbackLab(network,tls=True) as server:
                request(network,server.port,True)
                job=secret.CredentialFixtureJob(network,3,fence,ledger,vault)
                original=job.api.CreateProcessW
                def inspect(*args):
                    self.assertNotIn(marker.decode(),args[1].value)
                    self.assertNotIn(marker.decode(),"".join(args[6]))
                    self.assertTrue(args[4])
                    attributes=job._creation_attributes()
                    self.assertEqual([key for key,_ in attributes],[0x2000D,0x20002])
                    self.assertEqual(list(attributes[1][1]),[job.read_pipe.value])
                    return original(*args)
                job.api.CreateProcessW=inspect
                result=job.start().wait()
                self.assertTrue(result["safe_stop"])
                self.assertEqual(result["result"]["credential"],"synthetic-consumed")
                self.assertTrue(vault.closed)
                self.assertFalse(any(vault.blob))
                self.assertEqual((job.read_pipe.value,job.write_pipe.value),(None,None))
                self.assertEqual(server.peers,["127.0.0.1"])
            self.assertEqual(ledger.status()["reads"],1)
            for path in self.root.rglob("*"):
                if path.is_file():
                    if path.suffix==".lock":self.assertLessEqual(path.stat().st_size,1)
                    else:self.assertNotIn(marker,path.read_bytes())
            with self.assertRaises(secret.CredentialError):job.start()
            fence.seal()

    def test_failed_pipe_transfer_consumes_quota_and_cleans_without_resuming(self):
        network=self.root/"network";network.mkdir();request(network)
        with self.ledger() as ledger,self.fence() as fence:
            self.authorize(ledger);fence.begin()
            vault=self.vault()
            job=secret.CredentialFixtureJob(network,2,fence,ledger,vault)
            with patch.object(job.api,"WriteFile",return_value=0):
                with self.assertRaisesRegex(secret.CredentialError,"PIPE_TRANSFER_REFUSED"):job.start()
            self.assertTrue(job.safe)
            self.assertTrue(vault.closed)
            self.assertNotIn("resumed",job.trace)
            self.assertEqual(ledger.status()["reads"],1)
            with self.assertRaises(trial.TrialError):ledger.reserve("read")
            self.assertFalse((network/"phase.json").exists())
            fence.seal()

    def test_unsafe_emergency_stop_retains_trial_lock_and_wipes_parent_secret(self):
        network=self.root/"network";network.mkdir()
        with self.ledger() as ledger,self.fence() as fence:
            self.authorize(ledger);fence.begin()
            with lab.LoopbackLab(network,mode="headers_block") as server:
                request(network,server.port)
                vault=self.vault()
                job=secret.CredentialFixtureJob(network,3,fence,ledger,vault).start()
                try:
                    with patch.object(job.api,"TerminateJobObject",return_value=0):
                        with self.assertRaises(native.jobs.UnsafeStop):ledger.emergency_stop()
                    self.assertFalse(job.safe)
                    self.assertIsNotNone(ledger.db)
                    self.assertTrue(vault.closed)
                    with self.assertRaises(trial.TrialError):
                        with self.ledger(create=False):pass
                finally:ledger.emergency_stop()
            self.assertTrue(job.safe)
            fence.seal()

    def test_trial_deadline_caps_native_worker_without_waiting_five_minutes(self):
        network=self.root/"network";network.mkdir()
        plan=trial.FixturePlan(duration_seconds=1,fixture_enabled=True)
        with trial.FixtureTrial(self.root/"trial",ENROLLMENT,plan,create=True) as ledger,self.fence() as fence:
            self.authorize(ledger);fence.begin()
            with lab.LoopbackLab(network,mode="headers_block") as server:
                request(network,server.port)
                started=time.monotonic()
                result=secret.CredentialFixtureJob(network,5,fence,ledger,self.vault()).start().wait()
                self.assertLess(time.monotonic()-started,2.5)
                self.assertTrue(result["safe_stop"])
                self.assertFalse(result["timely"])
                self.assertIsNone(result["result"])
                self.assertEqual(ledger.status()["state"],"expired")
            fence.seal()

    def test_revoke_active_worker_reaps_before_unlock_and_rejects_late_reply(self):
        network=self.root/"network";network.mkdir()
        with self.ledger() as ledger,self.fence() as fence:
            self.authorize(ledger);fence.begin()
            with lab.LoopbackLab(network,mode="late",delay=1) as server:
                request(network,server.port)
                vault=self.vault()
                job=secret.CredentialFixtureJob(network,3,fence,ledger,vault).start()
                self.assertTrue(server.request_received.wait(2))
                ledger.emergency_stop()
                self.assertTrue(job.safe)
                self.assertEqual(job.final_counts["active"],0)
                self.assertTrue(vault.closed)
                self.assertTrue(server.reply_attempted.wait(2))
                self.assertFalse((network/"result.json").exists())
                with self.assertRaises(trial.TrialError):ledger.reserve("read")
            fence.seal()
        with self.ledger(create=False) as ledger:self.assertEqual(ledger.status()["state"],"revoked")

    def test_complete_private_path_local_success_and_interruption(self):
        for mode in ("complete","headers_block"):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory(prefix="phase823-c3-") as temp:
                root=Path(temp);store_path=root/"store"
                def session(**kwargs):return outbox.DeliveryStore(store_path,CONFIG,54321,NOW,generation=GEN,**kwargs)
                def fence(**kwargs):return recovery.FixtureFence(root/"fence",root/"witness",store_path/"fixtures.sqlite3",BINDING,NOW,**kwargs)
                with session(create=True):pass
                with fence(create=True):pass
                with trial.FixtureTrial(root/"trial",ENROLLMENT,PLAN,create=True) as ledger:
                    self.authorize(ledger)
                    receive=root/"receive";receive.mkdir()
                    batch=json.dumps(dict(ok=True,result=[update(),dict(update_id=12,message=dict(text="outsider"))])).encode()
                    with fence() as current:
                        current.begin();current.touch_store()
                        with session() as store:
                            with lab.LoopbackLab(receive,body=batch) as server:
                                request(receive,server.port)
                                result=secret.CredentialFixtureJob(receive,2,current,ledger,self.vault()).start().wait()
                            received=integration.receive_fixture(current,store,result["result"]["body"].encode(),0,NOW)
                            self.assertEqual((received["receipts"],received["stored_offset"]),(2,13))
                            response=integration.prepare_fixture(current,store,1,lambda:None,NOW)
                        current.seal()
                    send=root/"response";send.mkdir()
                    body=json.dumps(dict(ok=True,result=dict(message_id=1000001,chat=dict(id=12345,type="private"),text=unescape(response["text"])))).encode()
                    with fence() as current:
                        current.begin();current.touch_store()
                        with lab.LoopbackLab(send,mode=mode,body=body) as server:
                            request(send,server.port)
                            def exchange(stage):
                                if stage=="after_intent_commit":
                                    result=secret.CredentialFixtureJob(send,.8,current,ledger,self.vault(),action="response").start().wait()
                                    self.assertTrue(server.request_received.is_set())
                                    if mode!="complete":raise OSError("synthetic response lost")
                                    confirmed=contracts.validate_message(result["result"]["body"].encode(),200,12345,response["text"])
                                    self.assertEqual(confirmed["status"],"confirmed")
                            with session(fault=exchange) as store:
                                transport=outbox.ScriptedTransport()
                                if mode=="complete":self.assertEqual(store.deliver(1,transport,NOW,permission=PERMISSION),"confirmed")
                                else:
                                    with self.assertRaises(base.StoreError):store.deliver(1,transport,NOW,permission=PERMISSION)
                            with session() as store:
                                self.assertEqual(store.inspect_outbox(1)["status"],"confirmed" if mode=="complete" else "uncertain")
                                self.assertEqual(store.connection.execute("SELECT count(*) FROM attempts").fetchone()[0],1)
                        current.seal()
                    self.assertEqual((ledger.status()["reads"],ledger.status()["responses"]),(1,1))


if __name__=="__main__":unittest.main()
