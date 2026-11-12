"""Compare les règles au code Git de référence, hors réseau et hors production.

Exécution explicite : python tools/compare_reference.py [--reference COMMIT].
L'ancien code n'est pas recopié dans le dépôt ; seuls des états copiés ou factices
sont sauvegardés dans un répertoire temporaire. Aucun contenu d'article n'est logué.
"""
import argparse
import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from newsbot import collection, persistence, publication

REFERENCE = "1dd233d764b0f1cf5997944fd3d8f4d97fb1be6d"
NOW = 1_800_000_000
FEED = "https://example.invalid/feed"


class Clock:
    def __init__(self, now=NOW):
        self.now = float(now)

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def entry(uid, age=60, link=None):
    value = {"id": uid, "title": "synthetic", "link": link or f"https://example.invalid/{uid}"}
    if age is not None:
        value["published_parsed"] = time.gmtime(NOW - age)
    return value


def item(uid):
    return {"text": "synthetic", "link": f"https://example.invalid/{uid}"}


def record(uid, reason=None, age=0):
    value = {"uid": uid, "item": item(uid), "first_seen_at": NOW - age}
    if reason is None:
        value["admitted_at"] = NOW - age
    else:
        value["reason"] = reason
        value["published_at"] = NOW + 600 if reason == "future_date" else None
    return value


def state(**extra):
    return {"vus": [], "file": [], "erreurs": [], "feeds": [FEED], "yt": {}, "init": True, **extra}


def collect(api, initial, entries, *, now=NOW, budget=180, interrupt=False):
    """Compare aussi les sauvegardes et une éventuelle reprise de checkpoint."""
    current, saved, metrics = copy.deepcopy(initial), [], {}
    clock = Clock(now)
    feeds = list(entries)

    def get(url, **kwargs):
        clock.sleep(0.25)
        return SimpleNamespace(content=url.encode(), raise_for_status=lambda: None)

    transport = SimpleNamespace(get=get)
    parser = lambda raw: SimpleNamespace(entries=copy.deepcopy(entries[raw.decode()]))
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state.json"

        def checkpoint(current, seen):
            api.save_state(current, seen, path)
            saved.append(json.loads(path.read_text(encoding="utf-8")))
            if interrupt and len(saved) == 1:
                raise InterruptedError("synthetic checkpoint interruption")

        options = dict(http=transport, parser=parser, clock=clock, queue_clock=lambda: now,
                       checkpoint=checkpoint, checkpoint_every=1, metrics=metrics,
                       max_age_seconds=86400, budget=budget)
        try:
            seen = api.collect_articles(feeds, current, **options)
        except InterruptedError:
            current = api.load_state(path)
            seen = api.collect_articles(feeds, current, **options)
        return current, list(seen), metrics, saved


