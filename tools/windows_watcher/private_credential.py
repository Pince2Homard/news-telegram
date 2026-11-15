"""C3/F2 : DPAPI en mémoire et pipe dupliqué, secrets synthétiques fermés."""
import ctypes
from ctypes import wintypes as w
from pathlib import Path
from dataclasses import dataclass
import json
import os
import re
import subprocess
import sys

from . import coordination, private_fenced_job as fenced, private_trial as trial, private_store as base
from . import private_stop


class CredentialError(RuntimeError):
    pass


def wipe(value):
    if value:ctypes.memset((ctypes.c_ubyte*len(value)).from_buffer(value),0,len(value))


def synthetic(value):
    return type(value) is bytearray and re.fullmatch(rb"C3-SYNTHETIC-[0-9a-f]{32}",value) is not None


class Blob(ctypes.Structure):
    _fields_=[("size",w.DWORD),("data",ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(value, protect):
    if os.name!="nt":raise CredentialError("WINDOWS_REQUIRED")
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    crypto=ctypes.WinDLL("crypt32",use_last_error=True)
    kernel.LocalFree.argtypes,kernel.LocalFree.restype=[ctypes.c_void_p],ctypes.c_void_p
    function=crypto.CryptProtectData if protect else crypto.CryptUnprotectData
    function.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,
                      ctypes.c_void_p,w.DWORD,ctypes.POINTER(Blob)]
    function.restype=w.BOOL
    buffer=(ctypes.c_ubyte*len(value)).from_buffer(value)
    source,target=Blob(len(value),buffer),Blob()
    try:
        # UI_FORBIDDEN seulement ; ni LOCAL_MACHINE, description, audit ou prompt.
        if not function(ctypes.byref(source),None,None,None,None,4,ctypes.byref(target)):
            raise CredentialError("SYNTHETIC_DPAPI_REFUSED")
        if not 0<target.size<=4096:raise CredentialError("SYNTHETIC_SIZE_INVALID")
        result=bytearray(target.size)
        ctypes.memmove((ctypes.c_ubyte*len(result)).from_buffer(result),target.data,target.size)
        return result
    finally:
        if target.data:
            ctypes.memset(target.data,0,target.size)
            kernel.LocalFree(target.data)


class SyntheticVault:
    """Aucun lecteur de credential, chemin, cible Credential Manager ou découverte.

    Le format fermé ne peut pas accueillir un token Telegram. Rien n'est écrit
    sur disque. DPAPI n'est ni une autorisation d'essai ni un anti-rollback.
    """

    def __init__(self, plaintext):
        self.closed=True
        self.blob=bytearray()
        if not synthetic(plaintext):raise CredentialError("SYNTHETIC_ONLY")
        try:
            self.blob=_dpapi(plaintext,True)
            self.closed=False
        finally:wipe(plaintext)

    def use(self, consumer):
        if self.closed:raise CredentialError("SYNTHETIC_REVOKED")
        plaintext=_dpapi(self.blob,False)
        try:
            if not synthetic(plaintext):raise CredentialError("SYNTHETIC_ONLY")
            return consumer(plaintext)
        finally:wipe(plaintext)

    def close(self):
        wipe(self.blob)
        self.closed=True

    def __repr__(self):
        return "<SyntheticVault redacted>"


@dataclass(frozen=True)
class PrivateFixtureTarget:
    target_id: str
    bot_id: int
    generation: str

    def __post_init__(self):
        if (not coordination.uuid_text(self.target_id) or not base._positive(self.bot_id)
                or not coordination.uuid_text(self.generation)):
            raise CredentialError("PRIVATE_TARGET_INVALID")


class PinnedSyntheticProvider:
    """Stockage DPAPI en mémoire explicite ; aucun CredRead, fichier ou fallback."""

    def __init__(self,target,vault=None,*,enabled=False):
        if type(target) is not PrivateFixtureTarget or type(enabled) is not bool:
            raise CredentialError("PRIVATE_TARGET_INVALID")
        if vault is not None and type(vault) is not SyntheticVault:
            raise CredentialError("SYNTHETIC_ONLY")
        self.target,self.vault,self.enabled=target,vault,enabled

    def acquire(self,expected):
        if not self.enabled:raise CredentialError("PROVIDER_DISABLED")
        if type(expected) is not PrivateFixtureTarget or expected!=self.target:
            raise CredentialError("PRIVATE_TARGET_MISMATCH")
        if self.vault is None or self.vault.closed:raise CredentialError("PRIVATE_CREDENTIAL_ABSENT")
        return self.vault

    def close(self):
        if self.vault is not None:self.vault.close()
        self.enabled=False


class CredentialFixtureJob(fenced.FencedFixtureJob):
    """F2 : aucun handle héritable ; duplication vers le worker suspendu seul."""

    def __init__(self, fixture, budget, fence, authorization, vault, *, action="read", stop_gate=None):
        if stop_gate is not None and type(stop_gate) is not private_stop.NativeStopGate:
            raise CredentialError("STOP_GATE_REQUIRED")
        self.stop_gate=stop_gate
        if type(authorization) is not trial.FixtureTrial or type(vault) is not SyntheticVault or vault.closed:
            raise CredentialError("SYNTHETIC_AUTHORIZATION_REQUIRED")
        if action not in {"read","response"}:raise CredentialError("ACTION_INVALID")
        if authorization.job is not None and not authorization.job.safe:
            raise CredentialError("WORKER_ALREADY_ACTIVE")
        binding=fence.identity["binding"]
        enrollment=authorization.enrollment
        if (enrollment.bot_id,enrollment.owner_id,enrollment.chat_id,enrollment.deployment_generation)!=(
                binding["bot_id"],binding["owner_id"],binding["chat_id"],binding["generation"]):
            raise CredentialError("ENROLLMENT_MISMATCH")
        remaining=authorization.remaining()
        if remaining<.1:raise trial.TrialError("TRIAL_EXPIRED")
        super().__init__(fixture,min(budget,remaining),fence)
        self.authorization,self.vault,self.action=authorization,vault,action
        self.read_pipe,self.write_pipe=w.HANDLE(),w.HANDLE()
        signatures={
            "CreatePipe":([ctypes.POINTER(w.HANDLE),ctypes.POINTER(w.HANDLE),ctypes.c_void_p,w.DWORD],w.BOOL),
            "GetCurrentProcess":([],w.HANDLE),
            "DuplicateHandle":([w.HANDLE,w.HANDLE,w.HANDLE,ctypes.POINTER(w.HANDLE),w.DWORD,w.BOOL,w.DWORD],w.BOOL),
            "WriteFile":([w.HANDLE,ctypes.c_void_p,w.DWORD,ctypes.POINTER(w.DWORD),ctypes.c_void_p],w.BOOL),
        }
        for name,(args,result) in signatures.items():
            function=getattr(self.api,name);function.argtypes,function.restype=args,result
        authorization.job=self

    def start(self):
        if self.started is not None:raise CredentialError("ALREADY_STARTED")
        try:
            if self.stop_gate is not None:self.stop_gate.check()
            remaining=self.authorization.reserve(self.action)  # Durable avant tentative.
            if remaining<.1:raise trial.TrialError("TRIAL_EXPIRED")
            self.budget=min(self.budget,remaining)
            if not self.api.CreatePipe(ctypes.byref(self.read_pipe),ctypes.byref(self.write_pipe),None,4096):
                raise CredentialError("PIPE_CREATE_REFUSED")
            return super().start()
        except BaseException:
            self.stop()
            self.authorization.suspend()
            raise

    def _inherit_handles(self):
        return False

    def _process_executable(self):
        # Le launcher venv ferait un second processus sans transmettre le handle
        # non héritable. Ce worker n'utilise que la stdlib de ce même Python.
        return sys._base_executable

    def _creation_command(self, command):
        return ctypes.create_unicode_buffer(subprocess.list2cmdline([self._process_executable(),"-I",
            str(Path(__file__).with_name("private_local_worker.py")),"--fixture-secret-duplicated",str(self.root)]))

    def _launch(self, command, environment):
        try:
            self.deadline=min(self.deadline,self.authorization.native_deadline)
            super()._launch(command,environment)
            if self.stop_gate is not None:self.stop_gate.publish(self)
            remote=w.HANDLE()
            if not self.api.DuplicateHandle(self.api.GetCurrentProcess(),self.read_pipe,self.process.process,
                    ctypes.byref(remote),0,False,2):
                raise CredentialError("PIPE_DUPLICATE_REFUSED")
            self.api.CloseHandle(self.read_pipe)
            self.read_pipe=w.HANDLE()
            path=self.root/"request.json"
            config=fenced.read_request(path)
            config["secret_handle"]=remote.value  # Numéro uniquement, aucun secret.
            path.write_text(json.dumps(config,ensure_ascii=False),encoding="utf-8")
            def transfer(plaintext):
                frame=bytearray(len(plaintext).to_bytes(4,"little"))+plaintext
                try:
                    written=w.DWORD()
                    buffer=(ctypes.c_ubyte*len(frame)).from_buffer(frame)
                    # Une seule petite écriture dans le pipe vide de 4096 octets,
                    # pendant que le processus est encore suspendu dans son job.
                    if not self.api.WriteFile(self.write_pipe,buffer,len(frame),ctypes.byref(written),None) or written.value!=len(frame):
                        raise CredentialError("PIPE_TRANSFER_REFUSED")
                finally:wipe(frame)
            self.vault.use(transfer)
        finally:
            self._close_pipes()
            self.vault.close()

    def _resume(self):
        if self.stop_gate is not None:self.stop_gate.check()
        return super()._resume()

    def _close_pipes(self):
        for handle in (self.read_pipe,self.write_pipe):
            if handle.value:self.api.CloseHandle(handle)
        self.read_pipe,self.write_pipe=w.HANDLE(),w.HANDLE()

    def stop(self):
        try:return super().stop()
        finally:
            self._close_pipes()
            self.vault.close()

    def wait(self):
        try:result=super().wait()
        except BaseException:
            self.authorization.suspend()
            raise
        try:self.authorization.remaining()
        except trial.TrialError:
            result["timely"],result["result"]=False,None
        if not result["timely"] or not isinstance(result["result"],dict) or result["result"].get("code")!="complete":
            self.authorization.suspend()
        return result
