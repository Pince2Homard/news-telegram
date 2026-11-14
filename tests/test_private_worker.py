"""C1 : contrats purs, boucle locale et qualification native Windows Job Object."""
from contextlib import ExitStack
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tools.windows_watcher import private_contracts as contracts, private_job as jobs
from tools.windows_watcher import private_local_lab as lab, private_local_worker as worker
from tools.windows_watcher import private_outbox as outbox, private_store as store, watcher


CONFIG=dict(mode="simulation",send_enabled=False,notifications_channel_id=None,
            owner_user_id=12345,arbitration_private_chat_id=12345)
GEN="11111111-1111-4111-8111-111111111111"
NOW=1800000000


def message():
    return dict(message_id=1,date=NOW,text="/status",chat=dict(id=12345,type="private"),
                **{"from":dict(id=12345,is_bot=False)})


def encoded(value):
    return json.dumps(value).encode()


def request(root,port=0,mode="request",tls=False,delay=0,size=1):
    value=dict(mode=mode,port=port,delay=delay,tls=tls,size=size)
    (root/"request.json").write_text(json.dumps(value),encoding="utf-8")


def wait_file(path,timeout=3):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        if path.is_file():
            try:return json.loads(path.read_text(encoding="utf-8"))
            except (ValueError,OSError):pass
        time.sleep(0.01)
    raise AssertionError("fixture marker not ready")


class ContractTests(unittest.TestCase):
    def test_updates_identity_minimal_rejection_and_no_cursor_for_malformed_batch(self):
        raw=encoded({"ok":True,"result":[{"update_id":1,"message":message()},
            {"update_id":3,"message":{"text":"private-fixture","from":{"id":99999}}}]})
        result=contracts.validate_updates(raw,CONFIG)
        self.assertEqual(result[0]["message"],message())
        self.assertEqual(result[1],{"update_id":3,"disposition":"rejected"})
        self.assertNotIn("private-fixture",json.dumps(result))
        for bad in ([{"update_id":True}],[{"update_id":2},{"update_id":1}],
                    [{"update_id":1},{"update_id":1}],[{}],[],"bad"):
            if bad==[]:
                self.assertEqual(contracts.validate_updates(encoded({"ok":True,"result":[]}),CONFIG),[])
            else:
                with self.assertRaises(contracts.ResponseRefused):
                    contracts.validate_updates(encoded({"ok":True,"result":bad}),CONFIG)

    def test_all_forward_bot_business_anonymous_and_missing_identity_are_refused(self):
        for field in {"sender_chat","forward_origin","forward_from","via_bot","business_connection_id",
                      "sender_business_bot","is_from_offline","via_business_bot"}:
            candidate=message();candidate[field]=None
            result=contracts.validate_updates(encoded({"ok":True,"result":[{"update_id":1,"message":candidate}]}),CONFIG)
            self.assertEqual(result,[{"update_id":1,"disposition":"rejected"}])
        for config in (None,{}, {**CONFIG,"owner_user_id":None}):
            result=contracts.validate_updates(encoded({"ok":True,"result":[{"update_id":1,"message":message()}]}),config)
            self.assertEqual(result,[{"update_id":1,"disposition":"rejected"}])

    def test_duplicate_keys_nan_truncation_size_and_malformed_envelope(self):
        for raw in (b'{"ok":true,"ok":false}',b'{"ok":NaN}',b'\xff',b'{"ok":',b'x'*65537,
                    b'[]',b'{"ok":1}',b'null'):
            with self.assertRaises(contracts.ResponseRefused):contracts.decode_envelope(raw)

    def test_message_success_rejection_and_inconsistent_confirmation(self):
        good={"ok":True,"result":{"message_id":7,"chat":{"id":12345,"type":"private"},"text":"<fixture>&"}}
        self.assertEqual(contracts.validate_message(encoded(good),200,12345,"&lt;fixture&gt;&amp;")["status"],"confirmed")
        for change in (dict(message_id=True),dict(chat=dict(id=-1001234567890,type="channel")),
                       dict(text="different"),dict(sender_chat={})):
            bad={"ok":True,"result":{**good["result"],**change}}
            with self.assertRaises(contracts.ResponseRefused):
                contracts.validate_message(encoded(bad),200,12345,"&lt;fixture&gt;&amp;")
        rejected=encoded({"ok":False,"error_code":429,"description":"private fixture omitted",
                          "parameters":{"retry_after":2}})
        result=contracts.validate_message(rejected,429,12345,"fixture")
        self.assertEqual(result,{"status":"rejected","code":429,"retry_after":2})
        self.assertNotIn("private",json.dumps(result))
        for status in (500,200,True):
            with self.assertRaises(contracts.ResponseRefused):contracts.validate_message(rejected,status,12345,"fixture")

    def test_audit_isolation_rejects_remote_dns_udp_or_wrong_loopback_port_without_socket(self):
        with patch.object(worker,"PORT",12345):
            worker.audit("socket.connect",(None,("127.0.0.1",12345)))
            for event,args in (("socket.connect",(None,("192.0.2.1",443))),
                               ("socket.connect",(None,("127.0.0.1",12346))),
                               ("socket.getaddrinfo",("fixture.invalid",443)),
                               ("socket.__new__",(None,2,2,0))):
                with self.assertRaises(RuntimeError):worker.audit(event,args)

    def test_pure_contracts_do_not_access_files_network_or_environment(self):
        raw=encoded({"ok":True,"result":[]})
        with ExitStack() as stack:
            for target in ("builtins.open","socket.socket","subprocess.Popen","os.getenv","os._Environ.__getitem__"):
                stack.enter_context(patch(target,side_effect=AssertionError("forbidden")))
            self.assertEqual(contracts.validate_updates(raw,CONFIG),[])


class LocalBankTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix="phase823-c1-")
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)

    def run_worker(self,budget=3):
        if os.name=="nt":return jobs.WindowsFixtureJob(self.root,budget).start().wait()
        # Aucun descendant dans ce chemin portable. Aucun Job Windows revendiqué.
        subprocess.run([sys.executable,"-I",str(Path(worker.__file__)),str(self.root)],
                       timeout=budget,check=True,capture_output=True)
        return {"timely":True,"safe_stop":True,"result":json.loads((self.root/"result.json").read_text())}

    def test_loopback_tls_is_verified_without_system_certificate_installation(self):
        with lab.LoopbackLab(self.root,tls=True) as server:
            request(self.root,server.port,tls=True)
            result=self.run_worker()
            self.assertTrue(result["safe_stop"])
            self.assertEqual(result["result"]["code"],"complete")
            self.assertTrue(result["result"]["tls"])
            self.assertEqual(server.peers,["127.0.0.1"])
            self.assertEqual(contracts.validate_updates(result["result"]["body"].encode(),CONFIG),[])

    def test_truncated_redirect_reset_and_malformed_are_not_successes(self):
        for mode in ("truncated","redirect","reset","malformed"):
            with self.subTest(mode=mode),lab.LoopbackLab(self.root,mode=mode) as server:
                request(self.root,server.port)
                result=self.run_worker()["result"]
                if result["code"]=="complete":
                    with self.assertRaises(contracts.ResponseRefused):
                        contracts.validate_updates(result["body"].encode(),CONFIG)
                else:self.assertEqual(result["code"],"fixture_error")
                self.assertEqual(server.peers,["127.0.0.1"])
            (self.root/"result.json").unlink(missing_ok=True)

    def test_loopback_429_rejection_is_validated_without_description(self):
        with lab.LoopbackLab(self.root,mode="error") as server:
            request(self.root,server.port)
            result=self.run_worker()["result"]
            self.assertEqual(contracts.validate_message(result["body"].encode(),result["http_status"],12345,"fixture")["status"],"rejected")

    def test_untrusted_loopback_certificate_is_rejected(self):
        with lab.LoopbackLab(self.root,tls=True) as server:
            other=self.root/"other-cert";other.mkdir()
            cert,_=lab.certificate_fixture(other)
            (self.root/"cert.pem").write_bytes(cert.read_bytes())
            request(self.root,server.port,tls=True)
            self.assertEqual(self.run_worker()["result"]["code"],"fixture_error")

    def test_worker_rejects_arbitrary_host_configuration_before_connecting(self):
        request(self.root)
        path=self.root/"request.json"
        config=json.loads(path.read_text());config["host"]="192.0.2.1"
        path.write_text(json.dumps(config))
        with patch("socket.socket",side_effect=AssertionError("no socket")):
            worker.main(self.root)
        self.assertFalse((self.root/"result.json").exists())


