"""Validation compatible et sauvegarde atomique avec retries bornés."""
import json
import copy
import time
import math
import os
import re
import tempfile
from pathlib import Path
from .deduplication import is_http_url
from .settings import MAX_ERREURS, MAX_URL_SEEN


def load_state(path="state.json", *, allow_new=True):
    try:
        with open(path, encoding="utf-8") as source:
            state = json.load(source)
    except FileNotFoundError:
        if not allow_new:
            raise ValueError("État absent : initialisation explicite requise") from None
        state = {"vus": [], "file": [], "init": False}

    if not isinstance(state, dict):
        raise ValueError("state.json doit contenir un objet JSON")
    if "file" not in state:
        raise ValueError("État incomplet : file absente")
    if "vus" not in state and (state.get("init") or state.get("feeds") or state.get("file")):
        raise ValueError("État actif incomplet : vus absent")
    if state.get("v") is not None and (type(state["v"]) is not int or state["v"] not in (1, 2)):
        raise ValueError("Version d'état inconnue")
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

    if any(not isinstance(uid, str) for uid in state["vus"]):
        raise ValueError("Identifiants vus invalides")
    for item in state["file"]:
        if (not isinstance(item, dict) or not isinstance(item.get("text"), str)
                or not is_http_url(item.get("link"))):
            raise ValueError("Article invalide")
        retry = item.get("delivery_retry")
        if retry is not None:
            if not isinstance(retry, dict) or type(retry.get("uncertain_count")) is not int:
                raise ValueError("Compteur de reprise invalide")
            if not 0 <= retry["uncertain_count"] <= 3:
                raise ValueError("Compteur de reprise hors limites")
            for key in ("last_attempt_at", "not_before"):
                value = retry.get(key)
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError("Date de reprise invalide")
            if retry["not_before"] < retry["last_attempt_at"]:
                raise ValueError("Dates de reprise incohérentes")
    value = state.get("last_attempt_at", 0)
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Dernière tentative invalide")
    # Reject non-standard JSON numbers anywhere, including historical fields.
    json.dumps(state, allow_nan=False)
    # Migration non destructive : ne jamais effacer la file d'attente existante.
    if state.get("v") != 2:
        state["v"] = 2
    return state


def save_state(state, seen, path="state.json", *, sleep=time.sleep):
    """Retry only a frozen checkpoint, never its network side effects."""
    candidate = copy.deepcopy(state)
    candidate["vus"] = list(seen)[-5000:]
    dropped = max(0, len(candidate.get("erreurs", [])) - MAX_ERREURS)
    candidate["erreurs"] = candidate.get("erreurs", [])[-MAX_ERREURS:]
    if dropped:
        candidate["quarantine_pruned"] = candidate.get("quarantine_pruned", 0) + dropped
    candidate["url_seen"] = list(dict.fromkeys(candidate.get("url_seen", [])))[-MAX_URL_SEEN:]
    candidate.setdefault("admission_buffer", [])
    candidate.setdefault("date_quarantine", [])
    json.dumps(candidate, allow_nan=False)
    for attempt in range(3):
        try:
            _write_state(candidate, path)
            for key in ("vus", "erreurs", "url_seen"):
                state[key] = candidate[key]
            if dropped:
                state["quarantine_pruned"] = candidate["quarantine_pruned"]
                try:
                    print("PERSISTANCE | rétention erreurs | entrées retirées:", dropped)
                except OSError:
                    pass
            state.setdefault("admission_buffer", [])
            state.setdefault("date_quarantine", [])
            return
        except OSError:
            if attempt == 2:
                raise
            sleep((1, 5)[attempt])


def _write_state(state, path):
    """Écriture atomique : un arrêt ne laisse pas un JSON partiellement écrit."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(target.parent),
            prefix=f".{target.name}.", suffix=".tmp", delete=False,
        ) as temp:
            temp_path = temp.name
            json.dump(state, temp, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
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
