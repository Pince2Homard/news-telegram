"""F2 : contrats portables, boucle locale et duplication Windows sans héritage."""
from concurrent.futures import ThreadPoolExecutor
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from tools.windows_watcher import private_bot_adapter as api, private_credential as secret
from tools.windows_watcher import private_local_lab as lab, private_local_worker as worker
from tools.windows_watcher import private_outbox as outbox, private_recovery as recovery, private_trial as trial

GEN="11111111-1111-4111-8111-111111111111"
HOST="22222222-2222-4222-8222-222222222222"
TID="33333333-3333-4333-8333-333333333333"
E=trial.ManualFixtureEnrollment(54321,12345,12345,GEN,HOST,TID)
TARGET=secret.PrivateFixtureTarget(TID,54321,GEN)
CONFIG=dict(mode="simulation",send_enabled=False,notifications_channel_id=None,
            owner_user_id=12345,arbitration_private_chat_id=12345)
BINDING=dict(simulation_only=True,bot_id=54321,owner_id=12345,chat_id=12345,generation=GEN)
NOW=1800000000


class FixtureSetup:
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="phase-f2-")
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def network(self):
        root=self.root/uuid4().hex;root.mkdir();return root


class ContractTests(FixtureSetup,unittest.TestCase):
    def test_disabled_no_network_and_no_remote_or_ambiguous_endpoint(self):
        adapter=api.LocalBotAdapter(E,TARGET)
        with self.assertRaises(api.TransportRefused):adapter.write_fixture(self.root,adapter.prepare("getMe",{}))
        for host in ("localhost","::1","127.0.0.2","192.0.2.1","127.0.0.1:80","https://example.invalid"):
            with self.assertRaises(api.TransportRefused):api.LocalEndpoint(443,host)
        for port in (True,0,-1,65536,"443"):
            with self.assertRaises(api.TransportRefused):api.LocalEndpoint(port)

    def test_provider_missing_disabled_wrong_target_bot_or_generation(self):
        provider=secret.PinnedSyntheticProvider(TARGET)
        with self.assertRaises(secret.CredentialError):provider.acquire(TARGET)
        provider.enabled=True
        for target in (TARGET,secret.PrivateFixtureTarget(str(uuid4()),54321,GEN),
                secret.PrivateFixtureTarget(TID,99999,GEN),secret.PrivateFixtureTarget(TID,54321,str(uuid4()))):
            with self.assertRaises(secret.CredentialError):provider.acquire(target)
        with self.assertRaises(api.TransportRefused):api.LocalBotAdapter(E,secret.PrivateFixtureTarget(TID,99999,GEN))

    def test_four_methods_closed_payloads_and_bounded_requests(self):
        adapter=api.LocalBotAdapter(E,TARGET)
        for method,data in (("getMe",{}),("getWebhookInfo",{}),
                ("getUpdates",dict(offset=0,limit=20,timeout=5)),
                ("sendMessage",dict(chat_id=12345,text="fixture",parse_mode="HTML"))):
            self.assertEqual(adapter.prepare(method,data).method,method)
        for method,data in (("deleteWebhook",{}),("getMe",{"url":"fixture"}),
                ("getUpdates",dict(offset=True,limit=20,timeout=5)),
                ("sendMessage",dict(chat_id=99999,text="fixture",parse_mode="HTML")),
                ("sendMessage",dict(chat_id=12345,text="x"*2049,parse_mode="HTML"))):
            with self.assertRaises(api.TransportRefused):adapter.prepare(method,data)
        for value in ('{"port":1,"port":2}', '{"port":NaN}', '[]'):
            path=self.root/"ambiguous.json";path.write_text(value)
            with self.assertRaises((ValueError,RuntimeError)):worker.read_request(path)

    def test_strict_response_contracts_and_expurgated_errors(self):
        adapter=api.LocalBotAdapter(E,TARGET)
        def result(body,status=200):return dict(safe_stop=True,timely=True,result=dict(code="complete",http_status=status,body=body))
        call=adapter.prepare("getMe",{})
        for body in ('{"ok":true,"result":{"id":true,"is_bot":true}}',
                     '{"ok":true,"result":{"id":99999,"is_bot":true}}','{"ok":true}',
                     '{"ok":true,"ok":false}', '{"ok":NaN}', '{"ok":'):
            value=adapter.validate(call,result(body))
            self.assertEqual(value,{"classification":"uncertain","code":"RESULT_UNKNOWN"})
        webhook=adapter.prepare("getWebhookInfo",{})
        self.assertEqual(adapter.validate(webhook,result(json.dumps(dict(ok=True,result=dict(url="sensitive-fixture",pending_update_count=0)))))["classification"],"uncertain")
        for status in (400,401,403,409,429):
            response=result(json.dumps(dict(ok=False,error_code=status,description="sensitive-fixture")),status)
            value=adapter.validate(call,response)
            self.assertEqual(value["classification"],"rejected")
            self.assertNotIn("sensitive-fixture",str(value))
        self.assertEqual(adapter.validate(call,result('{"ok":false,"error_code":500}',500))["classification"],"uncertain")

    def test_audit_refuses_dns_remote_reused_destination_and_udp_without_connect(self):
        with patch.object(worker,"PORT",12345):
            for event,args in (("socket.getaddrinfo",("localhost",12345)),
                    ("socket.connect",(None,("192.0.2.1",443))),
                    ("socket.connect",(None,("127.0.0.1",12346))),
                    ("socket.__new__",(None,2,2,0))):
                with self.assertRaises(RuntimeError):worker.audit(event,args)


