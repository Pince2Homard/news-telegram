"""Rapprochement GET uniquement. Aucun import du publicateur ni appel de processus."""
import hashlib
import json
from pathlib import Path
import re
import tempfile

if __package__:
    from . import coordination
else:
    import coordination

STATUSES = {"local", "confirmed", "uncertain", "unverifiable"}
MAX_CONFIRMATION_AGE = 900


def _json(path):
    raw = path.read_bytes()
    if len(raw) > 32768:
        raise ValueError("oversized")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _bundle_hashes(path):
    result = {}
    for name in ("receipt.json", "prepared.json", "result.json", "comment.md"):
        with path.with_name(name).open("rb") as source:
            raw = source.read(32769)
        if len(raw) > 32768:
            raise ValueError("oversized")
        result[name] = hashlib.sha256(raw).hexdigest()
    return result


def anchor(path, identity, url, approved_digest, repository):
    """Les ancres attendues viennent du demandeur, pas du reçu importé."""
    if (Path(path).name != "receipt.json" or not coordination.uuid_text(identity)
            or not isinstance(approved_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", approved_digest)
            or not isinstance(url, str) or not re.fullmatch(
                r"https://github\.com/" + re.escape(repository) + r"/issues/7#issuecomment-[1-9][0-9]*", url)):
        raise ValueError("invalid anchor")
    return {"receipt_path": str(Path(path).absolute()), "report_id": identity,
            "expected_url": url, "approval_digest": approved_digest}


def assess(entry, state, repository, client, now):
    """Un reçu local seul ne suffit jamais. Revalidation du corps distant exact."""
    result = {**entry, "status": "unverifiable", "reason": "invalid_receipt", "checked_at": now}
    try:
        anchor(entry["receipt_path"], entry["report_id"], entry["expected_url"], entry["approval_digest"], repository)
        path = Path(entry["receipt_path"])
        hashes = _bundle_hashes(path)
        receipt = _json(path)
        manifest = _json(path.with_name("prepared.json"))
        report = _json(path.with_name("result.json"))
        body_raw = path.with_name("comment.md").read_bytes()
        if len(body_raw) > 12000:
            raise ValueError()
        body = body_raw.decode("utf-8")
        digest = manifest.pop("approval_digest")
        if digest != entry["approval_digest"] or _digest(manifest) != digest:
            raise ValueError()
        identity = entry["report_id"]
        number = receipt["comment_id"]
        destination = f"https://github.com/{repository}/issues/7"
        if (set(receipt) != {"status", "comment_id", "url", "approval_digest", "report_id"}
                or receipt["status"] != "confirmed" or type(number) is not int or number <= 0
                or receipt["report_id"] != identity or receipt["approval_digest"] != digest
                or receipt["url"] != entry["expected_url"]
                or receipt["url"] != f"{destination}#issuecomment-{number}"
                or manifest["repository"] != repository or manifest["issue"] != 7
                or manifest["destination"] != destination or manifest["mode"] != "factice"
                or manifest["report_id"] != identity or manifest["version"] != 1
                or _digest(report) != manifest["report_digest"]
                or hashlib.sha256(body_raw).hexdigest() != manifest["body_sha256"]):
            raise ValueError()
        envelope, error = coordination.parse_envelope(body, repository)
        if (error or set(report) != {"envelope", "summary"} or envelope != report["envelope"]
                or envelope["coordination_id"] != identity or envelope["kind"] != "report"
                or envelope["reply_to"] != manifest["reply_to"]):
            raise ValueError()
        record = state.get("coordination_registry", {}).get(identity)
        if not record:
            result["reason"] = "report_not_in_registry"
            return result
        if (record["conflict"] or record.get("source_changed") or record.get("source_missing")
                or len(record["references"]) != 1 or record["references"][0] != receipt["url"]
                or record["event"]["coordination"] != envelope):
            result["reason"] = "registry_conflict_or_duplicate"
            return result
        result.update(comment_id=number, body_sha256=manifest["body_sha256"], author=manifest["author"], bundle_hashes=hashes)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return result
    try:
        # Le client du watcher n'expose que get_json/get_pages, aucun POST.
        remote = client.get_json(f"/repos/{repository}/issues/comments/{number}")
    except Exception as exc:
        if hasattr(exc, "retry_at"):
            raise
        result.update(status="uncertain", reason="network_unconfirmed")
        return result
    remote_user = remote.get("user") if isinstance(remote, dict) else None
    if (not isinstance(remote, dict) or remote.get("id") != number
            or remote.get("html_url") != entry["expected_url"]
            or remote.get("issue_url") != f"https://api.github.com/repos/{repository}/issues/7"
            or not isinstance(remote_user, dict) or remote_user.get("login") != manifest["author"]
            or remote.get("body") != body):
        result["reason"] = "remote_divergence"
        return result
    try:
        if _bundle_hashes(path) != hashes:
            raise ValueError()
    except (OSError, ValueError):
        result["reason"] = "local_receipt_changed"
        return result
    result.update(status="confirmed", reason="exact_remote_match")
    return result


def view(state, identity, now):
    entry = state.get("publication_receipts", {}).get(identity)
    if not entry:
        return {"status": "local", "reason": "no_receipt", "url": None, "checked_at": None}
    status, reason = entry["status"], entry["reason"]
    checked = entry["checked_at"]
    if status == "confirmed":
        record = state.get("coordination_registry", {}).get(identity, {})
        if (not record or record.get("conflict") or record.get("source_missing") or record.get("source_changed")
                or len(record.get("references", [])) != 1):
            status, reason = "unverifiable", "registry_divergence"
        elif not 0 <= now - checked <= MAX_CONFIRMATION_AGE:
            status, reason = "unverifiable", "confirmation_expired"
        else:
            try:
                if _bundle_hashes(Path(entry["receipt_path"])) != entry.get("bundle_hashes"):
                    raise ValueError()
            except (OSError, ValueError):
                status, reason = "unverifiable", "local_receipt_changed"
    return {"status": status, "reason": reason, "url": entry["expected_url"], "checked_at": checked}


def retain(entry, directory):
    """Conserve uniquement les quatre pièces vérifiées, pas le dossier runtime."""
    if entry["status"] != "confirmed" or not coordination.uuid_text(entry["report_id"]):
        return entry
    source = Path(entry["receipt_path"])
    parent = Path(directory)
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / entry["report_id"]
    if target.exists():
        if _bundle_hashes(target / "receipt.json") != entry["bundle_hashes"]:
            raise ValueError("stored receipt conflict")
    else:
        with tempfile.TemporaryDirectory(prefix=".receipt-", dir=parent) as temp:
            staged = Path(temp) / "bundle"
            staged.mkdir()
            for name in entry["bundle_hashes"]:
                (staged / name).write_bytes(source.with_name(name).read_bytes())
            if _bundle_hashes(staged / "receipt.json") != entry["bundle_hashes"]:
                raise ValueError("source changed during import")
            staged.rename(target)
    return {**entry, "receipt_path": str((target / "receipt.json").absolute())}
