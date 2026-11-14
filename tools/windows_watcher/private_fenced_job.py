"""C2 : création atomiquement dans le job et preuve OS retenue, fixtures seulement."""
import ctypes
from ctypes import wintypes as w
import json
import os
import sys
import time

from . import private_job as jobs, private_recovery as recovery


class StartupEx(ctypes.Structure):
    _fields_ = [("startup",jobs.Startup),("attributes",ctypes.c_void_p)]


def api():
    result = jobs._api()
    signatures = {
        "InitializeProcThreadAttributeList": ([ctypes.c_void_p,w.DWORD,w.DWORD,ctypes.POINTER(ctypes.c_size_t)],w.BOOL),
        "UpdateProcThreadAttribute": ([ctypes.c_void_p,w.DWORD,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_void_p],w.BOOL),
        "DeleteProcThreadAttributeList": ([ctypes.c_void_p],None),
        "IsProcessInJob": ([w.HANDLE,w.HANDLE,ctypes.POINTER(w.BOOL)],w.BOOL),
        "OpenJobObjectW": ([w.DWORD,w.BOOL,w.LPCWSTR],w.HANDLE),
        "GetProcessTimes": ([w.HANDLE,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p],w.BOOL),
    }
    for name,(args,restype) in signatures.items():
        function=getattr(result,name)
        function.argtypes,function.restype=args,restype
    return result


def birth(kernel, handle):
    values=[ctypes.c_ulonglong() for _ in range(4)]
    if not kernel.GetProcessTimes(handle,*[ctypes.byref(value) for value in values]):
        raise recovery.FenceError("PROCESS_IDENTITY_UNPROVEN")
    return values[0].value


class FencedFixtureJob(jobs.WindowsFixtureJob):
    def __init__(self, fixture, budget, fence, *, fault=None):
        if type(fence) is not recovery.FixtureFence:
            raise recovery.FenceError("FENCE_REQUIRED")
        fence.assert_active(fence.state["session"])
        if fence.state["phase"]!="running" or fence.local_job is not None:
            raise recovery.FenceError("JOB_ALREADY_BOUND")
        super().__init__(fixture,budget)
        self.api=api()
        self.fence,self.session=fence,fence.state["session"]
        self.name="Local\\phase823-c2-"+self.session
        self.fault=fault or (lambda point:None)
        fence.local_job=self

    def _new_job(self):
        handle=self.api.OpenProcess(0x100000|0x1000,False,os.getpid())
        if not handle:raise recovery.FenceError("CONTROLLER_UNPROVEN")
        try:owner={"pid":os.getpid(),"birth":birth(self.api,handle)}
        finally:self.api.CloseHandle(handle)
        self.fence.intent(self.name,owner,self.session)
        self.fault("before_job")
        ctypes.set_last_error(0)
        handle=self.api.CreateJobObjectW(None,self.name)
        if ctypes.get_last_error()==183:  # Ne jamais adopter un job préexistant.
            if handle:self.api.CloseHandle(handle)
            raise recovery.FenceError("JOB_EXISTS")
        # Publier le handle avant le point de crash injecté, pour les erreurs normales.
        self.job=handle
        self.fault("after_job")
        return handle

    def _launch(self, command, environment):
        # Ajouter la génération synthétique avant toute exécution du worker.
        path=self.root/"request.json"
        if path.is_symlink() or path.stat().st_size>4096:
            raise recovery.FenceError("REQUEST_INVALID")
        config=json.loads(path.read_text(encoding="utf-8"))
        if "generation" in config:
            raise recovery.FenceError("REQUEST_ALREADY_BOUND")
        config["generation"]=self.session
        path.write_text(json.dumps(config),encoding="utf-8")
        attributes=self._creation_attributes()
        size=ctypes.c_size_t()
        self.api.InitializeProcThreadAttributeList(None,len(attributes),0,ctypes.byref(size))
        if not 0<size.value<65536:raise jobs.WorkerError("JOB_ATTRIBUTE_SIZE")
        buffer=ctypes.create_string_buffer(size.value)
        startup=StartupEx()
        startup.startup.cb=ctypes.sizeof(startup)
        startup.attributes=ctypes.cast(buffer,ctypes.c_void_p)
        if not self.api.InitializeProcThreadAttributeList(startup.attributes,len(attributes),0,ctypes.byref(size)):
            raise jobs.WorkerError("JOB_ATTRIBUTE_INIT")
        try:
            for key,value in attributes:
                if not self.api.UpdateProcThreadAttribute(startup.attributes,0,key,
                        ctypes.byref(value),ctypes.sizeof(value),None,None):
                    raise jobs.WorkerError("JOB_ATTRIBUTE_REFUSED")
            flags=0x4|0x400|0x08000000|0x80000  # Extended startup + suspended.
            if not self.api.CreateProcessW(sys.executable,self._creation_command(command),None,None,
                    self._inherit_handles(),flags,environment,str(self.root),
                    ctypes.cast(ctypes.byref(startup),ctypes.POINTER(jobs.Startup)),ctypes.byref(self.process)):
                raise jobs.WorkerError("ATOMIC_PROCESS_CREATE")
        finally:self.api.DeleteProcThreadAttributeList(startup.attributes)
        inside=w.BOOL()
        if not self.api.IsProcessInJob(self.process.process,self.job,ctypes.byref(inside)) or not inside.value:
            raise jobs.UnsafeStop("ATOMIC_ASSIGN_UNPROVEN")
        self.trace.extend(["suspended","assigned"])
        self.fault("after_process")  # Toujours suspendu ET déjà dans le job.

    def _creation_attributes(self):
        return [(0x2000D,(w.HANDLE*1)(self.job))]

    def _creation_command(self, command):
        return command

    def _inherit_handles(self):
        return False

    def stop(self):
        if self.safe:return self.final_counts
        # Couper le réseau AVANT un fsync de preuve potentiellement bloquant.
        # stop() de base répète la terminaison puis récolte ; échec = quarantaine.
        if self.job and not self.api.TerminateJobObject(self.job,77):
            return super().stop()
        proof_error=False
        if self.fence.state["job"]==self.name:
            try:
                self.fence.stopping(self)
                self.fault("during_stop")
            except Exception:
                # Un témoin plein/cassé n'empêche JAMAIS la terminaison OS.
                proof_error=True
        result=super().stop()
        if proof_error:
            self.fence.poisoned=True
            raise recovery.FenceError("STOP_PROOF_WRITE_FAILED")
        if self.fence.state["job"]==self.name:
            try:self.fence.stopped(self)
            except Exception:
                self.fence.poisoned=True
                raise recovery.FenceError("STOP_PROOF_WRITE_FAILED") from None
        return result

    def wait(self):
        result=super().wait()
        if result["result"] is not None:
            recovery.accept_result(self.fence,self.session,result["result"])
        return result


