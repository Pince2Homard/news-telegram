"""Collecte de flux et diffusion Telegram, avec publication verrouillée par défaut."""
import html
import hashlib
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests

DEFAULT_TELEGRAM_INTERVAL_SECONDS = 30
MAX_INLINE_CADENCE_WAIT_SECONDS = 120
TELEGRAM_REQUEST_TIMEOUT_SECONDS = 30
TELEGRAM_MAX_ATTEMPTS = 3
TELEGRAM_MAX_RETRY_AFTER_SECONDS = 60
MAX_TELEGRAM_SEND_BUDGET_SECONDS = (
    TELEGRAM_MAX_ATTEMPTS * TELEGRAM_REQUEST_TIMEOUT_SECONDS
    + (TELEGRAM_MAX_ATTEMPTS - 1) * TELEGRAM_MAX_RETRY_AFTER_SECONDS
)
MAX_PAR_RUN = 50
MAX_ERREURS = 500
MAX_FILE = 1000
COLLECT_BUDGET = 180
RUN_BUDGET = 12 * 60
CHECKPOINT_EVERY = 5
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
GDATA = re.compile(r"https?://gdata\.youtube\.com/feeds/base/users/([^/]+)/uploads")
MOTIFS = [
    r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
    r'"externalId":"(UC[\w-]{22})"',
    r'itemprop="channelId" content="(UC[\w-]{22})"',
]


def load_feeds(path="feeds.txt"):
    with open(path, encoding="utf-8") as source:
        return [
            line.strip() for line in source
            if line.strip() and not line.lstrip().startswith("#")
        ]


class PermanentTelegramError(Exception):
    """Erreur Telegram propre à un article : l’article ne pourra pas être envoyé tel quel."""


class GlobalTelegramError(Exception):
    """Erreur de configuration ou d’accès Telegram : toute la file doit être préservée."""


@dataclass(frozen=True)
class TelegramSendResult:
    """Résultat confirmé d'un appel Telegram, sans conserver de donnée secrète."""

    success: bool
    message_id: int | str | None = None
    telegram_date: float | None = None

    def __bool__(self):
        return self.success


def get_telegram_interval_seconds(value=None):
    """Lit une cadence positive configurable par variable d'environnement."""
    raw = os.getenv("TELEGRAM_INTERVAL_SECONDS", str(DEFAULT_TELEGRAM_INTERVAL_SECONDS)) \
        if value is None else value
    try:
        interval = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("TELEGRAM_INTERVAL_SECONDS doit être un entier positif") from exc
    if interval <= 0:
        raise ValueError("TELEGRAM_INTERVAL_SECONDS doit être un entier positif")
    return interval


def _utc_iso(timestamp=None):
    moment = datetime.now(timezone.utc) if timestamp is None else datetime.fromtimestamp(
        float(timestamp), timezone.utc,
    )
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _http_status_from_exception(exc):
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _http_status_category(status):
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 410:
        return "gone"
    if status == 429:
        return "rate_limited"
    if status is not None and status >= 500:
        return "upstream_error"
    if status is not None and status >= 400:
        return "client_error"
    return "unknown"


def _retry_after_seconds(exc):
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    value = headers.get("Retry-After")
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


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
    if (not isinstance(state["vus"], list) or not isinstance(state["file"], list)
            or not isinstance(state["erreurs"], list)):
        raise ValueError("Format invalide dans state.json : listes 'vus', 'file' et 'erreurs' requises")
    if not isinstance(state["feeds"], list) or not isinstance(state["yt"], dict):
        raise ValueError("Format invalide dans state.json : 'feeds' ou 'yt'")
    if not isinstance(state["feed_health"], dict):
        raise ValueError("Format invalide dans state.json : 'feed_health'")
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


def is_http_url(value):
    try:
        parsed = urlparse(value)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except (TypeError, ValueError):
        return False


def format_item(entry):
    title = html.escape(str(entry.get("title") or "(sans titre)"))
    link = str(entry.get("link") or "").strip()
    if not is_http_url(link):
        return None
    return {
        "text": f"<b>{title}</b>\n{html.escape(link)}",
        "link": link,
        "pub": str(entry.get("published") or ""),
    }


