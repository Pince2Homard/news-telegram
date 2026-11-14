"""Routeur privé simulé, pur : aucune lecture, écriture ou fonction de transport."""
from datetime import datetime, timezone
from html import escape
import json
import math
import re

from . import coordination, supervision


MAX_RESPONSE_LENGTH = 2048
MAX_COMMAND_LENGTH = 256
MAX_SNAPSHOT_AGE_SECONDS = 3600
MAX_INBOX_ENTRIES = 1500  # Limites existantes : 1000 ouverts + 500 historiques.
MAX_DISPLAY_ITEMS = 3
REPOSITORY = "Pince2Homard/news-telegram"
PROVENANCE_FIELDS = frozenset({
    "sender_chat", "sender_business_bot", "business_connection_id", "via_bot",
    "forward_origin", "forward_from", "forward_from_chat", "forward_date",
    "forward_sender_name", "forward_signature", "forward_from_message_id",
    "is_automatic_forward", "author_signature",
})
HELP = (
    "/start — Présenter le QG mobile Codex.\n"
    "/help — Afficher les commandes disponibles.\n"
    "/status — Consulter un instantané du watcher.\n"
    "/pr [numéro] — Consulter les PR connues.\n"
    "/ci [numéro ou SHA] — Consulter les résultats CI connus.\n"
    "/rapports [UUID] — Consulter les rapports Codex structurés.\n"
    "Consultation hors ligne, sans vérification en direct."
)
PROTOCOL_ERRORS = {
    "invalid_size": "taille de métadonnées invalide",
    "ambiguous_json": "métadonnées ambiguës",
    "invalid_envelope": "enveloppe de coordination invalide",
    "legacy": "événement sans enveloppe structurée",
}


def authenticate(message, config):
    """Comparer uniquement les identifiants simulés ; jamais une preuve réseau."""
    try:
        config = supervision.validate_config(config)
    except (ValueError, TypeError):
        return False
    if not isinstance(message, dict) or PROVENANCE_FIELDS.intersection(message):
        return False
    # Le contrat Phase 8 vérifie les deux IDs, le type privé et is_bot=False.
    return supervision.simulate_private_identity(config, message)["identity_matches"]


def _response(authorized, code, text=None, command=None):
    # Le texte libre d'entrée n'est jamais réémis. Échappement avant le bornage :
    # ne pas tronquer une entité HTML au milieu ni laisser du balisage actif.
    rendered = escape(text, quote=True) if text is not None else None
    if rendered is not None and len(rendered) > MAX_RESPONSE_LENGTH:
        rendered = "État actuel indisponible : réponse trop volumineuse."
    return {"simulation": True, "sent": False, "authorized": authorized,
            "code": code, "command": command, "text": rendered, "parse_mode": "HTML"}


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("timestamp")
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("timezone")
    return moment.astimezone(timezone.utc)


def _status(snapshot, now):
    header = "Instantané hors ligne du watcher ; aucune vérification en temps réel.\n"
    unavailable = "État actuel indisponible"
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return header + unavailable + " : référence temporelle invalide."
    if snapshot is None:
        return header + unavailable + " : instantané absent."
    if not isinstance(snapshot, dict):
        return header + unavailable + " : instantané invalide."
    try:
        initialized = snapshot["initialized"]
        inbox = snapshot["inbox"]
        if type(initialized) is not bool or not isinstance(inbox, list) or len(inbox) > MAX_INBOX_ENTRIES:
            raise ValueError("snapshot")
        anomalies = set()
        for event in inbox:
            if not isinstance(event, dict):
                raise ValueError("event")
            error = event.get("protocol_error")
            if error is not None:
                if not isinstance(error, str) or len(error) > 64:
                    raise ValueError("protocol_error")
                anomalies.add(PROTOCOL_ERRORS.get(error, "anomalie de protocole non détaillée"))
        delayed = snapshot.get("next_poll_at")
        if delayed is not None:
            if type(delayed) not in (float, int) or not math.isfinite(delayed) or delayed < 0:
                raise ValueError("next_poll_at")
            # Le délai est déjà enregistré par le watcher ; aucune relance.
            if delayed > now.timestamp():
                anomalies.add("prochain relevé différé dans l'instantané")
        raw_date = snapshot.get("last_poll_at")
        if raw_date is None:
            if initialized:
                raise ValueError("contradictory initialization")
            return (header + unavailable + " : aucune actualisation disponible.\n"
                    "État connu : non initialisé.\nFraîcheur : inconnue.\n" + _anomalies(anomalies))
        refreshed = _timestamp(raw_date)
        age = (now.astimezone(timezone.utc) - refreshed).total_seconds()
        if not initialized or age < 0:
            raise ValueError("contradictory snapshot")
        date = refreshed.isoformat(timespec="seconds")
        lines = [header.rstrip(), "Dernière actualisation disponible : " + date + ".",
                 f"Fraîcheur : âge {int(age)} s ; seuil {MAX_SNAPSHOT_AGE_SECONDS} s."]
        if age > MAX_SNAPSHOT_AGE_SECONDS:
            lines += [unavailable + " : instantané périmé.",
                      "Dernier état connu : watcher initialisé."]
        else:
            lines += ["État connu : watcher initialisé (instantané récent)."]
        lines += ["Activité actuelle du processus non vérifiée.", _anomalies(anomalies)]
        return "\n".join(lines)
    except (KeyError, ValueError, TypeError, OverflowError):
        return header + unavailable + " : instantané invalide ou contradictoire.\nFraîcheur : inconnue."


