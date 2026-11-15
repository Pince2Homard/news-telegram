"""F3 : modèles transactionnels portables et qualification WinAPI explicite."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
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

from tools.windows_watcher import private_bot_adapter as api, private_credential as secret
from tools.windows_watcher import private_integration as integration, private_local_lab as lab
from tools.windows_watcher import private_outbox as outbox, private_recovery as recovery
from tools.windows_watcher import private_stop as stop, private_trial as trial, private_store as base
from tools.windows_watcher import private_qualification as qualification

GEN="11111111-1111-4111-8111-111111111111"
E=trial.ManualFixtureEnrollment(54321,12345,12345,GEN,
    "22222222-2222-4222-8222-222222222222","33333333-3333-4333-8333-333333333333")
TARGET=secret.PrivateFixtureTarget(E.trial_id,E.bot_id,GEN)
CONFIG=dict(mode="simulation",send_enabled=False,notifications_channel_id=None,
    owner_user_id=12345,arbitration_private_chat_id=12345)
BINDING=dict(simulation_only=True,bot_id=54321,owner_id=12345,chat_id=12345,generation=GEN)
NOW=1800000000


def update(mid=1,text="/help",**changes):
    message=dict(message_id=mid,date=NOW,text=text,**{"from":dict(id=12345,is_bot=False)},
        chat=dict(id=12345,type="private"))
    message.update(changes)
    return dict(update_id=mid*10,message=message)


def raw(values):return json.dumps(dict(ok=True,result=values)).encode()


class Clock:
    def __init__(self):self.value=NOW
    def __call__(self):return self.value


class ModelKernel:
    """Simulation de l'événement seulement : aucune preuve Windows."""
    def __init__(self):self.stopped=False
    def WaitForSingleObject(self,*_):return 0 if self.stopped else 258
    def SetEvent(self,*_):self.stopped=True;return 1
    def CloseHandle(self,*_):return 1


class Setup:
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="phase-f3-");self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.clock=Clock();self.store=self.root/"store"
        with self.session(create=True):pass
        with self.fence(create=True):pass

    def session(self,**kwargs):
        return outbox.DeliveryStore(self.store,CONFIG,54321,self.clock(),generation=GEN,**kwargs)

    def fence(self,**kwargs):
        return recovery.FixtureFence(self.root/"fence",self.root/"witness",
            self.store/"fixtures.sqlite3",BINDING,self.clock(),**kwargs)

    def network(self):
        root=self.root/uuid4().hex;root.mkdir();return root

    @contextmanager
    def chain(self,*,native=False,plan=None):
        with self.fence() as fence:
            fence.begin();fence.touch_store()
            with self.session() as store,trial.FixtureTrial(self.root/"trial",E,
                    plan or trial.FixturePlan(fixture_enabled=True),create=True,
                    wall=self.clock,monotonic=self.clock) as authorization:
                authorization.authorize_fixture(approval="reviewed-synthetic-trial",enrollment=E)
                if native:gate=stop.NativeStopGate(self.network(),fence.state["session"])
                else:
                    gate=stop.NativeStopGate.__new__(stop.NativeStopGate)
                    gate.api,gate.handle,gate.job,gate.data=ModelKernel(),1,None,{"session":fence.state["session"]}
                adapter=api.LocalBotAdapter(E,TARGET,api.LocalEndpoint(1),enabled=True,io_timeout=3)
                chain=integration.FixtureIntegration(fence,store,authorization,adapter,gate,clock=self.clock)
                try:yield chain
                finally:
                    if authorization.job is not None and not authorization.job.safe:authorization.job.stop()
                    gate.close()
            fence.seal()

    def provider(self):return secret.PinnedSyntheticProvider(TARGET)