def parse_feed(url, http=requests, parser=feedparser.parse, timeout=(5, 20)):
    """Télécharge un flux avec délais de connexion et de lecture bornés."""
    response = http.get(url, headers={"User-Agent": UA}, timeout=timeout)
    response.raise_for_status()
    return parser(response.content)


def entries_for_feed(url, state, http=requests, parser=feedparser.parse):
    match = GDATA.match(url)
    if not match:
        return parse_feed(url, http=http, parser=parser).entries

    username = match.group(1)
    data = parse_feed(
        "https://www.youtube.com/feeds/videos.xml?user=" + username,
        http=http, parser=parser,
    )
    if data.entries:
        return data.entries

    if username not in state["yt"]:
        try:
            response = http.get(
                "https://www.youtube.com/user/" + username,
                headers={"User-Agent": UA, "Accept-Language": "fr"},
                cookies={"CONSENT": "YES+1"}, timeout=30,
            )
            response.raise_for_status()
            for pattern in MOTIFS:
                found = re.search(pattern, response.text)
                if found:
                    state["yt"][username] = found.group(1)
                    break
        except requests.RequestException as exc:
            status = _http_status_from_exception(exc)
            print(
                "Erreur résolution YouTube |", type(exc).__name__,
                "| HTTP:", status if status is not None else "n/a",
                "| catégorie:", _http_status_category(status),
            )

    if username in state["yt"]:
        data = parse_feed(
            "https://www.youtube.com/feeds/videos.xml?channel_id=" + state["yt"][username],
            http=http, parser=parser,
        )
        return data.entries
    print("Chaîne YouTube non résolue")
    return []


def _feed_label(url):
    """Label stable de journal sans paramètres ni identifiants d’URL."""
    parsed = urlparse(url)
    host = parsed.hostname or "flux-inconnu"
    fingerprint = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return f"{host}#{fingerprint}"


