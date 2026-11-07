import html, json, os, re, time, requests, feedparser

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT"]
DELAI = 120        # secondes entre deux articles
MAX_PAR_RUN = 5    # articles envoyés à chaque passage
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

FEEDS = [l.strip() for l in open("feeds.txt") if l.strip() and not l.startswith("#")]

try:
    etat = json.load(open("state.json"))
except FileNotFoundError:
    etat = {"vus": [], "file": [], "init": False}
etat.setdefault("yt", {})

GDATA = re.compile(r"https?://gdata\.youtube\.com/feeds/base/users/([^/]+)/uploads")
MOTIFS = [
    r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
    r'"externalId":"(UC[\w-]{22})"',
    r'itemprop="channelId" content="(UC[\w-]{22})"',
]

def resoudre(url):
    """Transforme une ancienne adresse YouTube (gdata) en flux actuel."""
    m = GDATA.match(url)
    if not m:
        return url
    nom = m.group(1)
    if nom not in etat["yt"]:
        try:
            r = requests.get(f"https://www.youtube.com/user/{nom}",
                             headers={"User-Agent": UA, "Accept-Language": "fr"},
                             cookies={"CONSENT": "YES+1"}, timeout=30)
            for motif in MOTIFS:
                f = re.search(motif, r.text)
                if f:
                    etat["yt"][nom] = f.group(1)
                    break
        except Exception as e:
            print("Erreur", nom, e)
    if nom in etat["yt"]:
        return "https://www.youtube.com/feeds/videos.xml?channel_id=" + etat["yt"][nom]
    print("Chaine YouTube non resolue :", nom)
    return None

premier = not etat["init"]   # 1er lancement : on memorise sans envoyer
vus = dict.fromkeys(etat["vus"])
for url in FEEDS:
    url = resoudre(url)
    if not url:
        continue
    for e in reversed(feedparser.parse(url, agent=UA).entries):
        uid = e.get("id") or e.get("link")
        if uid and uid not in vus:
            vus[uid] = None
            if not premier:
                titre = html.escape(e.get("title", ""))
                etat["file"].append(f"<b>{titre}</b>\n{e.get('link', '')}")
etat["init"] = True

envoyes = 0
while etat["file"] and envoyes < MAX_PAR_RUN:
    if envoyes:
        time.sleep(DELAI)
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": etat["file"][0], "parse_mode": "HTML"},
        timeout=30,
    )
    if not r.ok:
        print(r.text)
        break
    etat["file"].pop(0)
    envoyes += 1

etat["vus"] = list(vus)[-5000:]
json.dump(etat, open("state.json", "w"))