class LocalTransportTests(FixtureSetup,unittest.TestCase):
    def test_simulated_connect_write_read_timeouts_are_not_retry_permissions(self):
        # Simulation sans socket réelle ; les stalls natifs TLS/write/read restent
        # aussi couverts par C1/C2. Aucun paquet vers une adresse extérieure.
        script=r'''
import json,sys
from pathlib import Path
from unittest.mock import patch
from tools.windows_watcher import private_local_worker as worker
root=Path(sys.argv[1]);stage=sys.argv[2];calls=[]
class Socket:
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def settimeout(self,value):calls.append('timeout')
    def connect(self,address):
        assert address[0]=='127.0.0.1'
        calls.append('connect')
        if stage=='connect':raise TimeoutError()
    def sendall(self,value):
        calls.append('write')
        if stage=='write':raise TimeoutError()
    def recv(self,size):
        calls.append('read');raise TimeoutError()
with patch.object(worker.socket,'socket',return_value=Socket()):
    try:worker.main(root)
    except TimeoutError:pass
print(json.dumps(dict(calls=calls,phase=json.loads((root/'phase.json').read_text())['phase'])))
'''
        for stage in ("connect","write","read"):
            root=self.network()
            adapter=api.LocalBotAdapter(E,TARGET,api.LocalEndpoint(12345),enabled=True)
            call=adapter.prepare("getMe",{});adapter.write_fixture(root,call)
            result=subprocess.run([sys.executable,"-c",script,str(root),stage],capture_output=True,timeout=4,
                creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),check=True)
            value=json.loads(result.stdout)
            self.assertEqual(value["phase"],stage)
            self.assertEqual(value["calls"].count(stage),1)
            self.assertEqual(adapter.validate(call,dict(safe_stop=True,timely=True,result=dict(code="fixture_error")))["classification"],"uncertain")

    def run_local(self,root,server,method="getMe",payload=None):
        adapter=api.LocalBotAdapter(E,TARGET,api.LocalEndpoint(server.port,tls=server.tls),enabled=True)
        call=adapter.prepare(method,{} if payload is None else payload)
        adapter.write_fixture(root,call)
        if os.name=="nt":
            # Même processus protégé C1, sans secret dans ces tests de framing.
            from tools.windows_watcher.private_job import WindowsFixtureJob
            result=WindowsFixtureJob(root,2).start().wait()
        else:
            subprocess.run([sys.executable,"-I",str(Path(worker.__file__)),str(root)],timeout=3,check=True,capture_output=True)
            result=dict(safe_stop=True,timely=True,result=json.loads((root/"result.json").read_text()))
        return adapter.validate(call,result)

    def test_valid_four_responses_over_loopback(self):
        cases=(("getMe",{},dict(id=54321,is_bot=True)),
               ("getWebhookInfo",{},dict(url="",pending_update_count=0)),
               ("getUpdates",dict(offset=0,limit=20,timeout=0),[]),
               ("sendMessage",dict(chat_id=12345,text="fixture",parse_mode="HTML"),
                dict(message_id=7,chat=dict(id=12345,type="private"),text="fixture")))
        for method,payload,data in cases:
            root=self.network()
            with self.subTest(method=method),lab.LoopbackLab(root,body=json.dumps(dict(ok=True,result=data)).encode()) as server:
                self.assertEqual(self.run_local(root,server,method,payload)["classification"],"confirmed")
                self.assertEqual(server.peers,["127.0.0.1"])

    def test_all_http_errors_and_no_retry(self):
        for status in (400,401,403,409,429,500):
            root=self.network()
            with self.subTest(status=status),lab.LoopbackLab(root,http_status=status,
                    body=json.dumps(dict(ok=False,error_code=status,description="sensitive-fixture")).encode()) as server:
                result=self.run_local(root,server)
                self.assertEqual(result["classification"],"uncertain" if status==500 else "rejected")
                self.assertNotIn("sensitive-fixture",str(result))
                self.assertEqual(server.peers,["127.0.0.1"])

    def test_truncation_redirect_oversize_reset_malformed_and_read_timeout(self):
        for mode,extra in (("truncated",{}),("redirect",{}),("reset",{}),("malformed",{}),
                           ("complete",dict(advertised_size=65537)),("headers_block",{}),("late",dict(delay=1))):
            root=self.network()
            with self.subTest(mode=mode,extra=extra),lab.LoopbackLab(root,mode=mode,**extra) as server:
                self.assertEqual(self.run_local(root,server)["classification"],"uncertain")
                self.assertEqual(server.peers,["127.0.0.1"])

    def test_untrusted_local_tls_and_handshake_timeout(self):
        for mode in ("complete","tls_block"):
            root=self.network()
            with self.subTest(mode=mode),lab.LoopbackLab(root,tls=True,mode=mode,
                    body=b'{"ok":true,"result":{"id":54321,"is_bot":true}}') as server:
                if mode=="complete":
                    other=self.network();cert,_=lab.certificate_fixture(other)
                    (root/"cert.pem").write_bytes(cert.read_bytes())
                self.assertEqual(self.run_local(root,server)["classification"],"uncertain")