def collect_articles(
    feeds, state, http=requests, parser=feedparser.parse,
    budget=COLLECT_BUDGET, clock=time.monotonic, checkpoint=None,
    checkpoint_every=CHECKPOINT_EVERY,
):
    state.setdefault("feed_health", {})
    state.setdefault("feeds", [])
    state.setdefault("file", [])
    state.setdefault("vus", [])
    state.setdefault("yt", {})
    seen = dict.fromkeys(state["vus"])
    # Les états historiques sans init mais avec de l’activité sont déjà en service.
    was_initialized = (
        bool(state["init"]) if "init" in state
        else bool(state.get("feeds") or state.get("vus") or state.get("file"))
    )
    # Rotation persistante : chaque exécution commence au flux suivant.
    # Elle évite que les derniers flux soient toujours pénalisés si la file sature.
    if feeds:
        offset = int(state.get("feed_offset", 0)) % len(feeds)
        ordered_feeds = feeds[offset:] + feeds[:offset]
    else:
        offset = 0
        ordered_feeds = []
        state["feed_offset"] = 0
    started = clock()
    counts = {"ok": 0, "empty": 0, "error": 0, "deferred_feeds": 0,
              "discovered": 0, "added": 0, "deferred": 0, "attempted": 0}
    for index, url in enumerate(ordered_feeds):
        if clock() - started >= budget:
            break
        feed_started = clock()
        counts["attempted"] += 1
        feed_deferred = 0
        feed_added = 0
        try:
            entries = entries_for_feed(url, state, http=http, parser=parser)
        except Exception as exc:
            counts["error"] += 1
            health = state["feed_health"].setdefault(url, {"consecutive_errors": 0})
            health["consecutive_errors"] = int(health.get("consecutive_errors", 0)) + 1
            health["last_error"] = type(exc).__name__
            error_duration = round(clock() - feed_started, 3)
            status = _http_status_from_exception(exc)
            retry_after = _retry_after_seconds(exc)
            health["last_http_status"] = status
            health["last_error_category"] = _http_status_category(status)
            # Log only at powers of two to limit recurring failure noise.
            streak = health["consecutive_errors"]
            if streak & (streak - 1) == 0:
                print(
                    "RSS erreur |", _feed_label(url), "|", type(exc).__name__,
                    "| HTTP:", status if status is not None else "n/a",
                    "| catégorie:", _http_status_category(status),
                    "| retry_after_s:", retry_after if retry_after is not None else "n/a",
                    "| échecs consécutifs:", streak,
                    "| durée:", error_duration, "s",
                )
            state["feed_offset"] = (offset + index + 1) % len(feeds)
            if checkpoint:
                checkpoint(state, seen)
            continue

        is_new_feed = url not in state["feeds"]
        if entries:
            counts["ok"] += 1
        else:
            counts["empty"] += 1
        health = state["feed_health"].setdefault(url, {"consecutive_errors": 0})
        health["consecutive_errors"] = 0
        health.pop("last_error", None)
        health.pop("last_http_status", None)
        health.pop("last_error_category", None)
        if entries and is_new_feed:
            state["feeds"].append(url)

        # On le premier démarrage, initialiser silencieusement tout l’historique.
        # Une source ajoutée plus tard est amorcée sans archive : ses anciennes
        # entrées sont baselinées, tandis que la plus récente reste publiable.
        silent_bootstrap = is_new_feed and not was_initialized
        ordered_entries = list(reversed(entries))
        cursor_uid = health.get("latest_uid")
        cursor_index = -1
        if cursor_uid is not None:
            cursor_index = next((
                i for i, entry in enumerate(ordered_entries)
                if str(entry.get("id") or entry.get("link") or "") == str(cursor_uid)
            ), -1)
        current_cursor = cursor_uid
        blocked_cursor = False
        for entry_index, entry in enumerate(ordered_entries):
            uid = entry.get("id") or entry.get("link")
            if not uid:
                continue
            uid = str(uid)
            # Les entrées antérieures au curseur ont déjà été parcourues, même
            # si leur ID a été éjecté de la fenêtre globale des 5 000 vus.
            if cursor_index >= 0 and entry_index < cursor_index:
                continue
            if cursor_index >= 0 and entry_index == cursor_index:
                if not blocked_cursor:
                    current_cursor = uid
                continue

            if uid in seen:
                if not blocked_cursor:
                    current_cursor = uid
                continue

            # Ne marquer comme vu qu'un article dont le lien est publiable.
            item = format_item(entry)
            if item is None:
                print("Article ignoré : lien HTTP(S) absent ou invalide.")
                blocked_cursor = True
                continue

            is_latest_new_feed_item = is_new_feed and entry_index == len(entries) - 1
            if is_new_feed and (silent_bootstrap or not is_latest_new_feed_item):
                seen[uid] = None
                if not blocked_cursor:
                    current_cursor = uid
                continue

            counts["discovered"] += 1

            # Ne pas faire croître la file sans limite : un article non ajouté
            # n'est pas marqué comme vu et sera réessayé au passage suivant.
            if len(state["file"]) >= MAX_FILE:
                feed_deferred += 1
                counts["deferred"] += 1
                blocked_cursor = True
                continue

            seen[uid] = None
            state["file"].append(item)
            counts["added"] += 1
            feed_added += 1
            if not blocked_cursor:
                current_cursor = uid

        if current_cursor is not None:
            health["latest_uid"] = current_cursor

        if feed_deferred:
            counts["deferred_feeds"] += 1
        duration = round(clock() - feed_started, 3)
        print("RSS |", _feed_label(url), "|", "vide" if not entries else "ok",
              "|", len(entries), "entrée(s) | +", feed_added,
              "article(s) | reportés:", feed_deferred, "|", duration, "s")
        # Le curseur pointe sur le prochain flux avant que l’état soit écrit :
        # une interruption au flux suivant reprend sans rebalayer le préfixe.
        state["feed_offset"] = (offset + index + 1) % len(feeds)
        if checkpoint and (index + 1) % max(1, checkpoint_every) == 0:
            checkpoint(state, seen)

    completed = counts["attempted"] == len(ordered_feeds)
    if completed and feeds:
        # Après un cycle complet, décaler le début d’un flux pour l’équité.
        state["feed_offset"] = (offset + 1) % len(feeds)
    elif not feeds:
        state["feed_offset"] = 0
    state["init"] = True
    recurrent = [
        _feed_label(url) for url, health in state["feed_health"].items()
        if url in feeds and int(health.get("consecutive_errors", 0)) >= 3
    ]
    print(
        "Collecte RSS | flux:", counts["attempted"], "/", len(ordered_feeds),
        "| ok:", counts["ok"], "| vides:", counts["empty"],
        "| erreurs:", counts["error"], "| flux avec reports:", counts["deferred_feeds"],
        "| découverts:", counts["discovered"], "| ajoutés:", counts["added"],
        "| reportés:", counts["deferred"], "| durée:", round(clock() - started, 3), "s",
        "| défaillances récurrentes:", ", ".join(recurrent) if recurrent else "aucune",
    )
    if checkpoint:
        checkpoint(state, seen)
    return seen


