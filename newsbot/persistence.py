"""persistence : responsabilité extraite sans changement de règle."""
import json
import math
import os
import re
import tempfile
from pathlib import Path
from .settings import MAX_ERREURS, MAX_URL_SEEN


def load_state(path="state.json"):
    try:
        with open(path, encoding="utf-8") as source:
            state = json.load(source)
    except FileNotFoundError:
        state = {"vus": [], "file": [], "init": False}

    if not isinstance(state, dict):
        raise ValueError("state.json doit contenir un objet JSON")
    state.setdefault("vus", [])
    state.setdefault("file", [])
    state.setdefault("erreurs", [])
    state.setdefault("feeds", [])
    state.setdefault("yt", {})
    state.setdefault("feed_health", {})
    state.setdefault("feed_offset", 0)
    state.setdefault("last_successful_send_at", None)
    state.setdefault("last_successful_message_id", None)
    state.setdefault("metrics_history", [])
    state.setdefault("admission_buffer", [])
    state.setdefault("date_quarantine", [])
    state.setdefault("url_seen", [])
    if (not isinstance(state["vus"], list) or not isinstance(state["file"], list)
            or not isinstance(state["erreurs"], list)):
        raise ValueError("Format invalide dans state.json : listes 'vus', 'file' et 'erreurs' requises")
    if not isinstance(state["feeds"], list) or not isinstance(state["yt"], dict):
        raise ValueError("Format invalide dans state.json : 'feeds' ou 'yt'")
    if not isinstance(state["feed_health"], dict):
        raise ValueError("Format invalide dans state.json : 'feed_health'")
    if not isinstance(state["metrics_history"], list):
        raise ValueError("Format invalide dans state.json : 'metrics_history'")
    if (not isinstance(state["admission_buffer"], list)
            or not isinstance(state["date_quarantine"], list)
            or not isinstance(state["url_seen"], list)):
        raise ValueError(
            "Format invalide dans state.json : 'admission_buffer', "
            "'date_quarantine' ou 'url_seen'",
        )
    for buffer_name in ("admission_buffer", "date_quarantine"):
        for record in state[buffer_name]:
            if not isinstance(record, dict):
                raise ValueError(f"Format invalide dans state.json : '{buffer_name}'")
            if not isinstance(record.get("item"), dict):
                raise ValueError(
                    f"Format invalide dans state.json : article de '{buffer_name}'",
                )
            uid = record.get("uid")
            if uid is not None and not isinstance(uid, str):
                raise ValueError(
                    f"Format invalide dans state.json : identifiant de '{buffer_name}'",
                )
            fingerprint = record.get("url_hash")
            if fingerprint is not None and (
                not isinstance(fingerprint, str)
                or not re.fullmatch(r"[0-9a-f]{32}", fingerprint)
            ):
                raise ValueError(
                    f"Format invalide dans state.json : empreinte de '{buffer_name}'",
                )
            for timestamp_name in ("first_seen_at", "admitted_at"):
                timestamp = record.get(timestamp_name)
                if timestamp is None and timestamp_name not in record:
                    continue
                if (isinstance(timestamp, bool)
                        or not isinstance(timestamp, (int, float))
                        or not math.isfinite(timestamp) or timestamp < 0):
                    raise ValueError(
                        f"Format invalide dans state.json : {timestamp_name} de '{buffer_name}'",
                    )
            published_at = record.get("published_at")
            if published_at is not None and (
                isinstance(published_at, bool)
                or not isinstance(published_at, (int, float))
                or not math.isfinite(published_at)
            ):
                raise ValueError(
                    f"Format invalide dans state.json : date de '{buffer_name}'",
                )
    if any(not isinstance(value, str) for value in state["url_seen"]):
        raise ValueError("Format invalide dans state.json : 'url_seen'")
    state["url_seen"] = list(dict.fromkeys(state["url_seen"]))[-MAX_URL_SEEN:]
    if not isinstance(state["feed_offset"], int):
        raise ValueError("Format invalide dans state.json : 'feed_offset'")
    last_send = state["last_successful_send_at"]
    if (last_send is not None and
            (isinstance(last_send, bool) or not isinstance(last_send, (int, float))
             or not math.isfinite(last_send) or last_send < 0)):
        raise ValueError("Format invalide dans state.json : 'last_successful_send_at'")
    message_id = state["last_successful_message_id"]
    if (message_id is not None and
            (isinstance(message_id, bool) or not isinstance(message_id, (int, str)))):
        raise ValueError("Format invalide dans state.json : 'last_successful_message_id'")

    # Migration non destructive : ne jamais effacer la file d'attente existante.
    if state.get("v") != 2:
        state["v"] = 2
    return state


def save_state(state, seen, path="state.json"):
    """Écriture atomique : un arrêt ne laisse pas un JSON partiellement écrit."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    state["vus"] = list(seen)[-5000:]
    state["erreurs"] = state.get("erreurs", [])[-MAX_ERREURS:]
    state["url_seen"] = list(dict.fromkeys(state.get("url_seen", [])))[-MAX_URL_SEEN:]
    state.setdefault("admission_buffer", [])
    state.setdefault("date_quarantine", [])
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(target.parent),
            prefix=f".{target.name}.", suffix=".tmp", delete=False,
        ) as temp:
            temp_path = temp.name
            json.dump(state, temp, ensure_ascii=False, separators=(",", ":"))
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_path, target)
        # Synchroniser le répertoire améliore la durabilité du remplacement atomique.
        try:
            directory_fd = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
