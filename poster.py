import html, json, os, re, time, requests, feedparser

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT"]
DELAI = 15         # secondes entre deux articles
DELAI_RAPIDE = 15  # (meme valeur : plus de rythme different quand la file est longue)
SEUIL_RAPIDE = 8
MAX_PAR_RUN = 50   # articles envoyes au maximum a chaque passage (limite aussi par le temps)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

FEEDS = [l.strip() for l in open("feeds.txt") if l.strip() and not l.startswith("#")]

GDATA = re.compile(r"https?://gdata\.youtube\.com/feeds/base/users/([^/]+)/uploads")
MOTIFS = [
    r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
    r'"externalId":"(UC[\w-]{22})"',
    r'itemprop="channelId" content="(UC[\w-]{22})"',
]

try:
    etat = json.load(open("state.json"))
except FileNotFoundError:
    etat = {"vus": [], "file": [], "init": False}
etat.setdefault("yt", {})

# Remise a zero de la liste des flux connus et de la file (version 2) :
# un flux n'est "connu" qu'apres avoir reussi a fournir des articles au moins une fois
if etat.get("v") != 2:
    etat["feeds"] = []
    etat["file"] = []
    etat["v"] = 2


def entrees(url):
    """Retourne les articles d'un flux (gere les anciennes adresses YouTube)."""
    m = GDATA.match(url)
    if not m:
        return feedparser.parse(url, agent=UA).entries
    nom = m.group(1)
    d = feedparser.parse("https://www.youtube.com/feeds/videos.xml?user=" + nom, agent=UA)
    if d.entries:
        return d.entries
    if nom not in etat["yt"]:
        try:
            r = requests.get("https://www.youtube.com/user/" + nom,
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
        d = feedparser.parse("https://www.youtube.com/feeds/videos.xml?channel_id=" + etat["yt"][nom], agent=UA)
        return d.entries
    print("Chaine YouTube non resolue :", nom)
    return []


def oembed(link):
    """Diagnostic : YouTube accepte-t-il de decrire cette video ? (200 = oui)"""
    if "youtube.com" not in link and "youtu.be" not in link:
        return "-"
    try:
        r = requests.get("https://www.youtube.com/oembed",
                         params={"url": link, "format": "json"},
                         headers={"User-Agent": UA}, timeout=15)
        return str(r.status_code)
    except Exception:
        return "erreur"


vus = dict.fromkeys(etat["vus"])
for url in FEEDS:
    try:
        liste = entrees(url)
    except Exception as e:
        print("Erreur flux", url, e)
        liste = []
    # un flux jamais vu avec des articles est memorise sans etre publie
    silencieux = url not in etat["feeds"]
    if liste and silencieux:
        etat["feeds"].append(url)
    for e in reversed(liste):
        uid = e.get("id") or e.get("link")
        if uid and uid not in vus:
            vus[uid] = None
            if not silencieux:
                titre = html.escape(e.get("title", ""))
                lien = e.get("link", "")
                etat["file"].append({"text": f"<b>{titre}</b>\n{lien}",
                                     "link": lien, "pub": e.get("published", "")})
etat["init"] = True
DEBUT = time.time()


def save():
    etat["vus"] = list(vus)[-5000:]
    json.dump(etat, open("state.json", "w"))


def envoyer(item):
    """Envoie un message ; reessaie en cas de coupure reseau ou de limite Telegram."""
    for essai in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": item["text"], "parse_mode": "HTML"},
                timeout=30,
            )
        except Exception as e:
            print("Erreur reseau Telegram :", e)
            time.sleep(5)
            continue
        if r.ok:
            return True
        if r.status_code == 429:
            try:
                attente = r.json()["parameters"]["retry_after"]
            except Exception:
                attente = 30
            print("Limite Telegram, attente de", attente, "s")
            time.sleep(min(attente, 60))
            continue
        print(r.text)
        return False
    return False


save()  # on memorise les flux et la file avant d'envoyer
print("FILE D'ATTENTE avant envoi :", len(etat["file"]), "article(s)")

envoyes = 0
while etat["file"] and envoyes < MAX_PAR_RUN and time.time() - DEBUT < 11 * 60:
    if envoyes:
        time.sleep(DELAI_RAPIDE if len(etat["file"]) > SEUIL_RAPIDE else DELAI)
    item = etat["file"][0]
    if not envoyer(item):
        break
    print("ENVOYE | publie:", item.get("pub") or "?", "|", item["link"])
    etat["file"].pop(0)
    envoyes += 1
    save()  # sauvegarde apres chaque message : plus de doublon si le script s'arrete

save()
reste = etat["file"]
print("FILE D'ATTENTE apres envoi :", len(reste), "article(s) |",
      envoyes, "envoye(s) ce passage | plus ancien en attente, publie :",
      (reste[0].get("pub") or "?") if reste else "-")
