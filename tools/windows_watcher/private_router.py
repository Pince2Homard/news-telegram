"""Routeur privé simulé, pur : aucune lecture, écriture ou fonction de transport."""
from datetime import datetime, timezone
from html import escape
import math

from . import supervision


MAX_RESPONSE_LENGTH = 2048
MAX_COMMAND_LENGTH = 256
MAX_SNAPSHOT_AGE_SECONDS = 3600
MAX_INBOX_ENTRIES = 1500  # Limites existantes : 1000 ouverts + 500 historiques.
PROVENANCE_FIELDS = frozenset({
    "sender_chat", "sender_business_bot", "business_connection_id", "via_bot",
    "forward_origin", "forward_from", "forward_from_chat", "forward_date",
    "forward_sender_name", "forward_signature", "forward_from_message_id",
    "is_automatic_forward", "author_signature",
})
HELP = (
    "/start — Présenter le QG mobile Codex.\n"
    "/help — Afficher les commandes disponibles.\n"
    "/status — Consulter un instantané du watcher, sans vérification en direct."
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


def route_message(message, config, snapshot, now):
    """Retourner des données pour un futur transport, sans destination ni envoi.

    Accepte le sous-ensemble from/chat/text d'un message Telegram simulé et
    l'état en mémoire du watcher (initialized, last_poll_at, inbox, next_poll_at).
    Les autres données du watcher, notamment corps, URLs, CI et registre GitHub,
    sont ignorées. now est un datetime conscient du fuseau fourni par l'appelant.
    """
    if not authenticate(message, config):
        return _response(False, "denied")
    text = message.get("text")
    if not isinstance(text, str) or not text or len(text) > MAX_COMMAND_LENGTH:
        return _response(True, "invalid_message", "Message invalide. Consultez /help.")
    # Formes exactes seulement : pas d'arguments, suffixe @bot ou normalisation.
    if text not in {"/start", "/help", "/status"}:
        return _response(True, "unsupported_command",
                         "Commande inconnue ou arguments non autorisés. Consultez /help.")
    if text == "/start":
        answer = "QG mobile Codex — simulation privée hors ligne.\n" + HELP
    elif text == "/help":
        answer = HELP
    else:
        answer = _status(snapshot, now)
    return _response(True, "ok", answer, text)