def send_message(
    item, token, chat_id, http=requests, sleep=time.sleep,
    monotonic=time.monotonic, clock=time.time,
):
    for attempt in range(TELEGRAM_MAX_ATTEMPTS):
        attempt_at = clock()
        request_started = monotonic()
        try:
            response = http.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": item["text"], "parse_mode": "HTML"},
                timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            # Résultat ambigu : Telegram a peut-être reçu le message.
            # Ne pas réessayer automatiquement pour éviter un doublon immédiat.
            duration = max(0, monotonic() - request_started)
            status = _http_status_from_exception(exc)
            print(
                "TELEGRAM tentative | début_utc:", _utc_iso(attempt_at),
                "| tentative:", f"{attempt + 1}/{TELEGRAM_MAX_ATTEMPTS}",
                "| résultat: réseau_ambigu | HTTP:", status if status is not None else "n/a",
                "| durée_s:", round(duration, 3), "| erreur:", type(exc).__name__,
            )
            return TelegramSendResult(False)

        duration = max(0, monotonic() - request_started)
        status = getattr(response, "status_code", None)
        if response.ok:
            message_id = None
            telegram_date = None
            try:
                payload = response.json()
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict):
                result = payload.get("result")
                if isinstance(result, dict):
                    candidate = result.get("message_id")
                    if isinstance(candidate, (int, str)) and not isinstance(candidate, bool):
                        message_id = candidate
                    candidate_date = result.get("date")
                    if (isinstance(candidate_date, (int, float))
                            and not isinstance(candidate_date, bool)):
                        telegram_date = float(candidate_date)
            confirmed_at = telegram_date if telegram_date is not None else clock()
            print(
                "TELEGRAM tentative | début_utc:", _utc_iso(attempt_at),
                "| tentative:", f"{attempt + 1}/{TELEGRAM_MAX_ATTEMPTS}",
                "| résultat: succès | HTTP:", status if status is not None else "n/a",
                "| durée_s:", round(duration, 3),
                "| confirmé_utc:", _utc_iso(confirmed_at),
                "| message_id:", message_id if message_id is not None else "indisponible",
            )
            return TelegramSendResult(True, message_id, telegram_date)

        if status == 429:
            try:
                wait = int(response.json()["parameters"]["retry_after"])
            except (KeyError, TypeError, ValueError):
                wait = 30
            print(
                "TELEGRAM tentative | début_utc:", _utc_iso(attempt_at),
                "| tentative:", f"{attempt + 1}/{TELEGRAM_MAX_ATTEMPTS}",
                "| résultat: rate_limited | HTTP: 429",
                "| durée_s:", round(duration, 3), "| retry_after_s:", wait,
            )
            if attempt < TELEGRAM_MAX_ATTEMPTS - 1:
                sleep(min(max(wait, 1), TELEGRAM_MAX_RETRY_AFTER_SECONDS))
            continue

        message = f"HTTP {status}"
        print(
            "TELEGRAM tentative | début_utc:", _utc_iso(attempt_at),
            "| tentative:", f"{attempt + 1}/{TELEGRAM_MAX_ATTEMPTS}",
            "| résultat: échec | HTTP:", status if status is not None else "n/a",
            "| catégorie:", _http_status_category(status),
            "| durée_s:", round(duration, 3),
        )
        if status in (401, 403):
            raise GlobalTelegramError(message)
        if isinstance(status, int) and 400 <= status < 500:
            raise PermanentTelegramError(message)
        return TelegramSendResult(False)
    return TelegramSendResult(False)