def _anomalies(anomalies):
    return "Anomalies de l'instantané : " + (
        "; ".join(sorted(anomalies)) + "." if anomalies else "aucune signalée dans les champs consultés."
    )


def _number(value):
    return isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,5}", value) is not None


def _freshness(value, now, poll):
    """Fraîcheur propre à l'élément ; ne pas rajeunir une ancienne CI."""
    if value is None:
        return "date indisponible ; fraîcheur inconnue"
    if type(value) in (int, float):
        if not math.isfinite(value) or value < 0:
            raise ValueError("date")
        moment = datetime.fromtimestamp(value, timezone.utc)
    else:
        moment = _timestamp(value)
    if moment > poll or moment > now:
        raise ValueError("date contradictoire")
    age = int((now - moment).total_seconds())
    label = "périmé" if (now - moment).total_seconds() > MAX_SNAPSHOT_AGE_SECONDS else "récent"
    return f"{moment.isoformat(timespec='seconds')} ; âge {age} s ; {label}"


def _github_link(value, pattern):
    # Deux niveaux : convention du watcher puis routes canoniques fermées.
    # Ni URL de redirection, requête, userinfo, port, encodage ou dépôt arbitraire.
    if (not isinstance(value, str) or len(value) > 240
            or not coordination.github_url(value, REPOSITORY)):
        return "lien indisponible"
    path = value.removeprefix(f"https://github.com/{REPOSITORY}/")
    return value if re.fullmatch(pattern, path) else "lien indisponible"


def _mapping(snapshot, key, limit):
    values = snapshot.get(key, {})
    if not isinstance(values, dict) or len(values) > limit:
        raise ValueError("registre invalide")
    return values


def _pr_line(number, data, snapshot, now, poll):
    if not _number(number) or not isinstance(data, dict) or type(data.get("closed")) is not bool:
        raise ValueError("PR invalide")
    closed = data["closed"]
    # Les champs facultatifs ne doivent pas contredire le format historique.
    state = data.get("state", "closed" if closed else "open")
    merged = data.get("merged")
    if (state not in ("open", "closed") or (state == "closed") != closed
            or (merged is not None and type(merged) is not bool) or (merged and not closed)):
        raise ValueError("PR contradictoire")
    title = data.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 4096 or not title.isprintable():
        title = "titre indisponible"
    elif len(title) > 80:
        title = title[:80] + "…"
    threads = _mapping(snapshot, "threads", 1001)
    thread = threads.get("pr:" + number, {})
    if not isinstance(thread, dict):
        raise ValueError("thread invalide")
    updated = data.get("updated_at", thread.get("updated_at"))
    date = _freshness(updated, now, poll)
    label = ("fusionnée" if merged else "fermée (non fusionnée)" if closed and merged is False
             else "fermée (fusion non renseignée)" if closed else "ouverte")
    link = _github_link(data.get("html_url", f"https://github.com/{REPOSITORY}/pull/{number}"),
                        rf"pull/{number}")
    return f"PR #{number} — {title}\nÉtat observé : {label}. Actualisation : {date}.\n{link}"


