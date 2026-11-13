"""Compare ancien/nouveau collecteur sur HTTP/1.1 local, sans source réelle.

python tools/benchmark_rss_session.py --reference ca80c30750a1e3f82d6225c37e6cc23128ea04ec
Le délai par nouvelle connexion est artificiel, pas une mesure de YouTube/TLS.
"""
import argparse
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import socket
import statistics
import subprocess
import sys
import tempfile
from threading import Thread
import time
from types import ModuleType
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from newsbot import collection, persistence

NOW = 1_800_000_000
ROOT = Path(__file__).resolve().parents[1]


class FeedHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.server.connections += 1
        time.sleep(self.server.connection_delay)

    def do_GET(self):
        self.server.trace.append((self.path, self.headers.get("Cookie"),
                                  self.headers.get("User-Agent")))
        uid = int(self.path.rsplit("/", 1)[-1])
        names = (f"new-{uid}", "shared", f"old-{uid}")
        if uid % 2:
            items = "".join(
                f"<entry><id>{name}</id><title>synthetic</title>"
                f"<link href='https://example.invalid/{name}'/>"
                "<published>2027-01-15T07:59:00Z</published></entry>" for name in names
            )
            body = f"<feed xmlns='http://www.w3.org/2005/Atom'>{items}</feed>".encode()
        else:
            items = "".join(
                f"<item><guid>{name}</guid><title>synthetic</title>"
                f"<link>https://example.invalid/{name}</link>"
                "<pubDate>Fri, 15 Jan 2027 07:59:00 GMT</pubDate></item>" for name in names
            )
            body = f"<rss version='2.0'><channel><title>synthetic</title>{items}</channel></rss>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "synthetic=feed-specific; Path=/")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def log_message(self, *args):
        pass


def compare(reference, *, feeds_count, repeats, connection_delay):
    source = subprocess.check_output(
        ["git", "show", f"{reference}:newsbot/collection.py"], cwd=ROOT, encoding="utf-8",
    )
    old = ModuleType("newsbot._rss_reference")
    old.__package__ = "newsbot"
    exec(compile(source, "<reference-collection>", "exec"), old.__dict__)
    server = ThreadingHTTPServer(("127.0.0.1", 0), FeedHandler)
    server.connection_delay = connection_delay
    server.connections = 0
    server.trace = []
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    feeds = [f"http://127.0.0.1:{server.server_port}/feed/{i}" for i in range(feeds_count)]
    results, expected = [], None
    try:
        # Le benchmark reste en boucle locale, même en présence d'un proxy.
        with patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
            for repeat in range(repeats):
                modes = [("reference", old), ("session", collection)]
                if repeat % 2:
                    modes.reverse()
                for name, api in modes:
                    current = {"init": True, "feeds": feeds.copy(), "file": [],
                               "vus": [], "feed_offset": 7 % feeds_count}
                    metrics, saved = {}, []
                    server.connections = 0
                    server.trace = []
                    output = io.StringIO()
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "state.json"

                        def checkpoint(state, seen):
                            persistence.save_state(state, seen, path)
                            saved.append(path.read_bytes())

                        started = time.perf_counter()
                        with redirect_stdout(output):
                            seen = api.collect_articles(
                                feeds, current, clock=lambda: 0, queue_clock=lambda: NOW,
                                metrics=metrics, checkpoint=checkpoint,
                            )
                        elapsed = time.perf_counter() - started
                    actual = (current, list(seen), metrics, saved, server.trace.copy())
                    if expected is None:
                        expected = actual
                    elif actual != expected:
                        raise RuntimeError("Écart de collecte/admission/checkpoints/requêtes (contenu masqué)")
                    if any(cookie is not None for _, cookie, _ in server.trace):
                        raise RuntimeError("Cookie transmis entre flux")
                    phases = re.findall(
                        r"réseau_s: ([\d.e+-]+) \| parsing_s: ([\d.e+-]+) \| admission_s: ([\d.e+-]+)",
                        output.getvalue(),
                    )
                    results.append({
                        "mode": name, "repeat": repeat + 1, "seconds": round(elapsed, 6),
                        "connections": server.connections, "requests": len(server.trace),
                        "admitted": metrics["articles_admitted"], "checkpoints": len(saved),
                        "phase_seconds": {
                            phase: round(sum(float(row[i]) for row in phases), 6)
                            for i, phase in enumerate(("network", "parsing", "admission"))
                        } if phases else None,
                    })
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    medians = {mode: statistics.median(row["seconds"] for row in results if row["mode"] == mode)
               for mode in ("reference", "session")}
    return {"reference": reference, "feeds": feeds_count, "repeats": repeats,
            "artificial_connection_delay_seconds": connection_delay,
            "equivalent_results": True, "runs": results, "median_seconds": medians,
            "local_time_reduction_percent": round(100 * (1 - medians["session"] / medians["reference"]), 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--feeds", type=int, default=53)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--connection-delay", type=float, default=0.025)
    args = parser.parse_args()
    if args.feeds <= 0 or args.repeats <= 0 or args.connection_delay < 0:
        parser.error("flux/répétitions positifs et délai non négatif requis")
    print(json.dumps(compare(args.reference, feeds_count=args.feeds, repeats=args.repeats,
                             connection_delay=args.connection_delay), indent=2))


if __name__ == "__main__":
    main()
