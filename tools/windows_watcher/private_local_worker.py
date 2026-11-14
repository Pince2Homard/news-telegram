"""Worker C1 fixe : seul 127.0.0.1 est adressable, aucune route Telegram."""
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
import os
import ctypes
from ctypes import wintypes as w
import re
from uuid import UUID


def audit(event, args):
    if event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
        raise RuntimeError("DNS_FORBIDDEN")
    if event == "socket.connect" and (not isinstance(args[1], tuple)
            or len(args[1]) != 2 or args[1] != ("127.0.0.1", PORT)):
        raise RuntimeError("DESTINATION_FORBIDDEN")
    if event == "socket.bind":
        raise RuntimeError("BIND_FORBIDDEN")
    if event == "socket.__new__" and (args[1] != socket.AF_INET or args[2] != socket.SOCK_STREAM):
        raise RuntimeError("SOCKET_FORBIDDEN")


PORT = 0
GENERATION = None
CREDENTIAL_CONSUMED = False


def consume_fixture_pipe(raw_handle):
    """Lecture bornée, format factice fermé, aucun fichier ni credential système."""
    global CREDENTIAL_CONSUMED
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.ReadFile.argtypes=[w.HANDLE,ctypes.c_void_p,w.DWORD,ctypes.POINTER(w.DWORD),ctypes.c_void_p]
    kernel.ReadFile.restype=w.BOOL
    kernel.CloseHandle.argtypes,kernel.CloseHandle.restype=[w.HANDLE],w.BOOL
    kernel.SetHandleInformation.argtypes,kernel.SetHandleInformation.restype=[w.HANDLE,w.DWORD,w.DWORD],w.BOOL
    handle=w.HANDLE(int(raw_handle))
    plaintext=bytearray()
    def read_exact(size):
        value=bytearray(size)
        offset=0
        try:
            while offset<size:
                count=w.DWORD()
                buffer=(ctypes.c_ubyte*(size-offset)).from_buffer(value,offset)
                if not kernel.ReadFile(handle,buffer,size-offset,ctypes.byref(count),None) or not count.value:
                    raise RuntimeError("SYNTHETIC_PIPE_REFUSED")
                offset+=count.value
            return value
        except BaseException:
            ctypes.memset((ctypes.c_ubyte*size).from_buffer(value),0,size)
            raise
    try:
        if not kernel.SetHandleInformation(handle,1,0):raise RuntimeError("SYNTHETIC_PIPE_REFUSED")
        length=int.from_bytes(read_exact(4),"little")
        if not 0<length<=128:raise RuntimeError("SYNTHETIC_SIZE_REFUSED")
        plaintext=read_exact(length)
        if re.fullmatch(rb"C3-SYNTHETIC-[0-9a-f]{32}",plaintext) is None:
            raise RuntimeError("SYNTHETIC_ONLY")
        CREDENTIAL_CONSUMED=True
    finally:
        if plaintext:ctypes.memset((ctypes.c_ubyte*len(plaintext)).from_buffer(plaintext),0,len(plaintext))
        kernel.CloseHandle(handle)


