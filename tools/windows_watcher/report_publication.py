"""Publication ponctuelle approuvée : séparée du watcher GET et jamais planifiée."""
import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

if __package__:
    from . import coordination, watcher
else:
    import coordination
    import watcher

REPOSITORY = "Pince2Homard/news-telegram"
MAX_BYTES = 12000


def load_json(path):
    raw = Path(path).read_bytes()
    if len(raw) > 32768:
        raise watcher.WatcherError("JSON trop volumineux.")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Clé JSON dupliquée")
            result[key] = value
        return result

    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique)


def report_check(report, expected_id, expected_reply):
    if not isinstance(report, dict) or set(report) != {"envelope", "summary"}:
        raise watcher.WatcherError("Schéma de rapport invalide.")
    envelope, error = coordination.parse_envelope(
        "```json\n" + json.dumps(report["envelope"]) + "\n```", REPOSITORY,
    )
    if (error or envelope["kind"] != "report" or envelope["pull_request"] != 10
            or envelope["head_sha"] != "a" * 40
            or envelope["coordination_id"] != expected_id or not coordination.uuid_text(expected_id)
            or envelope["reply_to"] != expected_reply or not coordination.uuid_text(expected_reply)
            or expected_id == expected_reply or envelope["evidence_urls"] != []
            or envelope["expires_at"] is not None
            or envelope["requested_action"] not in {"review_only", "diagnose_only"}
            or envelope["status"] not in {"completed", "failed", "awaiting_validation"}):
        raise watcher.WatcherError("Identifiants ou contexte factice incohérents.")
    summary = report["summary"]
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 8000:
        raise watcher.WatcherError("Résumé invalide.")
    if any(ord(char) < 32 and char not in "\n\t" for char in summary):
        raise watcher.WatcherError("Caractère de contrôle refusé.")
    if re.search(r"gh[pousr]_|github_pat_|sk-[A-Za-z0-9]{15,}|Bearer\s+\S+|\d{6,}:[A-Za-z0-9_-]{20,}", summary):
        raise watcher.WatcherError("Motif potentiellement sensible ; publication refusée.")
    return envelope


def render(report):
    envelope = report["envelope"]
    summary = html.escape(report["summary"], quote=False).replace("@", "&#64;")
    body = ("[CODEX-REPORT] Phase 7.5 — ESSAI FACTICE DE TRANSFERT\n\n"
            "**ESSAI FACTICE — aucune évaluation du code réel de la PR #10.**\n"
            "Le SHA et la directive ci-dessous sont synthétiques. Ce message ne constitue "
            "ni une validation de CI réelle, ni une autorisation d’exécution ou de fusion.\n\n"
            "```json\n" + json.dumps(envelope, ensure_ascii=False, indent=2) + "\n```\n\n"
            "Résumé produit par Codex, conservé comme donnée :\n\n" +
            "\n".join("> " + line for line in summary.splitlines()) +
            "\n\nAucun fichier de runtime, journal complet ou donnée d’authentification n’est joint.\n\n"
            f"<!-- news-telegram-report:v1:{envelope['coordination_id']} -->\n")
    if len(body.encode("utf-8")) > MAX_BYTES:
        raise watcher.WatcherError("Commentaire trop volumineux.")
    return body


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    separators=(",", ":")).encode()).hexdigest()


