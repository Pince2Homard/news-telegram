# news-telegram

Bot Python qui collecte des flux RSS/Atom et publie leurs nouveaux articles dans
un canal Telegram. GitHub Actions exécute la production ; `state.json`, versionné
dans Git, conserve les articles en attente et l'historique de déduplication.

## Installation

Utiliser **Python 3.12**, comme les workflows du dépôt, puis installer les
dépendances épinglées :

```sh
python -m venv .venv
# Linux/macOS : source .venv/bin/activate
# PowerShell : .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Les URL à collecter sont dans `feeds.txt`, une par ligne ; lignes vides et
commentaires commençant par `#` sont ignorés. Pour YouTube, utiliser directement
`https://www.youtube.com/feeds/videos.xml?channel_id=IDENTIFIANT_CHAINE`.
Le résolveur GDATA et son scraping ne sont plus pris en charge.

## Configuration

| Variable du processus | Valeur par défaut / rôle |
| --- | --- |
| `DRY_RUN` | `1` : simulation, sans publication ni sauvegarde d'état |
| `ALLOW_TELEGRAM_SEND` | Absente : impose la simulation ; seule la valeur `1` autorise l'envoi |
| `TELEGRAM_TOKEN`, `TELEGRAM_CHAT` | Requis pour publier ; secrets existants du dépôt en production |
| `TELEGRAM_INTERVAL_SECONDS` | Entier positif, `30` : intervalle minimal entre succès confirmés |
| `ARTICLE_MAX_AGE_HOURS` | Entier positif, `24` : fenêtre de fraîcheur des nouveaux candidats |
| `FEEDS_FILE` | `feeds.txt` |
| `STATE_FILE` | `state.json` |

La publication exige explicitement `DRY_RUN=0` **et** `ALLOW_TELEGRAM_SEND=1`.
`DRY_RUN=1` reste prioritaire. Le workflow de production configure ces deux
verrous et transmet la variable de dépôt `TELEGRAM_INTERVAL_SECONDS` avec repli à
30 s. Il conserve le défaut de 24 h pour la fraîcheur : il ne transmet pas de
variable de dépôt `ARTICLE_MAX_AGE_HOURS`.

## Utilisation

Une simulation du bot télécharge les flux, applique les règles en mémoire et
affiche les articles en attente. Elle ne contacte pas Telegram et ne sauvegarde
pas l'état. Sous Linux/macOS :

```sh
DRY_RUN=1 ALLOW_TELEGRAM_SEND=0 python poster.py
```

Sous PowerShell :

```powershell
$env:DRY_RUN = '1'
$env:ALLOW_TELEGRAM_SEND = '0'
python poster.py
```

Le simulateur de cadence est un outil exécuté explicitement :

```sh
python cadence_simulator.py --state state.json
```

Il lit une copie de l'état et écrit une analyse JSON sur la sortie standard,
sans réseau, publication ni écriture d'état. Ses projections restent informatives ;
la cadence effective du bot dépend uniquement de sa configuration.

## Tests

```sh
python -m unittest discover -s tests -v
python tools/compare_reference.py
```

Les tests utilisent des transports simulés et des horloges contrôlées. La
comparaison charge le code Git de référence `1dd233d` et utilise des états copiés
ou synthétiques ; elle ne modifie pas l'état de production. Le commit de référence
doit être présent dans l'historique local. Un autre commit peut être sélectionné
avec `--reference COMMIT`.

## Limites essentielles

- La cadence minimale entre succès est persistée, mais GitHub Actions ne garantit
  pas des démarrages ni des publications à intervalles réguliers.
- Les capacités et budgets sont bornés. Un candidat non encore conservé peut
  disparaître du flux source avant qu'une place se libère. Un article déjà admis
  reste en attente même s'il vieillit.
- La livraison peut être répétée : Telegram peut accepter un message avant un
  échec de sauvegarde locale ou distante. La reprise ne garantit pas une livraison
  exactement une fois.
- Un état corrompu provoque une erreur ; le bot ne le remplace pas silencieusement
  par un état vide. La simulation n'est pas une réinitialisation.

## Documentation

- [Architecture et règles](docs/architecture.md) : responsabilités, transitions,
  capacités et tests sans réseau.
- [Exploitation et essais réels](docs/operations.md) : workflows, persistance,
  limites de livraison et procédure d'essai borné.
- [Reprise des anciens états](docs/state-migrations.md) : compatibilité et retour arrière.
- [Référence du chantier](docs/maintenance-reference.md),
  [observations disponibles](docs/maintenance-observations.md) et
  [propositions fonctionnelles différées](docs/functional-proposals.md).
- [Bilan et vérifications](docs/maintenance-results.md).