@unittest.skipUnless(os.name=="nt","F2 native Windows DuplicateHandle / Job Object")
class WindowsTransferTests(FixtureSetup,unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store=self.root/"store"
        with outbox.DeliveryStore(self.store,CONFIG,54321,NOW,generation=GEN,create=True):pass
        with self.fence(create=True):pass

    def fence(self,**kwargs):
        return recovery.FixtureFence(self.root/"fence",self.root/"witness",self.store/"fixtures.sqlite3",BINDING,NOW,**kwargs)

    def ledger(self):
        return trial.FixtureTrial(self.root/"trial",E,trial.FixturePlan(fixture_enabled=True),create=True)

    def provider(self):
        raw=bytearray(b"C3-SYNTHETIC-")+bytearray(uuid4().hex,"ascii")
        return secret.PinnedSyntheticProvider(TARGET,secret.SyntheticVault(raw),enabled=True)

    def test_complete_provider_adapter_pipeline_and_no_legacy_credential(self):
        root=self.network()
        with self.ledger() as budget,self.fence() as fence:
            budget.authorize_fixture(approval="reviewed-synthetic-trial",enrollment=E);fence.begin()
            with lab.LoopbackLab(root,tls=True,body=b'{"ok":true,"result":{"id":54321,"is_bot":true}}') as server:
                adapter=api.LocalBotAdapter(E,TARGET,api.LocalEndpoint(server.port,tls=True),enabled=True)
                provider=self.provider()
                with patch("tools.windows_watcher.telegram_once.read_external_token",side_effect=AssertionError("legacy forbidden")):
                    result=adapter.execute(root,adapter.prepare("getMe",{}),fence,budget,provider)
                self.assertEqual(result,dict(classification="confirmed",data=dict(bot_verified=True)))
                self.assertTrue(provider.vault.closed)
                self.assertEqual(budget.status()["reads"],1)
                disabled=self.provider();disabled.enabled=False
                with self.assertRaises(api.TransportRefused):
                    adapter.execute(self.network(),adapter.prepare("getMe",{}),fence,budget,disabled)
                self.assertTrue(disabled.vault.closed)
                self.assertEqual(budget.status()["reads"],1)
            fence.seal()

    def test_global_deadline_ignores_late_response_and_cannot_retry(self):
        root=self.network()
        with self.ledger() as budget,self.fence() as fence:
            budget.authorize_fixture(approval="reviewed-synthetic-trial",enrollment=E);fence.begin()
            with lab.LoopbackLab(root,mode="late",delay=1,
                    body=b'{"ok":true,"result":{"id":54321,"is_bot":true}}') as server:
                adapter=api.LocalBotAdapter(E,TARGET,api.LocalEndpoint(server.port),enabled=True,io_timeout=3)
                call=adapter.prepare("getMe",{})
                result=adapter.execute(root,call,fence,budget,self.provider(),budget=.4)
                self.assertEqual(result["classification"],"uncertain")
                self.assertTrue(budget.job.safe)
                self.assertEqual(budget.job.final_counts["active"],0)
                self.assertEqual(budget.status()["reads"],1)
                self.assertTrue(server.reply_attempted.wait(2))
                self.assertFalse((root/"result.json").exists())
                with self.assertRaises(api.TransportRefused):
                    adapter.execute(self.network(),call,fence,budget,self.provider())
                self.assertEqual(budget.status()["reads"],1)
                self.assertEqual(server.peers,["127.0.0.1"])
            fence.seal()

    def test_concurrent_broad_inheritance_cannot_receive_noninheritable_pipe(self):
        root=self.network()
        script=r'''
import ctypes,json,sys
from ctypes import wintypes as w
k=ctypes.WinDLL('kernel32',use_last_error=True)
k.PeekNamedPipe.argtypes=[w.HANDLE,ctypes.c_void_p,w.DWORD,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p]
k.PeekNamedPipe.restype=w.BOOL
print(json.dumps({'readable':bool(k.PeekNamedPipe(int(sys.argv[1]),None,0,None,None,None))}))
'''
        with self.ledger() as budget,self.fence() as fence:
            budget.authorize_fixture(approval="reviewed-synthetic-trial",enrollment=E);fence.begin()
            with lab.LoopbackLab(root) as server:
                adapter=api.LocalBotAdapter(E,TARGET,api.LocalEndpoint(server.port),enabled=True)
                adapter.write_fixture(root,adapter.prepare("getUpdates",dict(offset=0,limit=20,timeout=0)))
                provider=self.provider();job=secret.CredentialFixtureJob(root,5,fence,budget,provider.acquire(TARGET))
                original=job.api.DuplicateHandle
                flags=ctypes.wintypes.DWORD()
                job.api.GetHandleInformation.argtypes=[ctypes.wintypes.HANDLE,ctypes.POINTER(ctypes.wintypes.DWORD)]
                def duplicate(*args):
                    for handle in (job.read_pipe,job.write_pipe):
                        self.assertTrue(job.api.GetHandleInformation(handle,ctypes.byref(flags)))
                        self.assertEqual(flags.value&1,0)
                    def launch(_):
                        p=subprocess.run([sys._base_executable,"-c",script,str(job.read_pipe.value)],
                            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                            close_fds=False,creationflags=subprocess.CREATE_NO_WINDOW,timeout=4)
                        self.assertEqual(p.returncode,0)
                        return json.loads(p.stdout)["readable"]
                    with ThreadPoolExecutor(max_workers=4) as pool:self.assertEqual(list(pool.map(launch,range(8))),[False]*8)
                    self.assertFalse(args[5])
                    return original(*args)
                with patch.object(job.api,"DuplicateHandle",side_effect=duplicate):result=job.start().wait()
                self.assertTrue(result["safe_stop"])
                self.assertEqual(result["result"]["credential"],"synthetic-consumed")
                self.assertEqual((job.read_pipe.value,job.write_pipe.value),(None,None))
            provider.close();fence.seal()

    def test_duplicate_failure_and_interrupted_partial_transfer_are_closed(self):
        for name in ("create","duplicate","interrupt","partial"):
            # Indépendance complète des gardes, quotas et processus par scénario.
            with self.subTest(name=name),tempfile.TemporaryDirectory(prefix="phase-f2-") as temp:
                root=Path(temp);network=root/"network";network.mkdir()
                (network/"request.json").write_text(json.dumps(dict(mode="dns_block",port=0,delay=0,tls=False,size=1)))
                store=root/"store"
                with outbox.DeliveryStore(store,CONFIG,54321,NOW,generation=GEN,create=True):pass
                with (recovery.FixtureFence(root/"fence",root/"witness",store/"fixtures.sqlite3",BINDING,NOW,create=True) as fence,
                        trial.FixtureTrial(root/"trial",E,trial.FixturePlan(fixture_enabled=True),create=True) as budget):
                    budget.authorize_fixture(approval="reviewed-synthetic-trial",enrollment=E);fence.begin()
                    provider=self.provider();vault=provider.acquire(TARGET)
                    job=secret.CredentialFixtureJob(network,2,fence,budget,vault)
                    if name=="create":patcher=patch.object(job.api,"CreateProcessW",return_value=0)
                    elif name=="duplicate":patcher=patch.object(job.api,"DuplicateHandle",return_value=0)
                    elif name=="interrupt":patcher=patch.object(vault,"use",side_effect=KeyboardInterrupt())
                    else:
                        original=job.api.WriteFile
                        def partial(handle,buffer,size,written,overlap):
                            original(handle,buffer,size//2,written,overlap)
                            return 0
                        patcher=patch.object(job.api,"WriteFile",side_effect=partial)
                    with patcher:
                        with self.assertRaises((RuntimeError,KeyboardInterrupt)):job.start()
                    self.assertTrue(job.safe)
                    self.assertTrue(vault.closed)
                    self.assertFalse(any(vault.blob))
                    self.assertNotIn("resumed",job.trace)
                    self.assertEqual(job.final_counts["active"],0)
                    self.assertEqual((job.read_pipe.value,job.write_pipe.value),(None,None))
                    provider.close();fence.seal()


if __name__=="__main__":unittest.main()
