"""C1 : confinement Windows de workers de fixtures, sans transport Telegram."""
import ctypes
from ctypes import wintypes as w
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

from . import watcher


class WorkerError(RuntimeError):
    pass


class UnsafeStop(WorkerError):
    pass


_QUARANTINE = []  # Garder verrou et handles si la récolte ne peut être prouvée.


class BasicLimit(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", w.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                ("active_limit", w.DWORD), ("affinity", ctypes.c_size_t),
                ("priority", w.DWORD), ("scheduling", w.DWORD)]


class IOCount(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class ExtendedLimit(ctypes.Structure):
    _fields_ = [("basic", BasicLimit), ("io", IOCount), ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


class Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_longlong) for name in ("user", "kernel", "period_user", "period_kernel")] + [
        (name, w.DWORD) for name in ("faults", "total", "active", "terminated")]


class Startup(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR),
                ("title", w.LPWSTR)] + [(name, w.DWORD) for name in
                ("x", "y", "xsize", "ysize", "xchars", "ychars", "fill", "flags")] + [
                ("show", w.WORD), ("reserved_size", w.WORD), ("reserved_bytes", ctypes.POINTER(w.BYTE)),
                ("stdin", w.HANDLE), ("stdout", w.HANDLE), ("stderr", w.HANDLE)]


class ProcessInfo(ctypes.Structure):
    _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]


def _api():
    if os.name != "nt":
        raise WorkerError("WINDOWS_REQUIRED")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
        "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
        "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
        "QueryInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
        "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
        "CreateProcessW": ([w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL, w.DWORD,
                            ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(Startup), ctypes.POINTER(ProcessInfo)], w.BOOL),
        "ResumeThread": ([w.HANDLE], w.DWORD),
        "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
        "TerminateProcess": ([w.HANDLE, w.UINT], w.BOOL),
        "CloseHandle": ([w.HANDLE], w.BOOL),
        "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    return api


