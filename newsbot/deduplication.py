"""Identifiants, empreintes URL et reconstruction des index de déduplication."""
import hashlib
from urllib.parse import urlparse, urlsplit, urlunsplit
from .settings import MAX_URL_SEEN


def is_http_url(value):
    try:
        parsed = urlparse(value)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except (TypeError, ValueError):
        return False


def _canonical_url_fingerprint(value):
    """Hash URL sans fragment; conserve chemin et requête pour éviter les collisions sémantiques."""
    try:
        parsed = urlsplit(str(value or "").strip())
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return None
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port
        if port and not (
            parsed.scheme.lower() == "http" and port == 80
            or parsed.scheme.lower() == "https" and port == 443
        ):
            host = f"{host}:{port}"
        canonical = urlunsplit((
            parsed.scheme.lower(), host, parsed.path or "/", parsed.query, "",
        ))
    except (TypeError, ValueError, UnicodeError):
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _remember_url(state, fingerprint):
    if fingerprint:
        values = state.setdefault("url_seen", [])
        if fingerprint not in values:
            values.append(fingerprint)
            if len(values) > MAX_URL_SEEN:
                del values[:-MAX_URL_SEEN]


def _entry_uid(entry):
    value = entry.get("id") or entry.get("link")
    return str(value) if value else None


def _quarantine_match(state, uid, fingerprint):
    for index, record in enumerate(state.get("date_quarantine", [])):
        if uid and record.get("uid") == uid:
            return index
        if fingerprint and record.get("url_hash") == fingerprint:
            return index
    return None


def prepare_seen_indexes(state):
    """Reconstruit les index actifs ; complète url_seen et les empreintes manquantes.

    Retourne les IDs ordonnés, URLs historiques et URLs actives du passage.
    Aucun GUID historique n’est converti en URL.
    """
    seen = dict.fromkeys(state["vus"])
    # Un GUID peut ressembler à une URL sans être le lien de l'article. Ne pas
    # fabriquer de correspondance URL à partir de l'ancien historique d'IDs.
    url_seen = set(state["url_seen"])
    active_url_seen = set()
    for item in state["file"]:
        if isinstance(item, dict):
            fingerprint = _canonical_url_fingerprint(item.get("link"))
            _remember_url(state, fingerprint)
            if fingerprint:
                active_url_seen.add(fingerprint)
    for record in state["admission_buffer"] + state["date_quarantine"]:
        if record.get("uid"):
            seen[str(record["uid"])] = None
        fingerprint = record.get("url_hash")
        if not fingerprint and isinstance(record.get("item"), dict):
            fingerprint = _canonical_url_fingerprint(record["item"].get("link"))
            record["url_hash"] = fingerprint
        _remember_url(state, fingerprint)
        if fingerprint:
            active_url_seen.add(fingerprint)

    return seen, url_seen, active_url_seen