def main(root):
    global PORT, GENERATION
    config_path = root / "request.json"
    if config_path.is_symlink() or not config_path.is_file() or config_path.stat().st_size > 4096:
        return
    config = json.loads(config_path.read_text(encoding="utf-8"))
    fields = {"mode", "port", "delay", "tls", "size"}
    if set(config) not in (fields, fields | {"generation"}):
        return
    if "generation" in config:
        if not isinstance(config["generation"], str) or str(UUID(config["generation"])) != config["generation"]:
            return
        GENERATION = config["generation"]
    mode = config["mode"]
    if (mode not in {"request", "dns_block", "write_block", "tree", "tree_exit"}
            or type(config["port"]) is not int or not 0 <= config["port"] <= 65535
            or type(config["delay"]) not in (int, float) or not 0 <= config["delay"] <= 30
            or type(config["tls"]) is not bool or type(config["size"]) is not int
            or not 1 <= config["size"] <= 32*1024*1024):
        return
    PORT = config["port"]
    sys.addaudithook(audit)
    def phase(name):
        (root / "phase.json").write_text(json.dumps({"phase": name}), encoding="utf-8")
    if mode in {"tree", "tree_exit"}:
        child = subprocess.Popen([sys.executable, "-I", str(Path(__file__)), "--descendant", str(root)],
            close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        (root / "tree.json").write_text(json.dumps({"worker": os.getpid(), "child": child.pid,
            "sentinel_present": "C1_SENTINEL_NOT_A_SECRET" in os.environ}), encoding="utf-8")
        if mode == "tree":
            time.sleep(30)
        return
    phase("dns_simulated")
    if mode == "dns_block":
        time.sleep(30)
    time.sleep(config["delay"])
    phase("connect")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw:
        if mode=="write_block":
            raw.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,0)
        raw.settimeout(15)
        raw.connect(("127.0.0.1", PORT))
        phase("tls" if config["tls"] else "connected")
        if config["tls"]:
            ca = root / "cert.pem"
            if ca.is_symlink():
                return
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.verify_mode = ssl.CERT_REQUIRED
            context.check_hostname = True
            context.load_verify_locations(cafile=str(ca))
            stream = context.wrap_socket(raw, server_hostname="127.0.0.1")
        else:
            stream = raw
        with stream:
            phase("write")
            payload = b"x" * config["size"]
            stream.sendall(b"POST /fixture HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\nContent-Length: "
                           +str(len(payload)).encode()+b"\r\n\r\n"+payload)
            phase("read")
            response = bytearray()
            while b"\r\n\r\n" not in response and len(response) <= 8192:
                chunk = stream.recv(1)
                if not chunk:
                    raise RuntimeError("TRUNCATED")
                response.extend(chunk)
            header, separator, body = bytes(response).partition(b"\r\n\r\n")
            if not separator or len(header) > 8192:
                raise RuntimeError("HEADERS_INVALID")
            lines = header.split(b"\r\n")
            if lines[0] not in (b"HTTP/1.1 200 OK", b"HTTP/1.1 429 Too Many Requests"):
                raise RuntimeError("HTTP_REFUSED")
            if any(line.lower().startswith(b"transfer-encoding:") for line in lines[1:]):
                raise RuntimeError("FRAMING_INVALID")
            lengths = [line[16:].strip() for line in lines[1:] if line.lower().startswith(b"content-length: ")]
            if len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= 65536:
                raise RuntimeError("LENGTH_INVALID")
            length = int(lengths[0])
            while len(body) < length:
                chunk = stream.recv(min(4096, length-len(body)))
                if not chunk:
                    raise RuntimeError("TRUNCATED")
                body += chunk
            result = {"code": "complete", "http_status": 200 if lines[0].startswith(b"HTTP/1.1 200") else 429,
                      "body": body.decode("utf-8"), "peer": "127.0.0.1", "tls": config["tls"]}
            if GENERATION is not None:
                result["generation"] = GENERATION
            if CREDENTIAL_CONSUMED:
                result["credential"] = "synthetic-consumed"
            (root / "result.json").write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv)==3 and sys.argv[1]=="--descendant":
        (Path(sys.argv[2])/"descendant.json").write_text(json.dumps({"pid":os.getpid()}),encoding="utf-8")
        time.sleep(30)
    elif len(sys.argv)==2 or (len(sys.argv)==4 and sys.argv[1]=="--fixture-secret"):
        directory = Path(sys.argv[-1]).absolute()
        try:
            if len(sys.argv)==4:consume_fixture_pipe(sys.argv[2])
            main(directory)
        except Exception:
            # Aucun texte d'exception, corps, chemin ou environnement dans les logs.
            error = {"code": "fixture_error"}
            if GENERATION is not None:
                error["generation"] = GENERATION
            (directory / "result.json").write_text(json.dumps(error), encoding="utf-8")