def prepare(report_path, validation_path, directory, expected_id, expected_reply, author):
    report, validation = load_json(report_path), load_json(validation_path)
    report_check(report, expected_id, expected_reply)
    if not isinstance(author, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", author):
        raise watcher.WatcherError("Compte GitHub attendu invalide.")
    if not isinstance(validation, dict) or any(validation.get(key) is not True for key in ("schema_valid", "ids_and_sha_valid", "input_files_unchanged")):
        raise watcher.WatcherError("Validation de l’essai absente ou négative.")
    execution = validation.get("execution", {})
    if (not isinstance(execution, dict) or type(execution.get("launches")) is not int
            or execution["launches"] != 1 or type(execution.get("exit_code")) is not int
            or execution["exit_code"] != 0 or execution.get("error") is not None):
        raise watcher.WatcherError("Exécution factice non validée.")
    body = render(report)
    manifest = {"version": 1, "mode": "factice", "repository": REPOSITORY,
                "issue": 7, "destination": f"https://github.com/{REPOSITORY}/issues/7",
                "author": author, "report_id": expected_id, "reply_to": expected_reply,
                "publisher_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "report_digest": digest(report), "body_sha256": hashlib.sha256(body.encode()).hexdigest()}
    manifest["approval_digest"] = digest(manifest)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    watcher.save_state(directory / "result.json", report)
    (directory / "comment.md").write_bytes(body.encode("utf-8"))
    watcher.save_state(directory / "prepared.json", manifest)
    return manifest


def prepared(directory):
    directory = Path(directory)
    manifest = load_json(directory / "prepared.json")
    signature = manifest.pop("approval_digest")
    if digest(manifest) != signature:
        raise watcher.WatcherError("Préparation modifiée.")
    if (manifest["version"] != 1 or manifest["mode"] != "factice" or manifest["repository"] != REPOSITORY
            or manifest["issue"] != 7 or manifest["destination"] != f"https://github.com/{REPOSITORY}/issues/7"
            or manifest["publisher_sha256"] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()):
        raise watcher.WatcherError("Destination refusée.")
    manifest["approval_digest"] = signature
    report = load_json(directory / "result.json")
    report_check(report, manifest["report_id"], manifest["reply_to"])
    body = (directory / "comment.md").read_bytes()
    if (digest(report) != manifest["report_digest"] or body != render(report).encode()
            or hashlib.sha256(body).hexdigest() != manifest["body_sha256"]):
        raise watcher.WatcherError("Rapport ou commentaire modifié ; nouvelle validation requise.")
    return manifest, body.decode("utf-8")


class GitHubCLI:
    """Un appel gh api par requête, sans shell, token exporté ou boucle de retry."""
    def __init__(self):
        self.command = shutil.which("gh")
        if not self.command:
            raise watcher.WatcherError("GitHub CLI introuvable.")

    def call(self, method, path, payload=None):
        if method not in {"GET", "POST"} or not (path == "/user" or path.startswith(f"/repos/{REPOSITORY}/")):
            raise watcher.WatcherError("Route GitHub refusée.")
        if method == "POST" and path != f"/repos/{REPOSITORY}/issues/7/comments":
            raise watcher.WatcherError("Écriture hors destination refusée.")
        args = [self.command, "api", "--hostname", "github.com", "--method", method, path,
                "--header", "Accept: application/vnd.github+json",
                "--header", "Content-Type: application/json",
                "--header", "X-GitHub-Api-Version: 2022-11-28"]
        raw = None
        if payload is not None:
            args += ["--input", "-"]
            raw = json.dumps(payload).encode("utf-8")
        env = dict(os.environ)
        env.pop("GH_DEBUG", None)
        env["GH_PROMPT_DISABLED"] = "1"
        try:
            result = subprocess.run(args, input=raw, capture_output=True, timeout=30, env=env, check=False)
            if result.returncode:
                raise watcher.WatcherError("Requête GitHub non confirmée ; aucun réessai automatique.")
            return json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            raise watcher.WatcherError("Réponse GitHub indisponible ou invalide ; aucun réessai.") from None


def existing(client, identity):
    matches = []
    for number in (7, 10):
        for page in range(1, 101):
            rows = client.call("GET", f"/repos/{REPOSITORY}/issues/{number}/comments?per_page=100&page={page}")
            if not isinstance(rows, list):
                raise watcher.WatcherError("Liste GitHub invalide.")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("body"), str):
                    raise watcher.WatcherError("Commentaire GitHub invalide ; aucun envoi.")
                body = row.get("body", "")
                envelope, _ = coordination.parse_envelope(body, REPOSITORY)
                if (f"<!-- news-telegram-report:v1:{identity} -->" in body
                        or envelope and envelope["coordination_id"] == identity):
                    matches.append(row)
            if len(rows) < 100:
                break
        else:
            raise watcher.WatcherError("Recherche de doublons incomplète ; aucun envoi.")
    return matches


def receipt(comment, manifest, body):
    identity = comment.get("id")
    url = f"{manifest['destination']}#issuecomment-{identity}"
    if (type(identity) is not int or identity <= 0 or comment.get("html_url") != url
            or comment.get("body") != body or comment.get("user", {}).get("login") != manifest["author"]
            or comment.get("issue_url") != f"https://api.github.com/repos/{REPOSITORY}/issues/7"):
        raise watcher.WatcherError("Commentaire distant non concordant ; arbitrage requis.")
    return {"status": "confirmed", "comment_id": identity, "url": url,
            "approval_digest": manifest["approval_digest"], "report_id": manifest["report_id"]}


def deliver(directory, client, approval=None, reconcile=False):
    directory = Path(directory)
    # Un verrou partagé entre les préparations de ce même dossier outbox.
    with watcher.single_instance_lock(directory.parent / "publication.lock"):
        manifest, body = prepared(directory)
        if not reconcile and approval != manifest["approval_digest"]:
            raise watcher.WatcherError("Approbation explicite de cette préparation nécessaire.")
        if (directory / "receipt.json").exists():
            saved = load_json(directory / "receipt.json")
            if (saved.get("approval_digest") != manifest["approval_digest"]
                    or saved.get("status") != "confirmed" or saved.get("report_id") != manifest["report_id"]
                    or type(saved.get("comment_id")) is not int
                    or saved.get("url") != f"{manifest['destination']}#issuecomment-{saved['comment_id']}"):
                raise watcher.WatcherError("Reçu incohérent.")
            return saved
        journal = directory / "delivery.json"
        if journal.exists() and load_json(journal).get("approval_digest") != manifest["approval_digest"]:
            raise watcher.WatcherError("Journal d’une autre préparation ; arbitrage requis.")
        if journal.exists() and not reconcile:
            raise watcher.WatcherError("Tentative antérieure : rapprochement GET obligatoire, jamais de nouvel envoi automatique.")
        user = client.call("GET", "/user")
        target = client.call("GET", f"/repos/{REPOSITORY}/issues/7")
        if (user.get("login") != manifest["author"] or target.get("number") != 7
                or target.get("html_url") != manifest["destination"] or target.get("state") != "open"
                or target.get("locked") or "pull_request" in target):
            raise watcher.WatcherError("Compte ou destination non concordant.")
        found = existing(client, manifest["report_id"])
        if found:
            if len(found) != 1:
                raise watcher.WatcherError("Plusieurs occurrences ; arbitrage requis.")
            saved = receipt(found[0], manifest, body)
            watcher.save_state(directory / "receipt.json", saved)
            return saved
        if reconcile:
            raise watcher.WatcherError("Aucune occurrence retrouvée ; absence non probante, aucune republication autorisée.")
        # Intention durable AVANT le POST : même un crash doit interdire le renvoi.
        watcher.save_state(journal, {"status": "in_flight", "approval_digest": manifest["approval_digest"]})
        try:
            response = client.call("POST", f"/repos/{REPOSITORY}/issues/7/comments", {"body": body})
            saved = receipt(response, manifest, body)
            watcher.save_state(directory / "receipt.json", saved)
            watcher.save_state(journal, {"status": "confirmed", "approval_digest": manifest["approval_digest"]})
            return saved
        except Exception:
            # delivery.json reste in_flight si le disque échoue : pas de renvoi.
            raise watcher.WatcherError("Résultat de publication ambigu ; rapprochement GET requis, aucun réessai.") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    preview = sub.add_parser("prepare")
    for name in ("result", "validation", "out", "expected-id", "reply-to", "author"):
        preview.add_argument("--" + name, required=True)
    for name in ("publish", "reconcile"):
        command = sub.add_parser(name)
        command.add_argument("--prepared", required=True, type=Path)
        if name == "publish":
            command.add_argument("--approve-digest", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            result = prepare(args.result, args.validation, args.out, args.expected_id, args.reply_to, args.author)
            print((Path(args.out) / "comment.md").read_text(encoding="utf-8"))
        else:
            result = deliver(args.prepared, GitHubCLI(), getattr(args, "approve_digest", None), args.action == "reconcile")
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (watcher.WatcherError, OSError, ValueError, KeyError, TypeError):
        print("Opération refusée ou non confirmée ; vérifier la préparation et le journal local. Aucun réessai automatique.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