class WindowsFixtureJob:
    """Processus suspendu puis affecté au job avant exécution ; fermeture sûre.

    Le job est anonyme, non hérité, sans breakaway. Seul le worker fixe de ce
    module est lancé. Les fichiers de commande contiennent des fixtures, pas de
    secrets ; aucun contrat de passage d'un token réel n'est exposé.
    """

    def __init__(self, fixture, budget):
        if type(budget) not in (float, int) or not math.isfinite(budget) or not 0.1 <= budget <= 10:
            raise WorkerError("BUDGET_INVALID")
        self.api = _api()
        self.root = Path(fixture).absolute()
        if self.root.is_symlink() or not self.root.is_dir():
            raise WorkerError("FIXTURE_INVALID")
        self.budget = float(budget)
        self.job = None
        self.process = ProcessInfo()
        self.lock = None
        self.safe = False
        self.started = None
        self.trace = []

    def start(self):
        if self.started is not None:
            raise WorkerError("ALREADY_STARTED")
        self.lock = watcher.single_instance_lock(self.root / "worker.lock")
        self.lock.__enter__()
        self.started = time.monotonic()
        self.deadline = self.started+self.budget
        try:
            if (self.root / "result.json").exists() or (self.root / "result.json").is_symlink():
                raise WorkerError("STALE_RESULT")
            self.job = self._new_job()
            if not self.job:
                raise WorkerError("JOB_CREATE")
            limits = ExtendedLimit()
            limits.basic.flags = 0x2000 | 0x8  # KILL_ON_JOB_CLOSE + ACTIVE_PROCESS
            limits.basic.active_limit = 8
            if not self.api.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise WorkerError("JOB_LIMITS")
            worker = Path(__file__).with_name("private_local_worker.py")
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(
                [sys.executable, "-I", str(worker), str(self.root)]))
            # Pas d'environnement hérité susceptible de contenir un credential.
            environment = ctypes.create_unicode_buffer("\0".join([
                "SystemRoot="+os.environ["SystemRoot"], "PYTHONDONTWRITEBYTECODE=1", "PYTHONUTF8=1", ""]) + "\0")
            self._launch(command, environment)
            if time.monotonic() >= self.deadline:
                raise WorkerError("START_EXPIRED")
            if self.api.ResumeThread(self.process.thread) == 0xFFFFFFFF:
                raise WorkerError("PROCESS_RESUME")
            self.trace.append("resumed")
            return self
        except BaseException:
            self.stop()
            raise

    def _new_job(self):
        return self.api.CreateJobObjectW(None, None)

    def _launch(self, command, environment):
        startup = Startup()
        startup.cb = ctypes.sizeof(startup)
        flags = 0x4 | 0x400 | 0x08000000  # SUSPENDED, UNICODE_ENVIRONMENT, NO_WINDOW
        if not self.api.CreateProcessW(sys.executable, command, None, None, False, flags,
                environment, str(self.root), ctypes.byref(startup), ctypes.byref(self.process)):
            raise WorkerError("PROCESS_CREATE")
        self.trace.append("suspended")
        if not self.api.AssignProcessToJobObject(self.job, self.process.process):
            # Aucun fallback non supervisé ; le processus est encore suspendu.
            if not self.api.TerminateProcess(self.process.process, 77):
                raise UnsafeStop("UNASSIGNED_PROCESS")
            if self.api.WaitForSingleObject(self.process.process, 2000) != 0:
                raise UnsafeStop("UNASSIGNED_PROCESS")
            raise WorkerError("JOB_ASSIGN")
        self.trace.append("assigned")

    def accounting(self):
        result = Accounting()
        if not self.job or not self.api.QueryInformationJobObject(self.job, 1, ctypes.byref(result), ctypes.sizeof(result), None):
            raise UnsafeStop("JOB_QUERY")
        return {"active": result.active, "total": result.total}

    def wait(self):
        try:
            return self._wait()
        except BaseException:
            self.stop()
            raise

    def _wait(self):
        if self.started is None:
            raise WorkerError("NOT_STARTED")
        remaining = max(0, self.deadline-time.monotonic())
        status = self.api.WaitForSingleObject(self.process.process, max(1, int(remaining*1000)))
        timely = status == 0 and time.monotonic() <= self.deadline
        if status not in (0, 258):
            raise UnsafeStop("PROCESS_WAIT")
        counts = self.stop()
        timely = timely and time.monotonic() <= self.deadline
        raw = None
        result = self.root / "result.json"
        if timely and result.is_file() and not result.is_symlink() and result.stat().st_size <= 100000:
            raw = json.loads(result.read_text(encoding="utf-8"))
        return {"fixtures_only": True, "timely": timely, "safe_stop": self.safe,
                "active": counts["active"], "total": counts["total"],
                "elapsed": time.monotonic()-self.started, "result": raw, "trace": list(self.trace)}

    def stop(self):
        if self.safe:
            return self.final_counts
        try:
            if self.job:
                if not self.api.TerminateJobObject(self.job, 77):
                    raise UnsafeStop("JOB_TERMINATE")
                until = time.monotonic()+2
                while True:
                    counts = self.accounting()
                    if counts["active"] == 0:
                        break
                    if time.monotonic() >= until:
                        raise UnsafeStop("JOB_NOT_EMPTY")
                    time.sleep(0.01)
            else:
                counts = {"active": 0, "total": 0}
            if self.process.process and self.api.WaitForSingleObject(self.process.process, 1000) != 0:
                raise UnsafeStop("PROCESS_NOT_REAPED")
            self.trace.append("reaped_zero")
        except BaseException:
            if self not in _QUARANTINE:
                _QUARANTINE.append(self)
            # Ne pas libérer le verrou ni perdre les handles si l'arrêt est douteux.
            raise
        for handle in (self.process.thread, self.process.process, self.job):
            if handle:
                self.api.CloseHandle(handle)
        self.process, self.job = ProcessInfo(), None
        if self.lock:
            self.lock.__exit__(None, None, None)
            self.lock = None
            self.trace.append("lock_released")
        self.final_counts = counts
        self.safe = True
        if self in _QUARANTINE:
            _QUARANTINE.remove(self)
        return counts