def _ci_line(sha, data, prs, now, poll):
    if not isinstance(sha, str) or not coordination.SHA.fullmatch(sha) or not isinstance(data, dict):
        raise ValueError("CI invalide")
    result = data.get("result")
    labels = {"success": "réussite", "failure": "échec", "pending": "en attente",
              "incomplete": "contrôles incomplets", "unknown": "information indisponible"}
    if not isinstance(result, str) or result not in labels:
        raise ValueError("CI invalide")
    # Le watcher stocke un résumé, pas les contrôles bruts. Refuser toute
    # représentation concurrente plutôt que l'interpréter comme un succès.
    if any(key in data for key in ("check_runs", "statuses", "conclusion", "state")):
        raise ValueError("CI contradictoire")
    associated = [number for number, pr in prs.items()
                  if _number(number) and isinstance(pr, dict) and pr.get("head_sha") == sha]
    association = ", ".join("#" + number for number in sorted(associated)[:3]) or "non renseignée"
    date = _freshness(data.get("checked_at"), now, poll)
    link = _github_link(data.get("html_url", f"https://github.com/{REPOSITORY}/commit/{sha}/checks"),
                        rf"(?:commit/{sha}/checks|actions/runs/[1-9][0-9]{{0,19}})")
    return f"Commit {sha} ; PR : {association}\nCI observée : {labels[result]}. Relevé : {date}.\n{link}"


def _report_line(identity, record, now, poll):
    if not isinstance(identity, str) or not coordination.uuid_text(identity) or not isinstance(record, dict):
        raise ValueError("rapport invalide")
    event = record.get("event")
    if not isinstance(event, dict):
        raise ValueError("rapport invalide")
    envelope = event.get("coordination")
    # Seul le registre structuré fait preuve ; jamais le corps d'un commentaire.
    if (event.get("prefix") != "CODEX-REPORT" or event.get("authorized_author") is not True
            or event.get("protocol_error") is not None or not isinstance(envelope, dict)):
        raise ValueError("rapport non vérifié")
    payload = json.dumps(envelope, ensure_ascii=True)
    parsed, error = coordination.parse_envelope("```json\n" + payload + "\n```", REPOSITORY)
    if error or parsed["kind"] != "report" or parsed["coordination_id"] != identity:
        raise ValueError("rapport non structuré")
    for field in ("conflict", "source_missing", "source_changed"):
        if field in record and type(record[field]) is not bool:
            raise ValueError("rapport invalide")
    if "conflict" not in record:
        raise ValueError("rapport non vérifié")
    condition = "source contradictoire ou non vérifiée" if any(
        record.get(key) for key in ("conflict", "source_missing", "source_changed")
    ) else "source structurée connue"
    date = _freshness(event.get("created_at"), now, poll)
    pr = "#" + str(parsed["pull_request"]) if parsed["pull_request"] else "non renseignée"
    link = _github_link(event.get("url"),
                        r"(?:issues/[1-9][0-9]{0,5}#issuecomment-[1-9][0-9]{0,19}|"
                        r"pull/[1-9][0-9]{0,5}#(?:issuecomment-|discussion_r|pullrequestreview-)[1-9][0-9]{0,19})")
    return (f"Rapport {identity} ; PR : {pr}\nType : report ; état observé : {parsed['status']} ; "
            f"{condition}.\nDate du rapport : {date}.\n{link}")