def publish(api, initial, outcomes, *, budget=720, dry_run=False):
    current, saved, requests_seen, metrics = copy.deepcopy(initial), [], [], {}
    clock = Clock()
    remaining = iter(outcomes)

    def post(url, *, data, timeout):
        requests_seen.append((data["text"], timeout))
        clock.sleep(0.25)
        status = next(remaining)
        if status == "ambiguous":
            raise requests.ConnectionError("synthetic ambiguous result")
        payload = {"result": {"message_id": len(requests_seen), "date": int(clock())}}
        if status == 429:
            payload = {"parameters": {"retry_after": 2}}
        return SimpleNamespace(status_code=status, ok=status == 200, json=lambda: payload)

    def sender(article, token, chat):
        return api.send_message(article, token, chat, http=SimpleNamespace(post=post),
                                clock=clock, monotonic=clock, sleep=clock.sleep)

    actual_save = api.save_state
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state.json"

        def save(current, seen, destination):
            actual_save(current, seen, destination)
            saved.append(json.loads(path.read_text(encoding="utf-8")))

        # Patch the actual storage boundary in each implementation.
        save_owner = api if isinstance(api, ModuleType) else persistence
        with patch.object(save_owner, "save_state", save):
            sent = api.process_queue(current, dict.fromkeys(current.get("vus", [])),
                                     "synthetic-token", "synthetic-chat", state_path=path,
                                     dry_run=dry_run, sender=sender, clock=clock,
                                     monotonic=clock, sleep=clock.sleep, max_per_run=3,
                                     interval_seconds=30, time_limit=budget, metrics=metrics)
        return sent, current, metrics, saved, requests_seen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", default=REFERENCE)
    args = parser.parse_args()

    def git_file(name):
        return subprocess.check_output(["git", "show", f"{args.reference}:{name}"],
                                       encoding="utf-8", cwd=Path(__file__).resolve().parents[1])

    reference = ModuleType("news_telegram_reference")
    sys.modules[reference.__name__] = reference  # Required by dataclasses.
    exec(compile(git_file("poster.py"), "<git-reference-poster>", "exec"), reference.__dict__)
    new = SimpleNamespace(collect_articles=collection.collect_articles,
                          load_state=persistence.load_state, save_state=persistence.save_state,
                          process_queue=publication.process_queue, send_message=publication.send_message)
    full = state(file=[item(f"queued-{i}") for i in range(1000)],
                 admission_buffer=[record(f"buffered-{i}", age=172800) for i in range(1000)])
    quarantine_full = state(date_quarantine=[record(f"held-{i}", "unknown_date") for i in range(500)])
    quarantined = state(date_quarantine=[record("unknown", "unknown_date"), record("future", "future_date")])
    persistent = json.loads(git_file("state.json"))
    latest_epoch = max((row.get("run_started_epoch", 0) for row in persistent.get("metrics_history", [])), default=NOW)

    def compare_state_copy(api):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(persistent), encoding="utf-8")
            current = api.load_state(path)
            api.save_state(current, dict.fromkeys(current["vus"]), path)
            return api.load_state(path)

    cases = [
        ("production-state-round-trip", compare_state_copy),
        ("production-state-collection-copy", lambda api: collect(api, persistent, {}, now=latest_epoch)),
        ("silent-bootstrap", lambda api: collect(api, state(init=False, feeds=[]), {FEED: [entry("new"), entry("old")]})),
        ("new-feed-bootstrap", lambda api: collect(api, state(feeds=[]), {FEED: [entry("new"), entry("old")]})),
        ("id-url-guid-deduplication", lambda api: collect(api, state(vus=["seen", "https://example.invalid/guid"]),
            {FEED: [entry("seen"), entry("alias", link="https://example.invalid/shared#frag"),
                    entry("original", link="https://example.invalid/shared"),
                    entry("distinct", link="https://example.invalid/guid"),
                    entry("query-a", link="https://example.invalid/story?id=1"),
                    entry("query-b", link="https://example.invalid/story?id=2")]})),
        ("date-classification", lambda api: collect(api, state(), {FEED: [entry("unknown", None), entry("future", -600), entry("stale", 86401), entry("fresh")]})),
        ("corrected-quarantine-dates", lambda api: collect(api, quarantined, {FEED: [entry("unknown"), entry("future")]})),
        ("quarantine-expiration", lambda api: collect(api, state(date_quarantine=[record("expired", "unknown_date", 86400)]), {FEED: [entry("replacement", None)]})),
        ("full-admission-structures", lambda api: collect(api, full, {FEED: [entry("overflow")]})),
        ("full-date-quarantine", lambda api: collect(api, quarantine_full, {FEED: [entry("overflow", None)]})),
        ("admitted-articles-age-without-expiration", lambda api: collect(api, state(file=[item("first")], admission_buffer=[record("old-admission", age=172800)]), {})),
        ("collection-budget", lambda api: collect(api, state(), {FEED: [entry("first")], FEED + "2": [entry("second")]}, budget=0.1)),
        ("checkpoint-restart", lambda api: collect(api, state(), {FEED: [entry("first")], FEED + "2": [entry("second")]}, interrupt=True)),
    ]
    queue = state(file=[item("first"), item("second")], admission_buffer=[record("third", age=172800)])
    cases += [(name, lambda api, statuses=statuses, budget=budget: publish(api, queue, statuses, budget=budget))
              for name, statuses, budget in [
                  ("publication-promotes-fifo", [200, 200, 200], 720),
                  ("publication-insufficient-budget", [], 209),
                  ("publication-ambiguous-network", ["ambiguous"], 720),
                  ("publication-rate-limited", [429, 200, 200, 200], 720),
                  ("publication-permanent-article", [400, 200, 200], 720),
                  ("publication-global-error", [403], 720),
              ]]
    for name, scenario in cases:
        with contextlib.redirect_stdout(io.StringIO()):
            before, after = scenario(reference), scenario(new)
        if before != after:
            raise SystemExit(f"Écart de comportement : {name} (contenu masqué).")
    print(f"Comparaison {args.reference[:12]} : {len(cases)} scénarios identiques ; réseau et état de production intacts.")


if __name__ == "__main__":
    main()
