"""Collecte de flux et diffusion Telegram, avec publication verrouillée par défaut."""
import html
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
    state.setdefault("feeds", [])
    state.setdefault("yt", {})
    state.setdefault("init", False)
    if not isinstance(state["vus"], list) or not isinstance(state["file"], list):
        raise ValueError("Format invalide dans state.json : listes 'vus' et 'file' requises")
    if not isinstance(state["feeds"], list) or not isinstance(state["yt"], dict):
        raise ValueError("Format invalide dans state.json : 'feeds' ou 'yt'")

    if state.get("v") != 2:
        state["feeds"] = []
        state["file"] = []
        state["v"] = 2
    return state


def save_state(state, seen, path="state.json"):
    """Écriture atomique : un arrêt ne laisse pas un JSON partiellement écrit."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    state["vus"] = list(seen)[-5000:]
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


def entries_for_feed(url, state, http=requests, parser=feedparser.parse):
    match = GDATA.match(url)
    if not match:
        return parser(url, agent=UA).entries

    username = match.group(1)
    data = parser("https://www.youtube.com/feeds/videos.xml?user=" + username, agent=UA)
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
            print("Erreur résolution YouTube", username, exc)

    if username in state["yt"]:
        data = parser(
            "https://www.youtube.com/feeds/videos.xml?channel_id=" + state["yt"][username],
            agent=UA,
        )
        return data.entries
    print("Chaîne YouTube non résolue :", username)
    return []


def collect_articles(feeds, state, http=requests, parser=feedparser.parse):
    seen = dict.fromkeys(state["vus"])
    for url in feeds:
        try:
            entries = entries_for_feed(url, state, http=http, parser=parser)
        except Exception as exc:
            print("Erreur flux", url, exc)
            continue

        silent = url not in state["feeds"]
        if entries and silent:
            state["feeds"].append(url)

        for entry in reversed(entries):
            uid = entry.get("id") or entry.get("link")
            if not uid or uid in seen:
                continue
            seen[uid] = None
            if not silent:
                item = format_item(entry)
                if item:
                    state["file"].append(item)

    state["init"] = True
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
            print("Erreur réseau Telegram :", exc)
            if attempt < 2:
                sleep(5)
            continue

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

        print("Erreur Telegram HTTP", response.status_code, response.text[:500])
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
            print("SIMULATION |", item.get("pub") or "?", "|", item.get("link", ""))
        return 0

    save_state(state, seen, state_path)
    started = clock()
    sent = 0
    while state["file"] and sent < max_per_run and clock() - started < time_limit:
        if sent:
            sleep(fast_delay if len(state["file"]) > fast_threshold else delay)
        item = state["file"][0]
        if not sender(item, token, chat_id):
            break
        print("ENVOYÉ | publié:", item.get("pub") or "?", "|", item["link"])
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

    feeds = load_feeds(os.getenv("FEEDS_FILE", "feeds.txt"))
    state_path = os.getenv("STATE_FILE", "state.json")
    state = load_state(state_path)
    seen = collect_articles(feeds, state)

    print("FILE D'ATTENTE avant traitement :", len(state["file"]), "article(s)")
    if dry_run:
        process_queue(state, seen, None, None, state_path=state_path, dry_run=True)
        return

    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT")
    if not token or not chat_id:
        raise RuntimeError("Publication refusée : TELEGRAM_TOKEN et TELEGRAM_CHAT sont requis.")
    process_queue(state, seen, token, chat_id, state_path=state_path, dry_run=False)


if __name__ == "__main__":
    main()
