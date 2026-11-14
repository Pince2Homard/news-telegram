"""C3 : DPAPI en mémoire et pipe hérité, uniquement secrets synthétiques fermés."""
import ctypes
from ctypes import wintypes as w
from pathlib import Path
import os
import re
import subprocess
import sys

from . import private_fenced_job as fenced, private_trial as trial


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


class CredentialFixtureJob(fenced.FencedFixtureJob):
    """Worker C1/C2 inchangé côté réseau ; un seul handle secret est hérité."""

    def __init__(self, fixture, budget, fence, authorization, vault, *, action="read"):
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
            "SetHandleInformation":([w.HANDLE,w.DWORD,w.DWORD],w.BOOL),
            "WriteFile":([w.HANDLE,ctypes.c_void_p,w.DWORD,ctypes.POINTER(w.DWORD),ctypes.c_void_p],w.BOOL),
        }
        for name,(args,result) in signatures.items():
            function=getattr(self.api,name);function.argtypes,function.restype=args,result
        authorization.job=self

    def start(self):
        if self.started is not None:raise CredentialError("ALREADY_STARTED")
        try:
            remaining=self.authorization.reserve(self.action)  # Durable avant tentative.
            if remaining<.1:raise trial.TrialError("TRIAL_EXPIRED")
            self.budget=min(self.budget,remaining)
            if not self.api.CreatePipe(ctypes.byref(self.read_pipe),ctypes.byref(self.write_pipe),None,4096):
                raise CredentialError("PIPE_CREATE_REFUSED")
            if not self.api.SetHandleInformation(self.read_pipe,1,1):
                raise CredentialError("PIPE_INHERIT_REFUSED")
            return super().start()
        except BaseException:
            self.stop()
            self.authorization.suspend()
            raise

    def _creation_attributes(self):
        return super()._creation_attributes()+[(0x20002,(w.HANDLE*1)(self.read_pipe.value))]

    def _inherit_handles(self):
        return True  # HANDLE_LIST contient uniquement l'extrémité de lecture.

    def _creation_command(self, command):
        return ctypes.create_unicode_buffer(subprocess.list2cmdline([sys.executable,"-I",
            str(Path(__file__).with_name("private_local_worker.py")),"--fixture-secret",
            str(self.read_pipe.value),str(self.root)]))

    def _launch(self, command, environment):
        try:
            self.deadline=min(self.deadline,self.authorization.native_deadline)
            super()._launch(command,environment)
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
