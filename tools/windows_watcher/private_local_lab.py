"""Banc C1 : TLS et HTTP de fixtures sur 127.0.0.1 exclusivement."""
import base64
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import threading


class LabError(RuntimeError):
    pass


_CERTIFICATE_SCRIPT = r'''
$ErrorActionPreference='Stop'
try {
  $rsa=[System.Security.Cryptography.RSACng]::new(2048)
  $request=[System.Security.Cryptography.X509Certificates.CertificateRequest]::new(
    'CN=C1 loopback fixture',$rsa,[System.Security.Cryptography.HashAlgorithmName]::SHA256,
    [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
  $san=[System.Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder]::new()
  $san.AddIpAddress([System.Net.IPAddress]::Loopback)
  $request.CertificateExtensions.Add($san.Build())
  $request.CertificateExtensions.Add([System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($true,$false,0,$true))
  $cert=$request.CreateSelfSigned([DateTimeOffset]::UtcNow.AddMinutes(-1),[DateTimeOffset]::UtcNow.AddDays(1))
  $p=$rsa.ExportParameters($true)
  $result=@{cert=[Convert]::ToBase64String($cert.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert)); ephemeral=$rsa.Key.IsEphemeral}
  foreach($name in @('Modulus','Exponent','D','P','Q','DP','DQ','InverseQ')) {
    $result[$name]=[Convert]::ToBase64String($p.$name)
  }
  $result|ConvertTo-Json -Compress
  $cert.Dispose(); $rsa.Dispose()
} catch { exit 2 }
'''


def _pem(kind, raw):
    body = base64.encodebytes(raw).decode("ascii")
    return "-----BEGIN "+kind+"-----\n"+body+"-----END "+kind+"-----\n"


