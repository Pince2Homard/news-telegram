"""F3 : arrêt explicite de fixtures Windows, sans lecteur ou credential."""
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import sys
import time

from . import coordination, private_boundary, private_fenced_job as native, private_job as jobs, private_store as base
from .private_local_worker import read_request


class StopError(RuntimeError):
    pass


def _api():
    kernel=native.api()
    signatures={
        "CreateEventW":([ctypes.c_void_p,w.BOOL,w.BOOL,w.LPCWSTR],w.HANDLE),
        "OpenEventW":([w.DWORD,w.BOOL,w.LPCWSTR],w.HANDLE),
        "SetEvent":([w.HANDLE],w.BOOL),
    }
    for name,(args,result) in signatures.items():
        function=getattr(kernel,name);function.argtypes,function.restype=args,result
    return kernel


def _process(value):
    return (type(value) is dict and set(value)=={"pid","birth"} and base._positive(value["pid"])
            and type(value["birth"]) is int and 0<value["birth"]<2**64)


def _ticket(path):
    path=base._fixture_directory(path)
    if path.name!="stop-ticket.json" or path.stat().st_size>4096:
        raise StopError("STOP_TICKET_INVALID")
    value=read_request(path)
    if (set(value)!={"fixtures_only","session","event","job","controller","worker"}
            or value["fixtures_only"] is not True or not coordination.uuid_text(value["session"])
            or value["event"]!="Local\\phase-f3-stop-"+value["session"]
            or value["job"]!="Local\\phase823-c2-"+value["session"]
            or not _process(value["controller"])
            or (value["worker"] is not None and not _process(value["worker"]))):
        raise StopError("STOP_TICKET_INVALID")
    return path,value


def stop_fixture(path, *, boundary=None):
    """Secours autonome : fermer admission, tuer, prouver. Aucune écriture SQLite.

    Le succès prouve l'arrêt du worker capturé ; le contrôleur doit encore
    récolter ses handles et persister son désarmement. Jamais une reprise C2.
    """
    if os.name!="nt":raise StopError("WINDOWS_REQUIRED")
    private_boundary.check(boundary,control=Path(path).absolute().parent)
    _,ticket=_ticket(path)
    kernel=_api();event=job=root=None
    try:
        event=kernel.OpenEventW(0x100000|2,False,ticket["event"])
        if not event or not kernel.SetEvent(event):raise StopError("ADMISSION_STOP_UNPROVEN")
        job=kernel.OpenJobObjectW(4|8,False,ticket["job"])
        if not job:raise StopError("WORKER_STOP_UNPROVEN")
        worker=ticket["worker"]
        if worker is not None:
            root=kernel.OpenProcess(0x100000|0x1000,False,worker["pid"])
        inside=w.BOOL()
        try:
            proven=(root and native.birth(kernel,root)==worker["birth"]
                    and kernel.IsProcessInJob(root,job,ctypes.byref(inside)) and inside.value)
        except Exception:proven=False
        # Même si la preuve racine manque, couper le job avant de signaler l'échec.
        if not kernel.TerminateJobObject(job,79):raise StopError("WORKER_STOP_UNPROVEN")
        until=time.monotonic()+2
        counts=jobs.Accounting()
        while True:
            if not kernel.QueryInformationJobObject(job,1,ctypes.byref(counts),ctypes.sizeof(counts),None):
                raise StopError("WORKER_STOP_UNPROVEN")
            if counts.active==0:break
            if time.monotonic()>=until:raise StopError("WORKER_STOP_UNPROVEN")
            time.sleep(.01)
        if not proven or kernel.WaitForSingleObject(root,1000)!=0:
            raise StopError("ROOT_STOP_UNPROVEN")
        return {"admission_closed":True,"worker_stopped":True,"active":0,"controller_closure":False}
    finally:
        for handle in (root,job,event):
            if handle:kernel.CloseHandle(handle)


class NativeStopGate:
    """Événement latché, jamais réarmé. Pas de thread ni de second moteur."""

    def __init__(self,directory,session,*,boundary=None):
        if os.name!="nt" or not coordination.uuid_text(session):raise StopError("FIXTURE_REQUIRED")
        private_boundary.check(boundary,control=directory)
        root=base._fixture_directory(directory)
        self.boundary=boundary
        self.path=root/"stop-ticket.json"
        if not root.is_dir() or self.path.exists():raise StopError("STOP_TICKET_EXISTS")
        self.api=_api();self.handle=None;self.job=None
        event="Local\\phase-f3-stop-"+session
        ctypes.set_last_error(0)
        self.handle=self.api.CreateEventW(None,True,False,event)
        if not self.handle or ctypes.get_last_error()==183:
            if self.handle:self.api.CloseHandle(self.handle)
            self.handle=None
            raise StopError("STOP_EVENT_EXISTS")
        try:
            process=self.api.OpenProcess(0x100000|0x1000,False,os.getpid())
            if not process:raise StopError("CONTROLLER_UNPROVEN")
            try:owner={"pid":os.getpid(),"birth":native.birth(self.api,process)}
            finally:self.api.CloseHandle(process)
            self.data=dict(fixtures_only=True,session=session,event=event,
                job="Local\\phase823-c2-"+session,controller=owner,worker=None)
            with self.path.open("x",encoding="utf-8") as output:json.dump(self.data,output)
        except BaseException:
            self.close();raise

    @property
    def requested(self):
        if not self.handle:return True
        status=self.api.WaitForSingleObject(self.handle,0)
        if status not in (0,258):raise StopError("ADMISSION_UNPROVEN")
        return status==0

    def check(self):
        boundary=getattr(self,"boundary",None)
        if boundary is not None:private_boundary.check(boundary,control=self.path.parent)
        if self.requested:raise StopError("ADMISSION_CLOSED")

    def publish(self,job):
        self.check()
        if job.session!=self.data["session"] or job.name!=self.data["job"]:
            raise StopError("JOB_MISMATCH")
        self.job=job
        value={**self.data,"worker":{"pid":job.process.pid,"birth":native.birth(self.api,job.process.process)}}
        temporary=self.path.with_name("stop-ticket.next")
        with temporary.open("x",encoding="utf-8") as output:json.dump(value,output)
        os.replace(temporary,self.path)
        self.data=value
        self.check()

    def close(self):
        if self.job is not None and not self.job.safe:raise StopError("WORKER_STOP_UNPROVEN")
        if self.handle:
            self.api.SetEvent(self.handle)
            self.api.CloseHandle(self.handle);self.handle=None


def main():
    if len(sys.argv)!=3 or sys.argv[1]!="--fixture-stop":return 2
    try:
        print(json.dumps(stop_fixture(Path(sys.argv[2]))));return 0
    except Exception:
        print('{"code":"STOP_UNPROVEN","safe_stop":false}');return 1


if __name__=="__main__":raise SystemExit(main())