def process_queue(
    state, seen, token, chat_id, state_path="state.json", dry_run=True,
    sender=send_message, sleep=time.sleep, max_per_run=MAX_PAR_RUN,
    interval_seconds=None, time_limit=11 * 60, clock=time.time,
    monotonic=time.monotonic,
):
    if dry_run:
        print("DRY-RUN : aucune requête Telegram et aucune sauvegarde.")
        for item in state["file"]:
            print("SIMULATION |", item.get("pub") or "?", "|", _feed_label(item.get("link", "")))
        return 0

    interval = get_telegram_interval_seconds(interval_seconds)
    state.setdefault("last_successful_send_at", None)
    state.setdefault("last_successful_message_id", None)

    # La file et les identifiants sont persistés avant toute tentative d'envoi.
    save_state(state, seen, state_path)
    started = monotonic()
    sent = 0
    last_success = state.get("last_successful_send_at")
    next_allowed = last_success + interval if last_success is not None else None
    print(
        "CADENCE Telegram | intervalle_s:", interval,
        "| dernier_succès_utc:", _utc_iso(last_success) if last_success is not None else "aucun",
        "| prochaine_publication_utc:", _utc_iso(next_allowed) if next_allowed is not None else "immédiate",
        "| file:", len(state["file"]),
    )

    while state["file"] and sent < max_per_run:
        elapsed = monotonic() - started
        remaining_budget = time_limit - elapsed
        if remaining_budget < MAX_TELEGRAM_SEND_BUDGET_SECONDS:
            print(
                "PUBLICATION REPORTÉE | raison: budget_insuffisant",
                "| file:", len(state["file"]),
                "| prochaine_publication_utc:", _utc_iso(next_allowed) if next_allowed is not None else "immédiate",
            )
            break

        now = clock()
        last_success = state.get("last_successful_send_at")
        next_allowed = last_success + interval if last_success is not None else now
        wait_seconds = max(0, next_allowed - now)
        if wait_seconds:
            if wait_seconds > MAX_INLINE_CADENCE_WAIT_SECONDS:
                print(
                    "PUBLICATION REPORTÉE | raison: attente_trop_longue_pour_ce_workflow",
                    "| file:", len(state["file"]),
                    "| attente_s:", round(wait_seconds, 3),
                    "| prochaine_publication_utc:", _utc_iso(next_allowed),
                )
                break
            if wait_seconds + MAX_TELEGRAM_SEND_BUDGET_SECONDS > remaining_budget:
                print(
                    "PUBLICATION REPORTÉE | raison: budget_insuffisant_avant_prochaine_cadence",
                    "| file:", len(state["file"]),
                    "| prochaine_publication_utc:", _utc_iso(next_allowed),
                )
                break
            print(
                "ATTENTE CADENCE | secondes:", round(wait_seconds, 3),
                "| file:", len(state["file"]),
                "| prochaine_publication_utc:", _utc_iso(next_allowed),
            )
            sleep(wait_seconds)
            if clock() < next_allowed:
                print(
                    "PUBLICATION REPORTÉE | raison: attente_interrompue",
                    "| file:", len(state["file"]),
                    "| prochaine_publication_utc:", _utc_iso(next_allowed),
                )
                break

        item = state["file"][0]
        try:
            outcome = sender(item, token, chat_id)
        except GlobalTelegramError as exc:
            # Erreur globale d'identifiants/droits : ne jamais mettre les articles
            # en quarantaine. On arrête et conserve la file pour correction.
            print("ARRÊT DE SÉCURITÉ Telegram | file préservée |", exc)
            break
        except PermanentTelegramError as exc:
            # Une erreur propre à l'article ne sera pas corrigée par une nouvelle tentative.
            # Isoler l’article défectueux pour ne pas bloquer toute la file.
            state["erreurs"].append({
                "item": item, "error": str(exc), "failed_at": int(clock()),
            })
            state["file"].pop(0)
            save_state(state, seen, state_path)
            print("ARTICLE MIS EN QUARANTAINE |", _feed_label(item.get("link", "")), "|", exc)
            continue
        if not outcome:
            # Résultat ambigu ou erreur temporaire : conserver l'article en tête.
            break

        telegram_date = outcome.telegram_date if isinstance(outcome, TelegramSendResult) else None
        message_id = outcome.message_id if isinstance(outcome, TelegramSendResult) else None
        successful_at = clock()
        if telegram_date is not None:
            # Le champ date Telegram est à la seconde : arrondir prudemment vers
            # le futur pour ne jamais raccourcir l'intervalle minimal.
            successful_at = max(successful_at, telegram_date + 1)
        state["file"].pop(0)
        sent += 1
        state["last_successful_send_at"] = successful_at
        state["last_successful_message_id"] = message_id
        try:
            save_state(state, seen, state_path)
        except Exception as exc:
            print(
                "ALERTE PERSISTANCE | Telegram avait confirmé l'envoi",
                "| source:", _feed_label(item.get("link", "")),
                "| message_id:", message_id if message_id is not None else "indisponible",
                "| état_non_sauvé:", type(exc).__name__,
                "| doublon_possible_après_reprise: oui",
            )
            raise
        next_allowed = successful_at + interval
        print(
            "ENVOYÉ | publié:", item.get("pub") or "?",
            "| source:", _feed_label(item["link"]),
            "| confirmé_utc:", _utc_iso(successful_at),
            "| message_id:", message_id if message_id is not None else "indisponible",
            "| file:", len(state["file"]),
            "| prochaine_publication_utc:", _utc_iso(next_allowed),
        )

    save_state(state, seen, state_path)
    remaining = state["file"]
    last_success = state.get("last_successful_send_at")
    next_allowed = last_success + interval if last_success is not None else None
    print(
        "FILE D'ATTENTE après envoi :", len(remaining), "article(s) |",
        sent, "envoyé(s) ce passage | plus ancien en attente, publié :",
        (remaining[0].get("pub") or "?") if remaining else "-",
        "| prochaine_publication_utc:", _utc_iso(next_allowed) if next_allowed is not None else "immédiate",
    )
    return sent