class TransactionModels(Setup,unittest.TestCase):
    def test_whitelist_authentication_before_snapshot_and_existing_router_preserved(self):
        texts=("/help","/status","/pr","/ci","/start","/rapports","/help "," /status","/HELP","/help@fixture","/status arg","/status\n")
        with self.chain() as chain:
            values=[update(i+1,text) for i,text in enumerate(texts)]
            values += [update(30,"/status",chat=dict(id=99999,type="private")),
                       update(31,"/status",**{"from":dict(id=99999,is_bot=False)})]
            integration.receive_fixture(chain.fence,chain.store,raw(values),0,NOW)
            loader=unittest.mock.Mock(return_value=None)
            for i,text in enumerate(texts):
                result=integration.prepare_trial_fixture(chain.fence,chain.store,i+1,loader,NOW)
                self.assertEqual(result is not None,text in {"/help","/status"})
            for mid in (30,31):self.assertIsNone(integration.prepare_trial_fixture(chain.fence,chain.store,mid,loader,NOW))
            self.assertEqual(loader.call_count,1)
            help_text=json.loads(chain.store.connection.execute("SELECT response FROM business_results WHERE message_id=1").fetchone()[0])["text"]
            self.assertNotIn("/pr",help_text)
            # Le contexte historique garde ses commandes et sa documentation.
            self.assertEqual(integration.prepare_fixture(chain.fence,chain.store,3,lambda:None,NOW)["command"],"/pr")
            self.assertEqual(chain.store.status()["receipts"],14)
            self.assertEqual(chain.store.status()["messages"],12)

    def test_modeled_preflights_receipts_dedup_and_quota_before_transport(self):
        calls=[]
        with self.chain() as chain:
            def execute(adapter,root,call,fence,authorization,provider,**kwargs):
                # Simulateur explicitement substitué : pas de socket ni WinAPI.
                authorization.reserve("response" if call.method=="sendMessage" else "read")
                calls.append(call.method)
                if call.method=="getUpdates":return dict(classification="confirmed",data=[update()])
                return dict(classification="confirmed",data={})
            with patch.object(api.LocalBotAdapter,"execute",autospec=True,side_effect=execute):
                with self.assertRaises(api.TransportRefused):chain.receive(self.network(),self.provider())
                # Nouveau contexte requis après un refus d'admission ; aucun reset.
                self.assertEqual(calls,[])
        # Autre fixture indépendante, pas réarmement du budget terminal ci-dessus.
        with tempfile.TemporaryDirectory(prefix="phase-f3-") as temp:
            original=self.root;self.root=Path(temp);self.store=self.root/"store"
            with self.session(create=True):pass
            with self.fence(create=True):pass
            with self.chain() as chain,patch.object(api.LocalBotAdapter,"execute",autospec=True,side_effect=execute):
                for method in ("getMe","getWebhookInfo"):chain.preflight(method,self.network(),self.provider())
                self.assertEqual(chain.receive(self.network(),self.provider())["next_offset"],11)
                self.assertEqual(chain.receive(self.network(),self.provider())["new_messages"],0)
                self.assertEqual(chain.authorization.status()["reads"],4)
                self.assertEqual(chain.store.status()["receipts"],1)
            self.root=original

    def test_modeled_send_requires_durable_intent_and_commits_both_rows(self):
        with self.chain() as chain:
            chain.preflights={"getMe","getWebhookInfo"}
            integration.receive_fixture(chain.fence,chain.store,raw([update()]),0,NOW)
            chain.prepare(1,lambda:self.fail("help must not read snapshot"))
            def exchange(adapter,root,call,*args,**kwargs):
                chain.authorization.reserve("response")
                with closing(sqlite3.connect(chain.store.path)) as independent:
                    self.assertEqual(independent.execute("SELECT status FROM attempts").fetchone()[0],"in_flight")
                    self.assertEqual(independent.execute("SELECT status FROM outbox").fetchone()[0],"in_flight")
                return dict(classification="confirmed",data=dict(status="confirmed",message_id=7))
            with patch.object(api.LocalBotAdapter,"execute",autospec=True,side_effect=exchange) as transport:
                self.assertEqual(chain.deliver(1,self.network(),self.provider()),"confirmed")
                self.assertEqual(chain.deliver(1,self.network(),self.provider()),"confirmed")
                self.assertEqual(transport.call_count,1)
            self.assertEqual(chain.authorization.status()["responses"],1)
            self.assertEqual(chain.store.connection.execute("SELECT status FROM attempts").fetchone()[0],"confirmed")
            chain.shutdown()
            self.assertEqual(chain.authorization.status()["state"],"revoked")

    def test_modeled_ambiguity_never_sends_again_and_stops_admission(self):
        with self.chain() as chain:
            chain.preflights={"getMe","getWebhookInfo"}
            integration.receive_fixture(chain.fence,chain.store,raw([update()]),0,NOW)
            chain.prepare(1,lambda:None)
            with patch.object(api.LocalBotAdapter,"execute",return_value=dict(classification="uncertain")) as transport:
                self.assertEqual(chain.deliver(1,self.network(),self.provider()),"uncertain")
                with self.assertRaises(stop.StopError):chain.deliver(1,self.network(),self.provider())
                self.assertEqual(transport.call_count,1)
            self.assertEqual(chain.store.connection.execute("SELECT status FROM attempts").fetchone()[0],"uncertain")
            chain.shutdown()
            self.assertEqual(chain.authorization.status()["state"],"recovery_required")

    def test_modeled_twenty_reads_include_preflights_without_cursor_progress(self):
        with self.chain() as chain:
            def execute(*args,**kwargs):
                chain.authorization.reserve("read")
                return dict(classification="confirmed",data=[])
            with patch.object(api.LocalBotAdapter,"execute",side_effect=execute):
                chain.preflight("getMe",self.network(),self.provider())
                chain.preflight("getWebhookInfo",self.network(),self.provider())
                for _ in range(18):chain.receive(self.network(),self.provider())
                with self.assertRaises(trial.TrialError):chain.receive(self.network(),self.provider())
            self.assertEqual(chain.store.status()["stored_offset"],0)
            self.assertEqual(chain.authorization.status()["reads"],20)
            self.assertEqual(chain.authorization.status()["state"],"exhausted")

    def test_actual_sqlite_process_crashes_at_each_shared_transaction_boundary(self):
        stages=("before_begin","after_receipt","before_commit","after_commit",
            "before_prepare","after_business_result","before_prepare_commit","after_prepare_commit",
            "before_intent","before_intent_commit","after_intent_commit","before_result_commit","after_result_commit")
        script=r'''
import json,os,sys
from tools.windows_watcher import private_outbox as o
root,stage=sys.argv[1:]
config=dict(mode='simulation',send_enabled=False,notifications_channel_id=None,owner_user_id=12345,arbitration_private_chat_id=12345)
g='11111111-1111-4111-8111-111111111111';now=1800000000
def crash(point):
    if point==stage:os._exit(61)
with o.DeliveryStore(root,config,54321,now,generation=g) as s:
    s.fault=crash
    s.receive_batch([dict(update_id=10,message=dict(message_id=1,date=now,text='/help',**{'from':dict(id=12345,is_bot=False)},chat=dict(id=12345,type='private')))],0,now)
    s.prepare(1,None,now,trial_only=True)
    request=s._begin_attempt(1,now)
    s._finish_attempt(request,'confirmed','success',7,now)
'''
        for stage in stages:
            with self.subTest(stage=stage),tempfile.TemporaryDirectory(prefix="phase-f3-") as temp:
                path=Path(temp)/"store"
                with outbox.DeliveryStore(path,CONFIG,54321,NOW,generation=GEN,create=True):pass
                process=subprocess.run([sys.executable,"-c",script,str(path),stage],capture_output=True,timeout=5,
                    creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
                self.assertEqual(process.returncode,61,process.stderr)
                with outbox.DeliveryStore(path,CONFIG,54321,NOW,generation=GEN) as store:
                    before_receive=stage in {"before_begin","after_receipt","before_commit"}
                    self.assertEqual(store.status()["stored_offset"],0 if before_receive else 11)
                    self.assertEqual(store.status()["receipts"],0 if before_receive else 1)
                    expected=("confirmed" if stage=="after_result_commit" else
                        "uncertain" if stage in {"after_intent_commit","before_result_commit"} else
                        "prepared" if stage in {"after_prepare_commit","before_intent","before_intent_commit"} else None)
                    row=store.inspect_outbox(1)
                    self.assertEqual(row["status"] if row else None,expected)
                    if expected in {"confirmed","uncertain"}:
                        transport=outbox.ScriptedTransport()
                        permission=outbox.SimulationPermission(54321,12345,12345,GEN,True,True)
                        self.assertEqual(store.deliver(1,transport,NOW,permission=permission),expected)
                        self.assertFalse(transport.requests)

    def test_modeled_six_responses_and_no_seventh_attempt_or_refund(self):
        with self.chain() as chain:
            chain.preflights={"getMe","getWebhookInfo"}
            integration.receive_fixture(chain.fence,chain.store,raw([update(i) for i in range(1,8)]),0,NOW)
            for mid in range(1,8):chain.prepare(mid,lambda:None)
            def execute(adapter,root,call,*args,**kwargs):
                chain.authorization.reserve("response")
                return dict(classification="confirmed",data=dict(status="confirmed",message_id=100+chain.authorization.status()["responses"]))
            with patch.object(api.LocalBotAdapter,"execute",autospec=True,side_effect=execute):
                for mid in range(1,7):
                    self.clock.value+=2
                    self.assertEqual(chain.deliver(mid,self.network(),self.provider()),"confirmed")
                self.clock.value+=2
                self.assertEqual(chain.deliver(7,self.network(),self.provider()),"uncertain")
            self.assertEqual(chain.authorization.status()["responses"],6)
            self.assertEqual(chain.authorization.status()["state"],"exhausted")
            with self.assertRaises(stop.StopError):chain.deliver(7,self.network(),self.provider())

    def test_modeled_expiration_and_300_second_deadline_preserve_receipts(self):
        with self.chain() as chain:
            chain.preflights={"getMe","getWebhookInfo"}
            integration.receive_fixture(chain.fence,chain.store,raw([update()]),0,NOW)
            self.clock.value+=121
            self.assertIsNone(chain.prepare(1,lambda:self.fail("expired snapshot")))
            self.assertEqual(chain.store.status()["receipts"],1)
            self.assertEqual(chain.store.status()["stored_offset"],11)
            self.clock.value=NOW+300
            with self.assertRaises(trial.TrialError):chain.prepare(1,lambda:None)
            self.assertEqual(chain.authorization.status()["state"],"expired")
            chain.shutdown()
            self.assertEqual(chain.authorization.status()["state"],"expired")

    def test_changed_pinned_target_is_refused_before_snapshot_or_transport(self):
        with self.chain() as chain:
            chain.preflights={"getMe","getWebhookInfo"}
            integration.receive_fixture(chain.fence,chain.store,raw([update()]),0,NOW)
            chain.adapter.target=secret.PrivateFixtureTarget(TARGET.target_id,54321,str(uuid4()))
            with self.assertRaises(recovery.FenceError):
                chain.prepare(1,lambda:self.fail("changed generation snapshot"))
            chain.adapter.target=TARGET
            with self.assertRaises(stop.StopError):chain.prepare(1,lambda:None)
            self.assertEqual(chain.authorization.status()["state"],"recovery_required")
            self.assertIsNone(chain.store.inspect_outbox(1))
            self.assertEqual(chain.store.status()["receipts"],1)

    def test_restore_barrier_cannot_be_bypassed_by_f3_preparation(self):
        def acl(paths):
            user="S-1-5-21-111111111-222222222-333333333-1001"
            return {"user":user,"rows":[dict(path=str(p),owner=user,protected=True,null_dacl=False,reparse=False,
                rules=[dict(sid=user,allow=True,rights=2032127,inherit_only=False)]) for p in paths]}
        # ACL simulées uniquement, aucune mutation Windows.
        with ExitStack() as stack:
            if os.name=="nt":
                stack.enter_context(patch.object(qualification,"_powershell"))
                stack.enter_context(patch.object(qualification.credential_acl,"inspect",side_effect=acl))
            with qualification.QualificationArea(parent=self.root) as area:
                source=area.reserve("source");source_id=str(uuid4());restored_id=str(uuid4())
                with qualification.QualifiedStore(source,CONFIG,54321,NOW,area=area,store_id=source_id,generation=GEN,create=True) as store:
                    store.receive_batch([update()],0,NOW);store.prepare(1,None,NOW,trial_only=True)
                    store.backup_fixture(area.reserve("backup"),NOW)
                destination=area.reserve("restored")
                qualification.restore_fixture(area,area.root/"backup",destination,CONFIG,54321,NOW,generation=GEN,
                    source_id=source_id,store_id=restored_id)
                with recovery.FixtureFence(self.root/"restore-fence",self.root/"restore-witness",
                        destination/"fixtures.sqlite3",BINDING,NOW,create=True) as fence:
                    fence.begin();fence.touch_store()
                    with qualification.QualifiedStore(destination,CONFIG,54321,NOW,area=area,
                            store_id=restored_id,generation=GEN) as store:
                        with self.assertRaisesRegex(base.StoreError,"RESTORE_BARRIER"):
                            integration.prepare_trial_fixture(fence,store,1,lambda:self.fail("restored snapshot"),NOW)
                        self.assertEqual(store.status()["state"],"recovery_required")
                        self.assertEqual(store.inspect_outbox(1)["status"],"cancelled")
                    fence.seal()


@unittest.skipUnless(os.name=="nt","F3 native Windows event, Job Object and independent stop command")
class WindowsIntegration(Setup,unittest.TestCase):
    def provider(self):
        return secret.PinnedSyntheticProvider(TARGET,
            secret.SyntheticVault(bytearray(b"C3-SYNTHETIC-")+bytearray(uuid4().hex,"ascii")),enabled=True)

    def local(self,chain,method,values,*,mode="complete",**kwargs):
        root=self.network()
        with lab.LoopbackLab(root,mode=mode,body=raw(values),**kwargs) as server:
            chain.adapter.endpoint=api.LocalEndpoint(server.port,tls=kwargs.get("tls",False))
            if method in {"getMe","getWebhookInfo"}:result=chain.preflight(method,root,self.provider())
            elif method=="getUpdates":result=chain.receive(root,self.provider())
            else:result=chain.deliver(1,root,self.provider())
            self.assertEqual(server.peers,["127.0.0.1"])
            return result

    def preflights(self,chain):
        for method,data in (("getMe",dict(id=54321,is_bot=True)),("getWebhookInfo",dict(url="",pending_update_count=0))):
            self.assertEqual(self.local(chain,method,data)["classification"],"confirmed")

    def test_full_chain_native_confirmation_and_no_second_attempt(self):
        with self.chain(native=True) as chain:
            with self.assertRaises(stop.StopError):stop.NativeStopGate(self.network(),chain.fence.state["session"])
            self.assertFalse(chain.stop_gate.requested)
            self.preflights(chain)
            self.assertEqual(self.local(chain,"getUpdates",[update()])["stored_offset"],11)
            response=chain.prepare(1,lambda:self.fail("help snapshot"))
            self.assertEqual(self.local(chain,"sendMessage",dict(message_id=7,chat=dict(id=12345,type="private"),
                text=unescape(response["text"])),tls=True),"confirmed")
            self.assertEqual(chain.store.inspect_outbox(1)["status"],"confirmed")
            self.assertEqual(chain.authorization.status()["reads"],3)
            self.assertEqual(chain.authorization.status()["responses"],1)
            with patch.object(api.LocalBotAdapter,"execute",side_effect=AssertionError("duplicate forbidden")):
                self.assertEqual(chain.deliver(1,self.network(),self.provider()),"confirmed")
            chain.shutdown()
            self.assertTrue(chain.authorization.job.safe)

    def test_lost_contradictory_and_late_results_are_durable_uncertain(self):
        for mode in ("headers_block","complete","late","error"):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory(prefix="phase-f3-") as temp:
                self.root=Path(temp);self.store=self.root/"store"
                with self.session(create=True):pass
                with self.fence(create=True):pass
                with self.chain(native=True) as chain:
                    self.preflights(chain)
                    self.local(chain,"getUpdates",[update()]);response=chain.prepare(1,lambda:None)
                    # Mauvais chat dans le cas complete ; autres cas retour perdu/tardif.
                    expected="rejected" if mode=="error" else "uncertain"
                    self.assertEqual(self.local(chain,"sendMessage",dict(message_id=7,
                        chat=dict(id=99999 if mode=="complete" else 12345,type="private"),text=unescape(response["text"])),
                        mode=mode,**(dict(delay=3.5) if mode=="late" else {})),expected)
                    self.assertEqual(chain.store.inspect_outbox(1)["status"],expected)
                    self.assertEqual(chain.authorization.status()["responses"],1)
                    with self.assertRaises(stop.StopError):chain.deliver(1,self.network(),self.provider())
                    chain.shutdown()

    def stop_command(self,chain,network,phase):
        until=time.monotonic()+4
        while time.monotonic()<until:
            try:
                if json.loads((network/"phase.json").read_text())["phase"]==phase:break
            except (OSError,ValueError):pass
            time.sleep(.01)
        else:raise AssertionError("worker phase not reached")
        result=subprocess.run([sys.executable,"-m","tools.windows_watcher.private_stop","--fixture-stop",
            str(chain.stop_gate.path)],capture_output=True,timeout=4,creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        proof=json.loads(result.stdout)
        self.assertTrue(proof["worker_stopped"]);self.assertEqual(proof["active"],0)
        self.assertFalse(proof["controller_closure"])
        return proof

    def test_independent_command_stops_read_write_tls_and_suspended_worker(self):
        for mode,phase in (("headers_block","read"),("write_block","write"),("tls_block","tls"),("suspended",None)):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory(prefix="phase-f3-") as temp:
                self.root=Path(temp);self.store=self.root/"store"
                with self.session(create=True):pass
                with self.fence(create=True):pass
                with self.chain(native=True) as chain:
                    chain.preflights={"getMe","getWebhookInfo"}  # Ce cas qualifie l'arrêt seul.
                    integration.receive_fixture(chain.fence,chain.store,raw([update()]),0,NOW)
                    chain.prepare(1,lambda:None)
                    chain.store._begin_attempt(1,NOW)
                    network=self.network()
                    with lab.LoopbackLab(network,mode="headers_block" if mode=="suspended" else mode,
                            tls=mode=="tls_block") as server:
                        (network/"request.json").write_text(json.dumps(dict(mode="write_block" if mode=="write_block" else "request",
                            port=server.port,delay=0,tls=mode=="tls_block",size=32*1024*1024 if mode=="write_block" else 1)))
                        provider=self.provider()
                        job=secret.CredentialFixtureJob(network,5,chain.fence,chain.authorization,provider.acquire(TARGET),
                            action="response",stop_gate=chain.stop_gate)
                        if mode=="suspended":
                            publish=chain.stop_gate.publish
                            def interrupted(worker):
                                publish(worker)
                                result=subprocess.run([sys.executable,"-m","tools.windows_watcher.private_stop","--fixture-stop",str(chain.stop_gate.path)],
                                    capture_output=True,timeout=4,creationflags=subprocess.CREATE_NO_WINDOW)
                                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                            with patch.object(chain.stop_gate,"publish",side_effect=interrupted):
                                with self.assertRaises(Exception):job.start()
                            self.assertNotIn("resumed",job.trace)
                        else:
                            with ThreadPoolExecutor(max_workers=1) as pool:
                                helper=pool.submit(self.stop_command,chain,network,phase)
                                job.start().wait();helper.result(timeout=5)
                        self.assertTrue(job.safe);self.assertEqual(job.final_counts["active"],0)
                        self.assertLess(job.trace.index("reaped_zero"),job.trace.index("lock_released"))
                        self.assertTrue(provider.vault.closed)
                        with self.assertRaises(stop.StopError):chain.stop_gate.check()
                    chain.shutdown()
                    self.assertEqual(chain.store.inspect_outbox(1)["status"],"uncertain")

    def test_unproven_stop_keeps_quarantine_and_never_reopens_admission(self):
        from tools.windows_watcher import private_job as jobs
        with self.chain(native=True) as chain:
            root=self.network()
            with lab.LoopbackLab(root,mode="headers_block") as server:
                (root/"request.json").write_text(json.dumps(dict(mode="request",port=server.port,delay=0,tls=False,size=1)))
                provider=self.provider()
                job=secret.CredentialFixtureJob(root,4,chain.fence,chain.authorization,
                    provider.acquire(TARGET),stop_gate=chain.stop_gate).start()
                self.assertTrue(server.request_received.wait(2))
                try:
                    with patch.object(job.api,"TerminateJobObject",return_value=0):
                        with self.assertRaises(jobs.UnsafeStop):chain.shutdown()
                    self.assertFalse(job.safe)
                    self.assertIsNotNone(job.lock)
                    self.assertIsNotNone(chain.authorization.db)
                    self.assertTrue(chain.fence.connections)
                    self.assertTrue(provider.vault.closed)
                    with self.assertRaises(stop.StopError):chain._admit()
                    with self.assertRaises(stop.StopError):chain.stop_gate.close()
                finally:job.stop();chain.shutdown()

    def test_wrong_process_birth_is_not_a_stop_proof(self):
        with self.chain(native=True) as chain:
            root=self.network()
            with lab.LoopbackLab(root,mode="headers_block") as server:
                (root/"request.json").write_text(json.dumps(dict(mode="request",port=server.port,delay=0,tls=False,size=1)))
                provider=self.provider()
                job=secret.CredentialFixtureJob(root,4,chain.fence,chain.authorization,
                    provider.acquire(TARGET),stop_gate=chain.stop_gate).start()
                ticket=json.loads(chain.stop_gate.path.read_text());ticket["worker"]["birth"]+=1
                chain.stop_gate.path.write_text(json.dumps(ticket))
                command=subprocess.run([sys.executable,"-m","tools.windows_watcher.private_stop","--fixture-stop",str(chain.stop_gate.path)],
                    capture_output=True,timeout=4,creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(command.returncode,1)
                self.assertFalse(json.loads(command.stdout)["safe_stop"])
                job.wait()
                self.assertTrue(job.safe)  # Preuve du contrôleur par ses handles originaux.
                with self.assertRaises(stop.StopError):chain._admit()
            chain.shutdown()

    def test_real_controller_death_after_local_acceptance_preserves_retired_and_uncertain(self):
        from tools.windows_watcher.private_fenced_job import WindowsObservation
        script=r'''
import json,sys
from pathlib import Path
from tools.windows_watcher import private_integration as i,private_recovery as r,private_outbox as o
from tools.windows_watcher import private_trial as t,private_credential as c,private_stop as s,private_bot_adapter as a
root=Path(sys.argv[1]);port=int(sys.argv[2]);now=1800000000
g='11111111-1111-4111-8111-111111111111'
config=dict(mode='simulation',send_enabled=False,notifications_channel_id=None,owner_user_id=12345,arbitration_private_chat_id=12345)
binding=dict(simulation_only=True,bot_id=54321,owner_id=12345,chat_id=12345,generation=g)
e=t.ManualFixtureEnrollment(54321,12345,12345,g,'22222222-2222-4222-8222-222222222222','33333333-3333-4333-8333-333333333333')
with r.FixtureFence(root/'fence',root/'witness',root/'store/fixtures.sqlite3',binding,now) as fence:
    fence.begin();fence.touch_store()
    with o.DeliveryStore(root/'store',config,54321,now,generation=g) as store,t.FixtureTrial(root/'trial',e,t.FixturePlan(fixture_enabled=True),create=True,wall=lambda:now,monotonic=lambda:now) as auth:
        auth.authorize_fixture(approval='reviewed-synthetic-trial',enrollment=e)
        control=root/'control';control.mkdir();gate=s.NativeStopGate(control,fence.state['session'])
        target=c.PrivateFixtureTarget(e.trial_id,54321,g)
        adapter=a.LocalBotAdapter(e,target,a.LocalEndpoint(port),enabled=True,io_timeout=3)
        chain=i.FixtureIntegration(fence,store,auth,adapter,gate,clock=lambda:now)
        chain.preflights={'getMe','getWebhookInfo'}
        store.receive_batch([dict(update_id=10,message=dict(message_id=1,date=now,text='/help',**{'from':dict(id=12345,is_bot=False)},chat=dict(id=12345,type='private')))],0,now)
        chain.prepare(1,lambda:None)
        original=c.CredentialFixtureJob._launch
        def announce(job,*args):
            original(job,*args)
            (root/'ready.json').write_text(json.dumps(dict(identity=fence.identity,state=fence.state)))
        c.CredentialFixtureJob._launch=announce
        provider=c.PinnedSyntheticProvider(target,c.SyntheticVault(bytearray(b'C3-SYNTHETIC-'+b'a'*32)),enabled=True)
        chain.deliver(1,root/'network',provider)
'''
        root=self.root/"network";root.mkdir();observation=None;controller=None
        with lab.LoopbackLab(root,mode="headers_block") as server:
            try:
                controller=subprocess.Popen([sys.executable,"-c",script,str(self.root),str(server.port)],
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertTrue(server.request_received.wait(4))
                ready=json.loads((self.root/"ready.json").read_text())
                observation=WindowsObservation(**ready)
                controller.kill();controller.communicate(timeout=4)
                with self.fence() as fence:
                    fence.recover_observed(observation)
                    self.assertEqual(fence.state["phase"],"retired")
                    with self.assertRaises(recovery.FenceError):fence.begin()
                with self.session() as store:
                    self.assertEqual(store.inspect_outbox(1)["status"],"uncertain")
                    self.assertEqual(store.status()["stored_offset"],11)
                    self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],1)
                with self.assertRaises(trial.TrialError):
                    with trial.FixtureTrial(self.root/"trial",E,trial.FixturePlan(fixture_enabled=True)):pass
                self.assertEqual(server.peers,["127.0.0.1"])
            finally:
                if controller is not None and controller.poll() is None:controller.kill();controller.communicate(timeout=4)
                if observation is not None:observation.terminate_and_prove();observation.close()


if __name__=="__main__":unittest.main()