class WindowsObservation:
    """Handles indépendants capturés AVANT la mort ; jamais une preuve JSON/PID.

    Ce détenteur de tests n'est ni service de secours ni second orchestrateur.
    Son handle de job retarde kill-on-close : il DOIT terminer le job lui-même.
    """

    def __init__(self, identity, state):
        self.api=api()
        self.identity=json.loads(json.dumps(identity))
        self.session,self.name,self.owner=state["session"],state["job"],state["owner"]
        self.job=self.controller=None
        self.proven=False
        if not self.name or not isinstance(self.owner,dict):
            raise recovery.FenceError("OBSERVATION_MISSING")
        try:
            self.controller=self.api.OpenProcess(0x100000|0x1000,False,self.owner["pid"])
            if (not self.controller or birth(self.api,self.controller)!=self.owner["birth"]
                    or self.api.WaitForSingleObject(self.controller,0)!=258):
                raise recovery.FenceError("CONTROLLER_NOT_CAPTURED_ALIVE")
            self.job=self.api.OpenJobObjectW(0x4|0x8,False,self.name)  # QUERY + TERMINATE
            if not self.job:raise recovery.FenceError("JOB_NOT_CAPTURED")
        except BaseException:
            self.close()
            raise

    def terminate_and_prove(self):
        if (not self.controller or not self.job
                or self.api.WaitForSingleObject(self.controller,0)!=0):
            raise recovery.FenceError("CONTROLLER_STILL_ALIVE_OR_UNKNOWN")
        if not self.api.TerminateJobObject(self.job,78):
            raise recovery.FenceError("OLD_JOB_STOP_FAILED")
        until=time.monotonic()+2
        while True:
            counts=jobs.Accounting()
            if not self.api.QueryInformationJobObject(self.job,1,ctypes.byref(counts),ctypes.sizeof(counts),None):
                raise recovery.FenceError("OLD_JOB_QUERY_FAILED")
            if counts.active==0:break
            if time.monotonic()>=until:raise recovery.FenceError("OLD_JOB_NOT_EMPTY")
            time.sleep(.01)
        self.proven=True

    def close(self):
        for handle in (self.job,self.controller):
            if handle:self.api.CloseHandle(handle)
        self.job=self.controller=None