def main():
    # Double verrou : sans ALLOW_TELEGRAM_SEND=1, publication impossible.
    # DRY_RUN=1 reste prioritaire même si l'autorisation est présente.
    dry_run = os.getenv("DRY_RUN", "1").strip().lower() in ("1", "true", "yes")
    allow_send = os.getenv("ALLOW_TELEGRAM_SEND") == "1"
    if not allow_send:
        dry_run = True

    interval_seconds = get_telegram_interval_seconds()

    token = None
    chat_id = None
    if not dry_run:
        token = os.getenv("TELEGRAM_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT")
        if not token or not chat_id:
            raise RuntimeError("Publication refusée : TELEGRAM_TOKEN et TELEGRAM_CHAT sont requis.")

    run_started = time.monotonic()
    feeds = load_feeds(os.getenv("FEEDS_FILE", "feeds.txt"))
    state_path = os.getenv("STATE_FILE", "state.json")
    state = load_state(state_path)
    checkpoint = None if dry_run else lambda current, seen: save_state(current, seen, state_path)
    seen = collect_articles(
        feeds, state, budget=min(COLLECT_BUDGET, RUN_BUDGET), checkpoint=checkpoint,
    )

    print("FILE D'ATTENTE avant traitement :", len(state["file"]), "article(s)")
    if dry_run:
        process_queue(state, seen, None, None, state_path=state_path, dry_run=True)
        return

    remaining = max(0, RUN_BUDGET - (time.monotonic() - run_started))
    process_queue(
        state, seen, token, chat_id, state_path=state_path, dry_run=False,
        interval_seconds=interval_seconds, time_limit=remaining,
    )


if __name__ == "__main__":
    main()
