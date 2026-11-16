"""Actions recovery gate and bounded state synchronization; never imports a sender."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from newsbot.persistence import load_state, save_state  # noqa: E402

STEP = "Sauvegarder l'état de la file d'attente"


def recovery_mode(repo, run_id, attempt, get):
    if int(attempt) != 1:
        return "recovery"
    try:
        runs = get(f"/repos/{repo}/actions/workflows/poster.yml/runs?branch=main&per_page=100")
        if any(str(r["id"]) != str(run_id) and r["status"] == "in_progress"
               for r in runs["workflow_runs"]):
            return "recovery"
        completed = [r for r in runs["workflow_runs"]
                     if str(r["id"]) != str(run_id) and r["status"] == "completed"]
        if not completed:
            return "recovery"
        previous = max(completed, key=lambda r: r["updated_at"])
        if previous["conclusion"] != "success":
            return "recovery"
        jobs = get(f"/repos/{repo}/actions/runs/{previous['id']}/attempts/"
                   f"{previous['run_attempt']}/jobs")
        healthy = any(j["conclusion"] == "success" and any(
            s["name"] == STEP and s["conclusion"] == "success" for s in j["steps"]
        ) for j in jobs["jobs"])
        return "normal" if healthy else "recovery"
    except (KeyError, TypeError, ValueError, requests.RequestException):
        # Only an unavailable/unusable history permits the degraded route.
        # A fresh Git checkpoint must succeed before this run may publish.
        return "degraded"


def recovery_required(repo, run_id, attempt, get):
    return recovery_mode(repo, run_id, attempt, get) != "normal"


def probe(path, run_id, attempt):
    state = load_state(path, allow_new=False)
    state["persistence_check"] = {"run_id": str(run_id), "attempt": int(attempt)}
    save_state(state, dict.fromkeys(state["vus"]), path)


def synchronize(run, clock=time.monotonic):
    """One validated state candidate; no force or application-state merge."""
    load_state("state.json", allow_new=False)
    deadline = clock() + 60

    def git(*args):
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("Checkpoint budget exhausted")
        return run(["git", *args], timeout=min(15, remaining)).strip()

    base = git("rev-parse", "HEAD")
    base_blob = git("rev-parse", "HEAD:state.json")
    candidate = git("hash-object", "state.json")
    changed = git("diff", "--name-only", "HEAD")
    if any(p != "state.json" for p in changed.splitlines()):
        raise ValueError("Unexpected tracked changes")
    if candidate != base_blob:
        git("add", "--", "state.json")
        git("-c", "user.name=bot", "-c", "user.email=bot@users.noreply.github.com",
            "commit", "-m", "État du bot", "--", "state.json")
    for _ in range(3):
        git("fetch", "origin", "main")
        remote = git("rev-parse", "FETCH_HEAD")
        remote_blob = git("rev-parse", "FETCH_HEAD:state.json")
        if remote_blob == candidate:
            return
        if remote_blob != base_blob:
            raise ValueError("Concurrent state update; no merge attempted")
        if remote != base:
            git("-c", "user.name=bot", "-c", "user.email=bot@users.noreply.github.com",
                "rebase", remote)
        if git("rev-parse", "HEAD:state.json") != candidate:
            raise ValueError("Checkpoint candidate changed")
        if git("diff", "--name-only", remote, "HEAD") != "state.json":
            raise ValueError("Unexpected checkpoint diff")
        try:
            git("push", "origin", "HEAD:refs/heads/main")
        except (subprocess.SubprocessError, TimeoutError):
            # The next fetch verifies a possibly lost acknowledgement.
            continue
        # Always verify remotely, including a successful push.
    git("fetch", "origin", "main")
    if git("rev-parse", "FETCH_HEAD:state.json") != candidate:
        raise ValueError("Checkpoint not verified")


def main():
    mode = sys.argv[1]
    if os.getenv("DRY_RUN", "0").strip().lower() in ("1", "true", "yes"):
        print("mode=recovery" if mode == "gate" else "DRY_RUN: no persistence operation")
        return
    if mode == "gate":
        token = os.environ["GH_TOKEN"]

        def get(path):
            response = requests.get("https://api.github.com" + path,
                                    headers={"Authorization": "Bearer " + token},
                                    timeout=5, allow_redirects=False)
            response.raise_for_status()
            if response.status_code != 200 or len(response.content) > 2_000_000:
                raise ValueError("Invalid Actions response")
            return json.loads(response.content)

        mode = recovery_mode(os.environ["GITHUB_REPOSITORY"],
                                     os.environ["GITHUB_RUN_ID"],
                                     os.environ["GITHUB_RUN_ATTEMPT"], get)
        print("mode=" + mode)
    elif mode == "probe":
        probe("state.json", os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"])
    elif mode == "sync":
        def run(args, timeout):
            result = subprocess.run(args, capture_output=True, text=True,
                                    check=True, timeout=timeout)
            return result.stdout
        synchronize(run)
    else:
        raise ValueError("Unknown recovery command")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Neither raw HTTP/Git errors nor credentials are printed.
        print("Persistence recovery failed:", type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None