def _registry_view(command, argument, snapshot, now):
    header = "Instantané hors ligne ; état actuel non vérifié. Aucune aptitude à fusionner déduite."
    try:
        if (not isinstance(snapshot, dict) or not isinstance(now, datetime) or now.tzinfo is None
                or now.utcoffset() is None or snapshot.get("initialized") is not True
                or snapshot.get("repository", REPOSITORY) != REPOSITORY):
            raise ValueError("instantané")
        now = now.astimezone(timezone.utc)
        poll = _timestamp(snapshot.get("last_poll_at"))
        freshness = _freshness(snapshot.get("last_poll_at"), now, poll)
        header += "\nDernier relevé : " + freshness + ". Seuil : 3600 s."
        if (now - poll).total_seconds() > MAX_SNAPSHOT_AGE_SECONDS:
            header += "\nÉtat actuel indisponible : instantané périmé ; données historiques ci-dessous."
        # Le watcher borne les PR ouvertes à 100, mais conserve aussi les fermées.
        prs = _mapping(snapshot, "pr_snapshots", 1000)
        if command == "/pr":
            values = prs
            formatter = lambda key, value: _pr_line(key, value, snapshot, now, poll)
        elif command == "/ci":
            values = _mapping(snapshot, "ci_snapshots", 100)
            if argument and _number(argument):
                pr = prs.get(argument)
                if not isinstance(pr, dict) or not isinstance(pr.get("head_sha"), str):
                    return header + "\nPR inconnue ou commit indisponible dans l'instantané."
                argument = pr["head_sha"]
            formatter = lambda key, value: _ci_line(key, value, prs, now, poll)
        else:
            values = _mapping(snapshot, "coordination_registry", 1000)
            # Les autres types structurés ne sont pas présentés comme rapports.
            values = {key: value for key, value in values.items()
                      if isinstance(value, dict) and isinstance(value.get("event"), dict)
                      and isinstance(value["event"].get("coordination"), dict)
                      and value["event"]["coordination"].get("kind") == "report"}
            formatter = lambda key, value: _report_line(key, value, now, poll)
        if argument:
            if argument not in values:
                return header + "\nÉlément inconnu ou résultat absent de l'instantané."
            values = {argument: values[argument]}
        if not values:
            return header + "\nAucun élément connu dans l'instantané."
        lines = [header]
        displayed = 0
        # Garder des éléments entiers après échappement, réserver la note finale.
        for key, value in sorted(values.items(), key=lambda item: str(item[0])[:80]):
            if displayed >= MAX_DISPLAY_ITEMS:
                break
            try:
                line = formatter(key, value)
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                line = "Élément indisponible : données invalides, contradictoires ou non vérifiées."
            if len(escape("\n\n".join(lines + [line]), quote=True)) > MAX_RESPONSE_LENGTH - 120:
                break
            lines.append(line)
            displayed += 1
        if len(values) > displayed:
            lines.append("Affichage limité ; d'autres éléments de l'instantané ne sont pas affichés.")
        return "\n\n".join(lines)
    except (ValueError, TypeError, KeyError, OverflowError):
        return header + "\nÉtat actuel indisponible : instantané absent, invalide ou contradictoire."


def route_message(message, config, snapshot, now):
    """Retourner des données pour un futur transport, sans destination ni envoi.

    Accepte le sous-ensemble from/chat/text d'un message Telegram simulé et
    l'état en mémoire du watcher (initialized, last_poll_at, inbox, next_poll_at).
    Les vues privées lisent aussi pr_snapshots, ci_snapshots, threads et
    coordination_registry. Aucun corps libre n'est exposé. now est explicite.
    """
    if not authenticate(message, config):
        return _response(False, "denied")
    text = message.get("text")
    if not isinstance(text, str) or not text or len(text) > MAX_COMMAND_LENGTH:
        return _response(True, "invalid_message", "Message invalide. Consultez /help.")
    # Arguments bornés de consultation seulement ; aucune normalisation implicite.
    command, separator, argument = text.partition(" ")
    argument = argument if separator else None
    valid = command in {"/start", "/help", "/status", "/pr", "/ci", "/rapports"}
    if argument is not None:
        valid = valid and ((command in {"/pr", "/ci"} and _number(argument))
                          or (command == "/ci" and coordination.SHA.fullmatch(argument) is not None)
                          or (command == "/rapports" and coordination.uuid_text(argument)))
    if not valid:
        return _response(True, "unsupported_command",
                         "Commande inconnue ou arguments non autorisés. Consultez /help.")
    if command == "/start":
        answer = "QG mobile Codex — simulation privée hors ligne.\n" + HELP
    elif command == "/help":
        answer = HELP
    elif command == "/status":
        answer = _status(snapshot, now)
    else:
        answer = _registry_view(command, argument, snapshot, now)
    return _response(True, "ok", answer, command)