@unittest.skipUnless(os.name=="nt","Windows Job Object qualification")
class WindowsJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix="phase823-c1-")
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)

    def new_job(self,budget=0.6):
        job=jobs.WindowsFixtureJob(self.root,budget).start()
        self.addCleanup(job.stop)
        return job

    def test_native_abi_limits_suspended_assignment_and_reap_before_unlock(self):
        request(self.root,mode="dns_block")
        job=self.new_job()
        if ctypes.sizeof(ctypes.c_void_p)==8:
            self.assertEqual(ctypes.sizeof(jobs.ExtendedLimit),144)
            self.assertEqual(ctypes.sizeof(jobs.Startup),104)
        limits=jobs.ExtendedLimit()
        self.assertTrue(job.api.QueryInformationJobObject(job.job,9,ctypes.byref(limits),ctypes.sizeof(limits),None))
        self.assertTrue(limits.basic.flags & 0x2000)
        self.assertEqual(limits.basic.flags & (0x800|0x1000),0)
        result=job.wait()
        self.assertFalse(result["timely"])
        self.assertTrue(result["safe_stop"])
        self.assertEqual(result["active"],0)
        self.assertLess(result["elapsed"],2.7)
        self.assertEqual(result["trace"],["suspended","assigned","resumed","reaped_zero","lock_released"])

    def test_actual_tls_and_read_stalls_and_simulated_dns_share_one_budget(self):
        for name,server_mode,worker_mode,tls in (("dns",None,"dns_block",False),
                ("tls","tls_block","request",True),("read","headers_block","request",False)):
            with self.subTest(stage=name):
                directory=self.root/name;directory.mkdir()
                if server_mode is None:
                    request(directory,mode=worker_mode)
                    result=jobs.WindowsFixtureJob(directory,0.6).start().wait()
                else:
                    with lab.LoopbackLab(directory,mode=server_mode,tls=tls) as server:
                        request(directory,server.port,mode=worker_mode,tls=tls)
                        result=jobs.WindowsFixtureJob(directory,0.6).start().wait()
                        self.assertEqual(server.peers,["127.0.0.1"])
                self.assertFalse(result["timely"])
                self.assertTrue(result["safe_stop"])
                self.assertEqual(result["active"],0)

    def test_actual_blocking_write_is_interrupted(self):
        with lab.LoopbackLab(self.root,mode="write_block",tls=True) as server:
            request(self.root,server.port,mode="write_block",tls=True,size=32*1024*1024)
            result=self.new_job(0.8).wait()
            self.assertEqual(wait_file(self.root/"phase.json")["phase"],"write")
            self.assertFalse(result["timely"])
            self.assertTrue(result["safe_stop"])

    def test_accumulated_delays_do_not_reset_deadline_and_late_response_is_ignored(self):
        with lab.LoopbackLab(self.root,mode="late",delay=0.7) as server:
            request(self.root,server.port,delay=0.25)
            result=self.new_job(0.65).wait()
            self.assertTrue(server.request_received.is_set())
            self.assertFalse(result["timely"])
            self.assertIsNone(result["result"])
            self.assertTrue(server.reply_attempted.wait(1.5))
            self.assertFalse((self.root/"result.json").exists())

    def test_descendants_are_terminated_on_normal_or_deadline_exit(self):
        for mode in ("tree","tree_exit"):
            directory=self.root/mode;directory.mkdir()
            request(directory,mode=mode)
            result=jobs.WindowsFixtureJob(directory,0.8).start().wait()
            self.assertTrue((directory/"tree.json").exists())
            self.assertGreaterEqual(result["total"],3)
            self.assertEqual(result["active"],0)
            self.assertTrue(result["safe_stop"])

    def test_parent_death_closes_noninherited_job_and_kills_entire_tree(self):
        request(self.root,mode="tree")
        script=r'''
import json,os,sys,time
from pathlib import Path
from tools.windows_watcher.private_job import WindowsFixtureJob
root=Path(sys.argv[1])
job=WindowsFixtureJob(root,10).start()
(root/'controller.json').write_text(json.dumps({'launcher':job.process.pid}))
while not (root/'exit.flag').exists():time.sleep(.01)
os._exit(71)
'''
        controller=subprocess.Popen([sys.executable,"-c",script,str(self.root)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        handles=[]
        api=jobs._api()
        try:
            parent=wait_file(self.root/"controller.json")
            tree=wait_file(self.root/"tree.json")
            descendant=wait_file(self.root/"descendant.json")
            for pid in {parent["launcher"],tree["worker"],tree["child"],descendant["pid"]}:
                handle=api.OpenProcess(0x100000|0x1000,False,pid)
                self.assertTrue(handle)
                handles.append(handle)
            (self.root/"exit.flag").write_text("fixture")
            controller.communicate(timeout=3)
            self.assertEqual(controller.returncode,71)
            for handle in handles:self.assertEqual(api.WaitForSingleObject(handle,2000),0)
        finally:
            if controller.poll() is None:controller.kill();controller.communicate(timeout=3)
            for handle in handles:api.CloseHandle(handle)

    def test_assignment_failure_never_resumes_or_escapes_job(self):
        request(self.root,mode="dns_block")
        job=jobs.WindowsFixtureJob(self.root,1)
        job.api.AssignProcessToJobObject=lambda *args:0
        with self.assertRaisesRegex(jobs.WorkerError,"JOB_ASSIGN"):job.start()
        self.assertTrue(job.safe)
        self.assertNotIn("resumed",job.trace)
        self.assertFalse((self.root/"phase.json").exists())

    def test_stale_result_and_second_start_cannot_replace_active_evidence(self):
        request(self.root,mode="dns_block")
        result=self.root/"result.json"
        result.write_text('{"code":"complete"}')
        stale=jobs.WindowsFixtureJob(self.root,1)
        with self.assertRaisesRegex(jobs.WorkerError,"STALE_RESULT"):stale.start()
        self.assertTrue(stale.safe)
        self.assertFalse((self.root/"phase.json").exists())
        result.unlink()
        job=self.new_job(2)
        original_handle=job.job
        with self.assertRaisesRegex(jobs.WorkerError,"ALREADY_STARTED"):job.start()
        self.assertEqual(job.job,original_handle)
        with self.assertRaises(watcher.WatcherError):
            with watcher.single_instance_lock(self.root/"worker.lock"):pass
        self.assertTrue(job.stop()["active"]==0)

    def test_failed_termination_retains_fence_until_verified_reap(self):
        request(self.root,mode="dns_block")
        job=self.new_job(2)
        original=job.api.TerminateJobObject
        job.api.TerminateJobObject=lambda *args:0
        try:
            with self.assertRaises(jobs.UnsafeStop):job.stop()
            self.assertFalse(job.safe)
            self.assertIsNotNone(job.lock)
            with self.assertRaises(watcher.WatcherError):
                with watcher.single_instance_lock(self.root/"worker.lock"):pass
        finally:
            job.api.TerminateJobObject=original
            job.stop()
        self.assertTrue(job.safe)

    def test_no_parent_environment_secret_is_passed_to_worker(self):
        request(self.root,mode="tree_exit")
        with patch.dict(os.environ,{"C1_SENTINEL_NOT_A_SECRET":"fixture-value"}):
            result=self.new_job(2).wait()
        self.assertTrue(result["safe_stop"])
        self.assertFalse(wait_file(self.root/"tree.json")["sentinel_present"])

    def test_network_interruption_after_durable_intent_is_uncertain_without_retry(self):
        state=self.root/"store";root=self.root/"network";root.mkdir()
        request(root,mode="dns_block")
        evidence=[]
        def fail(stage):
            if stage=="after_intent_commit":
                job=jobs.WindowsFixtureJob(root,0.7).start()
                with self.assertRaises(watcher.WatcherError):
                    with watcher.single_instance_lock(state/"fixture.lock"):pass
                result=job.wait()
                evidence.append(result)
                self.assertTrue(result["safe_stop"])
                self.assertEqual(result["active"],0)
                raise OSError("fixture interruption")
        with lab.LoopbackLab(root,mode="headers_block") as server:
            request(root,server.port)
            with outbox.DeliveryStore(state,CONFIG,54321,NOW,generation=GEN,create=True,fault=fail) as session:
                session.receive_batch([{"update_id":1,"message":message()}],0,NOW)
                session.prepare(1,None,NOW)
                transport=outbox.ScriptedTransport()
                with self.assertRaises(store.StoreError):
                    session.deliver(1,transport,NOW,permission=outbox.SimulationPermission(54321,12345,12345,GEN,True))
                self.assertEqual(transport.requests,[])
                self.assertTrue(server.request_received.is_set())
        with outbox.DeliveryStore(state,CONFIG,54321,NOW,generation=GEN) as session:
            self.assertEqual(session.inspect_outbox(1)["status"],"uncertain")
        self.assertEqual(len(evidence),1)


if __name__=="__main__":unittest.main()
