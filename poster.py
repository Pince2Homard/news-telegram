"""Collecte de flux et diffusion Telegram, avec publication verrouillée par défaut."""
import html
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests

DELAI = 15
DELAI_RAPIDE = 15
SEUIL_RAPIDE = 8
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
    if (not isinstance(state["vus"], list) or not isinstance(state["file"], list)
            or not isinstance(state["erreurs"], list)):
        raise ValueError("Format invalide dans state.json : listes 'vus', 'file' et 'erreurs' requises")
    if not isinstance(state["feeds"], list) or not isinstance(state["yt"], dict):
        raise ValueError("Format invalide dans state.json : 'feeds' ou 'yt'")
    if not isinstance(state["feed_health"], dict):
        raise ValueError("Format invalide dans state.json : 'feed_health'")
    if not isinstance(state["feed_offset"], int):
        raise ValueError("Format invalide dans state.json : 'feed_offset'")

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
            print("Erreur résolution YouTube |", type(exc).__name__)

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
            # Log only at powers of two to limit recurring failure noise.
            streak = health["consecutive_errors"]
            if streak & (streak - 1) == 0:
                print("RSS erreur |", _feed_label(url), "|", type(exc).__name__,
                      "| échecs consécutifs:", streak,
                      "| durée:", error_duration, "s")
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


def send_message(item, token, chat_id, http=requests, sleep=time.sleep):
    for attempt in range(3):
        try:
            response = http.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": item["text"], "parse_mode": "HTML"},
                timeout=30,
            )
        except requests.RequestException as exc:
            # Résultat ambigu : Telegram a peut-être reçu le message.
            # Ne pas réessayer automatiquement pour éviter un doublon.
            print("Résultat d'envoi Telegram incertain, aucun nouvel essai |", type(exc).__name__)
            return False

        if response.ok:
            return True
        if response.status_code == 429:
            try:
                wait = int(response.json()["parameters"]["retry_after"])
            except (KeyError, TypeError, ValueError):
                wait = 30
            print("Limite Telegram, attente de", wait, "s")
            if attempt < 2:
                sleep(min(max(wait, 1), 60))
            continue

        message = f"HTTP {response.status_code}"
        print("Erreur Telegram HTTP", response.status_code)
        if response.status_code in (401, 403):
            raise GlobalTelegramError(message)
        if 400 <= response.status_code < 500:
            raise PermanentTelegramError(message)
        return False
    return False


def process_queue(
    state, seen, token, chat_id, state_path="state.json", dry_run=True,
    sender=send_message, sleep=time.sleep, max_per_run=MAX_PAR_RUN,
    delay=DELAI, fast_delay=DELAI_RAPIDE, fast_threshold=SEUIL_RAPIDE,
    time_limit=11 * 60, clock=time.time,
):
    if dry_run:
        print("DRY-RUN : aucune requête Telegram et aucune sauvegarde.")
        for item in state["file"]:
            print("SIMULATION |", item.get("pub") or "?", "|", _feed_label(item.get("link", "")))
        return 0

    # La file et les identifiants sont persistés avant toute tentative d'envoi.
    save_state(state, seen, state_path)
    started = clock()
    sent = 0
    while state["file"] and sent < max_per_run and clock() - started < time_limit:
        if sent:
            sleep(fast_delay if len(state["file"]) > fast_threshold else delay)
        item = state["file"][0]
        try:
            delivered = sender(item, token, chat_id)
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
        if not delivered:
            # Résultat ambigu ou erreur temporaire : conserver l’article en tête.
            break
        print("ENVOYÉ | publié:", item.get("pub") or "?", "|", _feed_label(item["link"]))
        state["file"].pop(0)
        sent += 1
        save_state(state, seen, state_path)

    save_state(state, seen, state_path)
    remaining = state["file"]
    print(
        "FILE D'ATTENTE après envoi :", len(remaining), "article(s) |",
        sent, "envoyé(s) ce passage | plus ancien en attente, publié :",
        (remaining[0].get("pub") or "?") if remaining else "-",
    )
    return sent


def main():
    # Double verrou : sans ALLOW_TELEGRAM_SEND=1, publication impossible.
    # DRY_RUN=1 reste prioritaire même si l'autorisation est présente.
    dry_run = os.getenv("DRY_RUN", "1").strip().lower() in ("1", "true", "yes")
    allow_send = os.getenv("ALLOW_TELEGRAM_SEND") == "1"
    if not allow_send:
        dry_run = True

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
        time_limit=remaining,
    )


if __name__ == "__main__":
    main()
