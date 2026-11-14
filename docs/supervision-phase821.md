# Phase 8.2.1 — routeur privé hors ligne

Cette phase ajoute uniquement `/start`, `/help` et `/status` en simulation.
Le module `tools.windows_watcher.private_router` n'est appelé ni par le watcher,
ni par son installation Windows, ni par un transport Telegram. Aucun service,
polling, webhook ou assistant n'est lancé. La PR ne doit pas être fusionnée ou
activée dans le cadre de cette autorisation.

## Contrat pur

`route_message(message, config, snapshot, now)` reçoit des objets en mémoire.
`message` est un message simulé dont le sous-ensemble requis est `from.id`,
`from.is_bot`, `chat.id`, `chat.type`, `text`. Les IDs doivent être des entiers
exacts et correspondre aux deux IDs privés configurés. La configuration est
validée par `supervision.validate_config` et la comparaison réutilise
`supervision.simulate_private_identity` : mode simulation, envoi désactivé,
aucun repli sur un nom public, une variable d'environnement ou le premier
utilisateur de `/start`. Les IDs privés absents bloquent tout accès.

L'authentification est séparée du traitement métier. Tout refus retourne le même
objet, avec `authorized=false`, `code=denied`, `text=null`, sans destination ni
identité. Même une indication de transfert/intermédiaire nulle ou fausse bloque
le message. Les anciens champs de transfert, `sender_chat`, `via_bot` et les
champs d'intermédiation business identifiables sont couverts. Cette comparaison
de fixtures ne prouve jamais l'origine d'un vrai message Telegram.

Seules les trois formes exactes sont acceptées, sans arguments, espaces ajoutés
ou suffixe `@bot`. Après authentification, les autres textes produisent une aide
générique sans réémission de l'entrée. Un texte absent, vide, non textuel ou de
plus de 256 caractères est invalide.

Le résultat contient `simulation=true`, `sent=false`, `authorized`, `code`,
`command`, `text`, `parse_mode=HTML`. Le texte est échappé (`&`, `<`, `>`,
guillemets), puis sa taille est contrôlée à 2048 caractères maximum, y compris
les entités HTML. Un dépassement produit un message fixe plutôt qu'une entité
tronquée. Aucun texte libre de l'instantané n'est exposé.

## Lecture de l'instantané existant

L'appelant fournit l'état du watcher en mémoire : `initialized` booléen,
`last_poll_at` date ISO avec fuseau ou absent avant initialisation, `inbox`
liste de 1500 événements maximum (limites existantes du watcher), et le champ
optionnel `next_poll_at` epoch fini positif ou nul. Les autres champs sont
ignorés : aucune consultation du registre GitHub ou des statuts CI/Telegram.
`now` est un `datetime` avec fuseau explicite. Aucun accès à l'horloge système.

`/status` présente la dernière date normalisée UTC, l'âge calculé et l'état
d'initialisation connu. Il ne prétend pas que le processus est en cours
d'exécution. Le seuil de fraîcheur fixe est de 3600 secondes incluses ; il
n'affecte aucune cadence existante. Au-delà, l'état actuel est déclaré
indisponible et l'état historique reste explicitement historique. Une date
future, une initialisation contradictoire, une date sans fuseau ou des champs
consultés malformés rendent l'état actuel indisponible. Ce contrôle porte sur
la projection consultée, pas sur l'ensemble du schéma persistant du watcher.

Les anomalies déjà présentes dans `inbox[].protocol_error` sont regroupées en
libellés fixes ; les codes inconnus deviennent une anomalie générique. Un
`next_poll_at` futur est signalé comme délai enregistré. Aucune analyse des
corps, URLs ou secrets n'est effectuée. L'absence d'anomalie dans ces champs
n'est pas une garantie de santé globale. Un instantané structurellement
invalide reçoit un diagnostic générique plutôt qu'un affichage partiel fiable
en apparence.

## Validation sans production

Depuis le dépôt de développement, avec l'environnement Python de développement :

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m compileall -q poster.py cadence_simulator.py newsbot tools tests
.\.venv\Scripts\python.exe -m pip_audit -r requirements.txt -r requirements-dev.txt --progress-spinner off
```

Les tests de `tests/test_private_router.py` sont des simulations en mémoire,
avec IDs synthétiques seulement. Ils couvrent les commandes, refus uniformes,
champs manquants/types incorrects, provenance, absence de configuration, limites
de fraîcheur, contradictions, anomalies, échappement, bornage, absence de
mutation et appels interdits (fichiers, sockets, HTTP, processus, environnement,
horloge). Ils vérifient également l'absence de raccordement aux points d'entrée
existants. L'audit de dépendances utilise le réseau de la base de vulnérabilités,
jamais Telegram ; ce n'est pas un appel du routeur.

## Protections et suite

Aucun token ou identifiant privé réel n'est demandé ou lu. Le journal durable du
message 1947, `state.json`, les workflows, sources, FIFO, cadences et installation
Windows restent intacts. Aucun fichier existant n'est modifié par cette phase.
Retour arrière : abandonner la branche ou retirer les trois fichiers ajoutés ;
aucune migration ou restauration de données de production n'est nécessaire.

Risques résiduels : fixtures falsifiables, instantané dépendant de son producteur,
authentification réseau non implémentée, absence de déduplication/rate limit de
transport et de preuve d'activité actuelle du watcher. Ces limites ne sont pas
des autorisations d'activation. Prochaine étape recommandée : **8.2.2**, consultation
du registre GitHub, après autorisation distincte. Les premiers échanges privés
réels restent en 8.2.3 ; aucun arbitrage ou gestionnaire d'effort n'est développé.