def _der(tag, data):
    size = len(data)
    encoded = bytes([size]) if size < 128 else bytes([0x80+(size.bit_length()+7)//8])+size.to_bytes((size.bit_length()+7)//8, "big")
    return bytes([tag])+encoded+data


def certificate_fixture(directory):
    """Clé synthétique éphémère, aucune importation dans un magasin Windows."""
    directory = Path(directory).absolute()
    cert, key = directory/"cert.pem", directory/"key.pem"
    if cert.exists() or key.exists():
        raise LabError("FIXTURE_EXISTS")
    try:
        if os.name == "nt":
            executable = Path(os.environ["SystemRoot"])/"System32/WindowsPowerShell/v1.0/powershell.exe"
            result = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-Command", _CERTIFICATE_SCRIPT],
                capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode or len(result.stdout)>20000:
                raise ValueError()
            values = json.loads(result.stdout)
            if values.get("ephemeral") is not True:
                raise ValueError()
            integers = [b"\0"]
            for name in ("Modulus", "Exponent", "D", "P", "Q", "DP", "DQ", "InverseQ"):
                value = base64.b64decode(values[name], validate=True).lstrip(b"\0")
                integers.append((b"\0" if value[0]&128 else b"")+value)
            # Encodage PKCS#1 uniquement ; RSA et signature viennent de .NET.
            private = _der(0x30, b"".join(_der(2, value) for value in integers))
            cert.write_text(_pem("CERTIFICATE", base64.b64decode(values["cert"])), encoding="ascii")
            key.write_text(_pem("RSA PRIVATE KEY", private), encoding="ascii")
        else:
            executable = shutil.which("openssl")
            if not executable:
                raise ValueError()
            result = subprocess.run([executable, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-sha256", "-days", "1", "-subj", "/CN=C1-loopback-fixture", "-addext", "subjectAltName=IP:127.0.0.1",
                "-keyout", str(key), "-out", str(cert)], capture_output=True, timeout=15)
            if result.returncode:
                raise ValueError()
        return cert, key
    except Exception:
        raise LabError("TLS_FIXTURE_UNAVAILABLE") from None


class LoopbackLab:
    """Pas de hostname/URL configurable, ni socket sortante dans le serveur."""

    def __init__(self, directory, *, mode="complete", tls=False, delay=0, body=None):
        if mode not in {"complete", "tls_block", "write_block", "headers_block", "late", "truncated", "reset", "redirect", "malformed", "error"}:
            raise LabError("MODE_INVALID")
        if type(tls) is not bool or type(delay) not in (int,float) or not 0 <= delay <= 5:
            raise LabError("MODE_INVALID")
        self.root = Path(directory).absolute()
        self.mode, self.tls, self.delay = mode, tls, delay
        self.body = body if body is not None else b'{"ok":true,"result":[]}'
        if not isinstance(self.body, bytes) or len(self.body)>65536:
            raise LabError("BODY_INVALID")
        self.closed = threading.Event()
        self.accepted = threading.Event()
        self.request_received = threading.Event()
        self.reply_attempted = threading.Event()
        self.errors = []
        self.peers = []
        self.connection = None
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if mode=="write_block":
            self.listener.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4096)
        if os.name == "nt":
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.context = None
        if tls:
            try:
                cert,key=certificate_fixture(self.root)
                self.context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                self.context.minimum_version=ssl.TLSVersion.TLSv1_2
                self.context.load_cert_chain(cert,key)
            except BaseException:
                self.listener.close()
                raise
        self.thread=threading.Thread(target=self._serve,name="c1-loopback-fixture",daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self,*args):
        self.closed.set()
        if self.connection:
            try:self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            self.connection.close()
        self.listener.close()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise LabError("SERVER_NOT_REAPED")

    def _serve(self):
        try:
            while not self.closed.is_set():
                try:
                    connection,peer=self.listener.accept()
                    break
                except socket.timeout:continue
            else:return
            self.connection=connection
            self.peers.append(peer[0])
            if peer[0]!="127.0.0.1":raise LabError("PEER_INVALID")
            self.accepted.set()
            connection.settimeout(2)
            if self.mode=="tls_block":
                self.closed.wait(5)
                return
            if self.context:
                connection=self.context.wrap_socket(connection,server_side=True)
                self.connection=connection
            with connection:
                if self.mode=="write_block":
                    self.closed.wait(5)
                    return
                request=bytearray()
                while b"\r\n\r\n" not in request and len(request)<8192:
                    value=connection.recv(1)
                    if not value:return
                    request.extend(value)
                headers,_,body=bytes(request).partition(b"\r\n\r\n")
                length=int(next(line.split(b":",1)[1] for line in headers.split(b"\r\n") if line.lower().startswith(b"content-length:")))
                if length>32*1024*1024:raise LabError("REQUEST_SIZE")
                while len(body)<length:
                    chunk=connection.recv(min(4096,length-len(body)))
                    if not chunk:return
                    body+=chunk
                self.request_received.set()
                if self.mode=="reset":return
                if self.mode=="headers_block":
                    self.closed.wait(5)
                    return
                if self.closed.wait(self.delay):return
                data=self.body
                status=b"HTTP/1.1 200 OK"
                if self.mode=="error":
                    status=b"HTTP/1.1 429 Too Many Requests"
                    data=b'{"ok":false,"error_code":429,"parameters":{"retry_after":2}}'
                if self.mode=="malformed":data=b'{"ok":'
                if self.mode=="redirect":
                    self.reply_attempted.set()
                    connection.sendall(b"HTTP/1.1 302 Found\r\nLocation: https://invalid.example/\r\nContent-Length: 0\r\n\r\n")
                    return
                advertised=len(data)+(5 if self.mode=="truncated" else 0)
                self.reply_attempted.set()
                connection.sendall(status+b"\r\nContent-Length: "+str(advertised).encode()+b"\r\n\r\n"+data)
        except (OSError,ValueError,LabError):
            self.errors.append("fixture_connection_closed")
