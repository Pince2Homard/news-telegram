# Phase 8.2.3-A — conception du transport privé Telegram

Rapport finalisé le 14 novembre 2026 — **conception soumise à revue intégrale,
aucune implémentation autorisée par ce document**.

Base analysée : `origin/main` à
`bae4ffec74ad247914e25d9017f7bf780d53bdd7`, fusion de la PR #27.
Base de publication documentaire : `939158489eb8e23abea88f53b5e191eee87d792b`,
dernier `origin/main` récupéré pour cette finalisation. Le seul commit depuis
la référence d'analyse concerne `state.json` ; les composants étudiés restent
inchangés. La publication part de cette base sans changer le checkout local
ni réécrire son fichier d'état. Le rapport initial a été sauvegardé avant édition.
Seul ce document est publié en PR ; aucun code, appel API Telegram ou paramètre
réel n'est créé ou modifié. La revue de A ne vaut pas autorisation de B ou C.

## 1. Architecture retenue pour finalisation

Ajouter ultérieurement un adaptateur privé, manuel, borné et mono-instance,
autour du routeur pur existant. Le watcher reste l'unique producteur de
l'instantané GitHub. Le canal Codex conserve sa fonction de notification ; le
dialogue privé possède sa propre destination et son propre journal.

La réception est enregistrée durablement avant tout acquittement Telegram.
La réponse est préparée une seule fois, puis chaque tentative sortante est
journalisée avant son démarrage. Une livraison ambiguë interdit le renvoi
automatique. Cette politique privilégie l'absence de doubles envois ; certaines
commandes peuvent rester sans réponse après une panne. Aucun exactly-once
distant n'est promis.

Le périmètre reste `/start`, `/help`, `/status`, `/pr`, `/ci`, `/rapports`, avec
les filtres consultatifs déjà implémentés. Ni action GitHub, arbitrage, commande
d'exécution, assistant, service ou disponibilité PC éteint n'est introduit.

Les orientations retenues par le propriétaire sont : adaptateur privé séparé,
routeur hors ligne conservé, SQLite indépendant, réception durable avant
progression de curseur, absence de renvoi automatique incertain, enrôlement
manuel numérique et expiration sans effacement des traces. Les paramètres
opérationnels détaillés ci-dessous restent soumis à la revue intégrale.

## 2. Inventaire des composants réutilisables

| Composant existant | Réutilisation proposée | Limite à conserver |
| --- | --- | --- |
| `private_router.route_message(message, config, snapshot, now)` | Les six commandes, authentification préalable, HTML et fraîcheur | Pur, aucun accès réseau/disque, contrat inchangé |
| `supervision.validate_config`, `simulate_private_identity` | Validation et comparaison d'IDs | Acceptent seulement simulation et envoi désactivé ; ce ne sont pas une preuve d'origine réseau |
| `coordination` | SHA, UUID, enveloppes, données structurées | Aucun exécuteur ; pas de confiance dans un commentaire libre |
| État du watcher : `pr_snapshots`, `threads`, `ci_snapshots`, `coordination_registry` | Instantané chargé en lecture seule par l'adaptateur | Pas de collecte GitHub à la demande, aucune écriture de cet état |
| `publication_tracking.view` et rapport structuré | Sémantique de publication connue et d'incertitude | Un reçu GitHub ne prouve pas une livraison Telegram |
| `watcher.single_instance_lock` | Verrou OS sur un fichier privé canonique | Verrou local ; ne contrôle pas un lecteur sur un autre ordinateur |
| `watcher.save_state` | Référence existante : temporaire, flush, fsync, remplacement | Ne fournit pas une transaction sur plusieurs journaux ; ne pas l'altérer |
| `telegram_once.read_external_token`, `credential_acl.verify` | Lecture externe contrôlée, chaîne ACL, contrôle de changement | Ne jamais appeler en A/B ; audit préalable de réutilisabilité en C |
| `telegram_http.TelegramHTTP` | Modèle de HTTPS direct, TLS validé, erreurs expurgées | Ne pas réutiliser cet objet pour le privé : méthodes, canal et message ponctuel verrouillés |
| `telegram_diagnostics` | Codes fixes et compteurs sans corps distant | Nouveaux codes privés séparés, bornage des étapes persistées |
| Tests des phases 8.1/8.2 | Fixtures, injection transport/temps, gardes anti-I/O | Tous conservés ; aucune dépendance aux identités réelles |

Constat : le transport actuel limite la connexion à 5 s et les opérations socket
à 15 s, sans budget total démontré. Sa réponse `result` attend un objet ;
`getUpdates` attendrait une liste. L'élargir affaiblirait la séparation existante.
Le journal confirmé 1947 reste entièrement hors du nouveau circuit, même en
récupération ou lors d'une rotation de token.

## 3. Contrats et séparation des responsabilités

Noms indicatifs pour B, sans création de modules maintenant :

- **Récepteur privé** : valider une enveloppe Update injectée, sa provenance
  technique et sa date, puis produire un reçu minimal. Aucun parsing de commande
  non authentifiée, aucun téléchargement de pièce jointe.
- **Magasin privé** : transactions de réception, curseur, réponse et tentatives.
  Il ne connaît pas le token et n'écrit jamais dans l'état du watcher.
- **Coordinateur de transport** : machine d'états bornée de lecture/réponse.
  Aucun métier dupliqué : il délègue exclusivement au routeur pour les commandes.
- **Transport privé** : interface injectable simulée en B ; adaptateur HTTPS
  réel uniquement après décision C. Sa seule destination sortante est l'ID privé
  autorisé. Aucun paramètre arbitraire fourni par un message ou par le routeur.

Le routeur reçoit une configuration de consultation validée avec ses réglages
actuels `mode=simulation`, `send_enabled=false`. Ces champs restent vrais pour
le calcul pur : son résultat indique que le routeur n'a rien envoyé. Une
configuration **distincte** du transport autoriserait la réception/envoi réel
en C. Le transport ne transforme jamais `simulation=true` en une permission.
L'activation réelle exige sa propre autorisation locale liée au SHA, aux deux
IDs privés, à l'identité numérique du bot, au répertoire et aux quotas.

Un refus `text=null` n'engendre aucune outbox. Les champs `authorized`, `sent`
et `command` du routeur ne constituent pas une preuve réseau. L'adaptateur
revalide le destinataire et le résultat avant de créer une tentative.

### Origine et schéma des messages

Un JSON fourni par l'utilisateur, un fichier ou GitHub n'est jamais admis dans
le chemin réel. Seule la réponse d'une requête HTTPS initiée vers l'hôte fixé,
avec le credential du bot vérifié, peut alimenter ce chemin. Cela authentifie
le canal technique, pas une intention humaine d'exécuter une action.

Valider `ok`, la liste, les bornes, l'absence de clés JSON dupliquées, les types
exacts (bool distinct d'int), `update_id`, puis l'unique type de contenu.
Accepter uniquement `message` textuel direct : `message_id`, `date`, `from.id`,
`from.is_bot=false`, `chat.id`, `chat.type=private`. Refuser éditions, canaux,
messages business, expéditeurs intermédiaires, transferts anciens/nouveaux,
messages automatiques identifiables et autres types. Conserver les refus déjà
implémentés, même si un champ de provenance est présent avec une valeur nulle.
Les types futurs inconnus sont refusés ; aucune projection ne doit supprimer
un champ de provenance avant le contrôle. Pas d'ouverture des liens entrants.

## 4. Identités, secrets et enrôlement manuel

Proposition à exécuter seulement sous autorisation C :

1. Préparer hors dépôt un dossier privé et une configuration désactivée. IDs
   propriétaire/chat absents = arrêt avant toute activité réseau normale.
2. Le propriétaire ouvre le bot attendu depuis sa session Telegram de confiance
   et initie lui-même une conversation. Aucun premier `/start` n'est enrôlé.
3. Une session locale spéciale d'observation, distinctement autorisée, limitée
   dans le temps et sans envoi ni consultation GitHub, pourrait recueillir un
   candidat. Une preuve locale indépendante relie la session du propriétaire
   au candidat : défi ponctuel à expiration courte, affiché localement et saisi
   dans son application, jamais publié. Le défi ne remplace pas l'approbation
   manuelle des deux IDs et de la provenance directe.
4. Afficher les candidats seulement sur le poste privé. Le propriétaire valide
   explicitement les IDs numériques et la session attendue ; s'il existe une
   ambiguïté ou plusieurs candidats, abandonner. Ne pas publier les IDs, captures
   ou transcriptions dans GitHub. Aucun bot tiers de découverte d'ID n'est requis.
5. Enregistrer manuellement la paire validée, l'ID numérique attendu du bot et
   la version d'identité dans le stockage privé. Garder l'activation désarmée.
   La vérification future du bot doit comparer l'ID épinglé ; le pseudonyme
   seul ne suffit pas. Aucun appel réseau d'enrôlement n'est autorisé en A/B.

Le credential dédié reste externe au dépôt et séparé du bot News Telegram.
Pas de repli sur ses variables, secrets ou chat de production. Réutiliser les
contrôles ACL existants sans les affaiblir, sur le credential et ses parents ;
protéger également config, base, fichiers auxiliaires et verrou. Les IDs privés
sont des données personnelles, pas des secrets d'authentification suffisants.
Toute modification de destination, bot, credential ou génération d'identité
désarme le circuit ; une ancienne outbox ne peut pas changer de destinataire.

## 5. Réception, curseur et acquittement

### Contraintes documentaires

Polling et webhook sont exclusifs. Un appel avec un offset supérieur à un
update_id confirme cette mise à jour. Les événements ne sont conservés que
24 heures ; après au moins une semaine sans nouvel événement, le prochain ID
peut être choisi aléatoirement. `allowed_updates` ne filtre pas rétroactivement
le stock antérieur. [Documentation Bot API](https://core.telegram.org/bots/api#getupdates).

### Flux normal proposé

1. Prendre le verrou privé pour toute la session, vérifier ACL, identité du
   magasin, intégrité, quotas, horloge et armement. Aucun appel si le verrou échoue.
2. En C seulement, vérifier l'absence de webhook par une méthode de lecture
   autorisée. S'il existe, arrêter : ni suppression automatique ni purge de
   mises à jour. La propriété exclusive du bot doit aussi être attestée humainement.
3. Charger `next_offset` durable. Une seule requête à la fois, avec paramètres
   explicites et stables, filtre `message`, limite proposée de 20 événements.
   Ne jamais utiliser un offset négatif ou `drop_pending_updates` pour démarrer.
4. Valider tout le lot. En une transaction, enregistrer chaque événement reçu
   ou son rejet terminal minimal, les doublons identifiés et le nouvel offset
   candidat `max(update_id)+1`. Ne pas supposer que tous les entiers existent.
5. Attendre le succès du commit durable. Alors seulement un appel suivant peut
   porter cet offset. Il confirme des événements déjà enregistrés, même si
   leur réponse n'est pas encore traitée. Le curseur n'attend pas l'envoi.
6. En cas d'échec disque ou de commit douteux, suspendre le réseau. Relire et
   vérifier le magasin sous verrou avant toute décision ; aucun saut de curseur.

### Transaction du lot et contraintes d'unicité

Définir `O` comme l'offset durable utilisé au lancement de la requête. Aucune
transaction SQLite ne reste ouverte pendant une opération réseau. Après
validation du lot, ouvrir une transaction d'écriture `BEGIN IMMEDIATE`, puis :

1. Vérifier que l'époque, la génération et `O` correspondent toujours au magasin.
2. Insérer chaque reçu sous contrainte UNIQUE `(bot_id, époque, update_id)`.
   Un conflit identique est un doublon, sans réinsertion ni reroutage ; un conflit
   de contenu sur un message admis provoque rollback et suspension. Les reçus
   de refus ne conservent pas le contenu tiers pour faire cette comparaison.
3. Pour les messages admissibles, réserver la clé UNIQUE
   `(bot_id, chat_id, message_id)`. La réservation et la disposition initiale
   sont enregistrées ensemble. Un doublon de cette clé ne crée aucune outbox.
4. Enregistrer les dispositions reçue, rejetée ou historique/expirée, puis
   `next_offset = max(O, max(update_id du lot)+1)` dans la même transaction,
   uniquement pour l'époque normale validée. Un lot vide conserve `O`.
5. Commit durable ; en cas d'erreur, ne publier aucun nouvel offset en mémoire
   comme acquis. Relire le magasin avant tout nouvel appel.

Les IDs non contigus croissants sont admis ; aucun reçu artificiel n'est créé
pour combler les trous. Un lot hors ordre, un ID inférieur inattendu ou un ID
réutilisé avec contenu incompatible suspend le flux au lieu de calculer un
nouvel offset. Les doublons connus peuvent être reconnus sans travail métier.
Le changement d'époque n'est jamais une conséquence automatique de `max`.

Le commit du curseur est une **permission d'acquitter**, pas la preuve que
Telegram l'a reçu. Un champ éventuel d'acquittement observé reste diagnostique ;
le curseur durable et les reçus font autorité pour la reprise.

| Point du crash | État durable et reprise autorisée |
| --- | --- |
| Après réception, avant transaction | Aucun reçu nouveau ; même offset, lot susceptible d'être répété |
| Pendant transaction, avant commit | Lot et curseur rollbackés ensemble ; ne pas utiliser l'offset candidat |
| Après commit, avant appel suivant | Lot sauvegardé ; prochain appel peut utiliser l'offset durable |
| Appel suivant accepté, réponse réseau perdue | Les événements confirmés étaient déjà sauvegardés ; reprendre le même offset durable |
| Après routage pur, avant commit outbox | Recalcul permis si encore frais ; aucune tentative sortante n'existe |
| Après commit outbox ou intention d'envoi | Réutiliser les états persistés ; jamais créer une seconde outbox ; intention interrompue = incertaine |

### Exclusivité locale et lecteurs distants

Le verrou OS est détenu durant toute la session, sur un emplacement canonique
lié au bot et au magasin, avec contrôle des reparse points et des ACL. Il
empêche les lecteurs coopérants du même poste, y compris deux sessions Windows
utilisant ce même fichier. Il n'est ni une élection distribuée ni une preuve
qu'aucun autre ordinateur n'utilise le token. Un autre dossier ne doit pas
constituer un moyen de contourner l'identité du magasin ou le verrou.

Un webhook déjà configuré ou un conflit de réception Telegram provoque une
suspension durable et la sortie désarmée. Aucun deleteWebhook, changement de
filtre compensatoire, purge, rotation de token ou relancement concurrent n'est
effectué automatiquement. Le propriétaire doit identifier et arrêter l'autre
consommateur avant une reprise explicitement autorisée. Certains lecteurs
distants peuvent acquitter entre deux appels sans produire de conflit visible :
l'exclusivité doit donc aussi être organisée humainement. L'absence d'erreur
409 n'est jamais présentée comme une preuve de continuité ou d'exclusivité.

Les refus stockent un reçu sans texte ni identité tierce : update_id, code fixe,
date de réception et disposition. Les messages authentifiés stockent seulement
la projection nécessaire à la commande. Un update_id valide avec contenu
invalide peut être rejeté durablement puis confirmé. Sans ID exploitable, ou si
le lot est structurellement ambigu, ne pas progresser : arrêt diagnostiqué,
sans purge automatique. Un lot entier est durable ou ne l'est pas.

Un timeout n'autorise pas deux appels chevauchants. Fermer l'appel précédent,
ignorer tout résultat arrivé après son échéance/génération, puis reprendre
depuis le curseur durable. Si une requête a déjà confirmé des événements,
ceux-ci étaient nécessairement enregistrés avant son lancement.

### Première activation, stock ancien et longue inactivité

En C, fixer un instant d'activation approuvé, une session d'enrôlement et une
tolérance d'horloge. Première lecture sans offset : classifier durablement le
stock préexistant comme historique, sans le router ni y répondre. Les types
exclus par le filtre demandent aussi un reçu s'ils sont effectivement retournés.
Aucun ancien message ne devient une nouvelle commande par redémarrage.

### Portée exacte de l'expiration à 120 secondes

La fenêtre est attachée au `message.date` original, pas au moment où le processus
redémarre ni à la réception locale. À âge **strictement supérieur à 120 s**,
la commande n'est plus admissible à un nouveau traitement automatique. À 120 s
exactes elle reste admissible si tous les autres contrôles passent. Une date
future de plus de 30 s ou une horloge incohérente suspend le traitement ; une
petite avance tolérée ne prolonge pas la fenêtre au-delà de `message.date+120 s`.

Vérifier cette échéance avant routage, puis avant toute tentative sortante.
La réponse préparée hérite de la même échéance absolue : sa préparation ne
redonne pas 120 s. Une commande expirée non routée reçoit une disposition
terminale `expired`; une outbox encore `prepared` devient `expired` sans envoi.
Si la fenêtre se ferme pendant l'appel déjà commencé, ne pas prétendre annuler
une livraison possible : enregistrer son résultat confirmé ou incertain. Les
états `in_flight`, `confirmed`, `rejected`, `uncertain` ne sont jamais effacés
ou remis à zéro par l'expiration.

**Ce qui n'expire pas avec ces 120 s :** le reçu durable, la réservation du
message, l'offset, les clés de déduplication, les tombstones et les intentions
d'envoi. La purge de payload obéit à une politique de rétention séparée.
Une commande trop ancienne est enregistrée avant acquittement, sans réponse
automatique, y compris sans message « expiré » vers Telegram. Ce délai est
distinct des 3600 s de fraîcheur de supervision, conservés.

### Reprise après interruption : état de supervision explicite

Conserver dans le magasin un état local de continuité (`normal`,
`gap_possible`, `recovery_required`) et une cause fixe. « normal » signifie
absence d'incident connu, jamais preuve absolue d'exhaustivité. Cet indicateur
sera consultable localement ; il n'impose aucun changement au routeur dans A.

| Durée ou situation | Reprise proposée, toujours sous les limites de la session autorisée |
| --- | --- |
| 3 heures | Valider magasin/verrou/horloge puis reprendre le curseur durable. Enregistrer les événements retournés ; ceux âgés de plus de 120 s restent expirés, sans réponse. Une commande réellement récente peut être traitée. Ne pas annoncer que tout a été conservé si un incident ou lecteur concurrent est suspecté. |
| 24 heures exactement ou davantage | Positionner `gap_possible` avant reprise : une perte devient possible à la frontière de conservation et selon les délais effectifs. Les reçus locaux restent valides. Les événements encore retournés sont enregistrés et classés ; aucun événement absent n'est inventé. Afficher localement « continuité non garantie, pertes potentielles ». |
| Plusieurs jours, moins d'une semaine | Même état de perte possible ; aucune récupération complète revendiquée. Reprendre seulement si l'époque et l'ordre restent cohérents ; stock ancien expiré et outbox ancienne non envoyée. |
| Une semaine sans activité d'updates connue, durée inconnue ou ID incompatible | `recovery_required`, pas de réutilisation aveugle de l'ancien offset. Revue manuelle de l'époque avant toute réception normale ou sortie. |

La durée se mesure depuis la dernière activité de réception fiable et les
horodatages persistés, pas simplement depuis le dernier démarrage. En cas de
doute, choisir l'état le plus prudent. Un nouveau lot réussi ne supprime pas
la trace d'une lacune ; une clôture humaine consignée ne recrée pas les données
perdues. Aucun journal local ne prolonge la conservation du serveur Telegram.

Après interruption atteignant 24 h, les événements absents ne sont pas
reconstituables. Après une
semaine d'inactivité possible, ne pas réutiliser aveuglément le vieux high-water
mark : passer en récupération manuelle, lecture bornée sans offset et sans
réponse. Un ID inattendu ou réutilisé provoque la même suspension. Une nouvelle
époque de curseur exige une décision explicite et une trace durable ; garder
les anciennes tombstones et la déduplication par message. Aucun reset automatique
à zéro, nouveau dossier ou effacement du journal pour résoudre le problème.
Dans cette procédure exceptionnelle, chaque lot d'observation et ses dispositions
doivent aussi être commités avant un offset qui le confirmerait. La nouvelle
époque est créée transactionnellement avec ces reçus ; elle ne reprend pas le
`max` de l'ancienne. La contrainte par message reste indépendante de l'époque.

## 6. Persistance et anti-doublons

Orientation retenue pour finalisation : une base SQLite locale dédiée, via la
bibliothèque standard, plutôt que plusieurs fichiers JSON à synchroniser.
Justification : l'inbox et son offset, puis l'état de traitement et l'outbox,
doivent partager une transaction. Le mécanisme JSON actuel est conservé pour
ses usages existants. Répertoire proposé : sous-dossier `telegram-private`
hors dépôt de NewsTelegramWatcher ; chemin logique proposé :
`%LOCALAPPDATA%\NewsTelegramWatcher\telegram-private\private-transport.sqlite3`.
Ce chemin ne contient aucun nom de compte réel. Il ne désigne ni le `state.json`
du dépôt, ni l'état du watcher, ni le dossier `telegram-once` du journal 1947.
L'emplacement canonique doit être approuvé, sans stockage réseau, synchronisation
cloud ou lien de réanalyse. Il n'est ni créé ni inspecté lors de cette publication.

Configuration proposée : journal rollback DELETE, synchronous EXTRA, foreign_keys
activées, délai de verrouillage court ; lire les valeurs effectives et tester
sur le Windows cible. EXTRA renforce la synchronisation du journal DELETE ;
la durabilité dépend encore du système de fichiers et du matériel.
[Référence SQLite](https://www.sqlite.org/pragma.html#pragma_synchronous).

Le processus privé est seul écrivain ; les transactions utilisent
`BEGIN IMMEDIATE` avec rollback intégral sur échec. Vérifier au démarrage les
PRAGMA effectivement appliqués, le schéma et l'intégrité. Pas de journal OFF,
de synchronous OFF/NORMAL ou de changement de mode pour contourner un blocage.
Un commit réussi est une exigence applicative ; la résistance aux pannes du
matériel et du VFS Windows reste à qualifier en B, sans garantie universelle.

### Permissions et fichiers annexes

Préparer un dossier à DACL restrictive avant toute création de base : compte
Windows dédié/exécutant, SYSTEM et Administrateurs de confiance uniquement.
Vérifier propriétaire, héritage, chaîne des parents, droits de remplacement et
absence de reparse point. Refuser une ACL non vérifiable ; ne pas la corriger
automatiquement. Ne pas élargir l'exception TrustedInstaller existante.

La protection s'applique à la base, `-journal`, verrou, configuration, sauvegardes,
trace d'initialisation et éventuels fichiers temporaires. DELETE évite le besoin
normal de `-wal`/`-shm` ; leur présence inattendue exige une vérification du mode,
pas leur suppression. Les fichiers créés doivent hériter d'une DACL sûre dès
leur naissance. Exiger des temporaires SQLite en mémoire lorsque possible et
qualifier le comportement de la bibliothèque sous Windows ; aucune copie brute
dans un répertoire temporaire aux permissions plus larges. Toute impossibilité
de protéger ces fichiers bloque la préparation de C.

### Sauvegarde et restauration

Préférer un mécanisme de sauvegarde SQLite cohérent, sous verrou et réseau
suspendu ; une copie isolée du fichier principal pendant une transaction est
interdite. Chiffrer/protéger la sauvegarde selon la politique du poste, vérifier
son intégrité et sa version, conserver son bot, époque, génération et curseur.
Proposition : deux sauvegardes cohérentes maximum, dans un espace protégé avec
quota propre ; rotation seulement après vérification de la nouvelle copie.
En cas de corruption, préserver une copie de diagnostic protégée sans effacer
le journal de récupération. Ne jamais supprimer manuellement un hot journal.

Toute restauration démarre désarmée. Comparer reçus, tombstones et tentatives
à la trace indépendante ; résoudre les envois potentiellement déjà acceptés
avant de réautoriser la sortie. Une sauvegarde ancienne ne permet pas de
reconstituer les événements déjà acquittés et absents : consigner la lacune,
pas de rejeu automatique. Migration de schéma et récupération nécessitent une
procédure relue ; une erreur n'autorise pas la création d'une base vide.

| Ensemble logique | Données minimales et contraintes |
| --- | --- |
| Métadonnées | version de schéma, UUID magasin, bot attendu, génération d'identité, époque de réception, offset, dernière activité, armement |
| Inbox | unicité `(bot, époque, update_id)`, projection authentifiée ou reçu de rejet, horodatage, disposition durable |
| Messages traités | unicité `(bot, chat privé, message_id)` ; empêche qu'un même message admis soit routé via deux Updates/époques |
| Outbox | un seul objet par message admis, ID local, texte HTML final figé, empreinte, destination/génération épinglées, création/expiration, état |
| Tentatives | numéro, outbox, intention durable, état, heure, code expurgé, éventuel message_id retourné |
| Événements d'exploitation | démarrage/arrêt, suspension, décision manuelle, compteur ; aucun corps ou token |

À réception répétée, un message déjà traité retrouve son résultat persistant ;
il ne recrée ni réponse ni tentative. Une même clé associée à un contenu
différent est un conflit bloquant, pas un nouvel ordre. Les empreintes ne sont
pas des preuves cryptographiques d'origine. Ne stocker aucun corps non autorisé.

Transaction de préparation : réserver le message, lire un instantané du watcher
sans l'écrire, appeler le routeur avec le temps explicite, puis enregistrer
réponse figée + état traité ensemble. Un crash avant commit permet ce calcul
pur à nouveau ; un crash après commit ne recalcule pas un texte différent.
La copie du watcher doit être cohérente ; échec de lecture = instantané absent,
jamais un lancement de collecte. Réauthentifier la génération au moment d'envoyer.

Prévoir chiffrement du disque ou du profil selon le besoin ; SQLite seul ne
chiffre pas. Les logs d'exploitation ne contiennent que des codes fixes et
des identifiants locaux non personnels. Les payloads autorisés expirés peuvent
être purgés après 7 jours, les empreintes/tombstones restent conservées. Ne
jamais purger un état ambigu non résolu. Proposition de seuils d'arrêt : 1000
messages non terminaux, 100000 tombstones ou 128 Mio de magasin ; pas d'éviction
silencieuse ni de suppression automatique pour libérer de la place.
Cette rétention est bornée par arrêt, pas par effacement des preuves anti-doublons :
les payloads terminaux éligibles peuvent être retirés après 7 jours, mais les
clés/empreintes restent jusqu'à une maintenance manuelle approuvée. Au seuil,
plus aucune nouvelle réception normale ou tentative ne démarre. Dimensionner
aussi les journaux auxiliaires et sauvegardes : proposition de plafond total
512 Mio, dont 128 Mio de base ; dépassement ou espace disque insuffisant = arrêt.
Les états ambigus non résolus ne sont jamais supprimés par cette maintenance.

Les diagnostics utilisent une liste fermée de codes et compteurs, sans URL API
contenant le token, corps distant, texte de commande, IDs personnels ou chemin
absolu. Les traces HTTP/debug, exceptions brutes et dumps SQL avec paramètres
sont interdits. Les fichiers de logs sont également sous ACL, avec rotation
bornée proposée à 5 fichiers de 1 Mio ; leur suppression ne touche pas aux
preuves transactionnelles. Une suppression logique de payload n'est pas une
garantie d'effacement physique des pages, journaux ou anciennes sauvegardes.

## 7. Sortie privée et reprise

Destination numérique positive exactement égale au chat privé enrôlé, jamais
au canal, au champ libre d'un message ou à une valeur de retour du routeur.
Une seule opération autorisée : réponse textuelle `sendMessage`, HTML validé,
sans médias, boutons, transfert, edit, suppression ou paramètre business.
Conserver la borne existante de 2048 caractères HTML et ne pas fractionner en
plusieurs envois. Désactiver les aperçus de liens et ne pas suivre ces liens.

Machine d'états proposée :

`prepared → in_flight → confirmed | rejected | uncertain`

`prepared → expired | cancelled` reste possible avant toute tentative.
Le passage à `in_flight`, son numéro de tentative et la réservation du quota
sont commités **avant** l'appel. Au redémarrage, tout `in_flight` devient
`uncertain` et ne peut pas revenir automatiquement à `prepared`.

| État durable | Sens et actions permises |
| --- | --- |
| `prepared` | Réponse figée, destination et échéance épinglées, aucune tentative ; admissible à une première tentative si fraîche et autorisée |
| `in_flight` | Intention et quota déjà commités ; acceptation distante possible, même sans réponse locale |
| `confirmed` | Succès et message_id cohérents enregistrés durablement ; terminal sans renvoi |
| `rejected` | Rejet explicite cohérent enregistré ; terminal, aucun retry automatique dans cette première version proposée |
| `uncertain` | Acceptation non déterminable ou confirmation non durable ; aucun retry, y compris après expiration/redémarrage |
| `expired` / `cancelled` | Première tentative non commencée ; conserver la trace sans envoi |

Scénario de doublon : Telegram accepte la tentative, la connexion tombe avant
le retour HTTP et l'état local reste `in_flight`. Repartir de cet état comme
s'il s'agissait d'un échec certain pourrait envoyer une seconde réponse déjà
livrée une première fois. Le même risque existe si le retour est reçu mais son
commit échoue. Même un arrêt juste avant l'émission ne prouve pas au redémarrage
où l'exécution s'est interrompue : classification conservatrice `uncertain`.
La clé d'unicité locale empêche les doubles intentions applicatives, sans rendre
la livraison distante exactly-once. Une nouvelle commande du propriétaire est
une nouvelle demande, jamais une preuve de non-livraison de l'ancienne.

Pour confirmer : HTTP/API cohérents, résultat de message valide, message_id
positif, destination privée exacte, texte correspondant après interprétation
des entités HTML. Ne pas comparer naïvement le texte Telegram sans balisage au
payload HTML échappé. Définir/tester en B la projection en texte et les entités
autorisées ; une divergence rend la tentative incertaine.

`sendMessage` limite le texte à 4096 caractères après parsing des entités ;
`retry_after` indique un délai après limitation. [Contrat Bot API](https://core.telegram.org/bots/api#sendmessage).
Le choix conservateur 2048 reste inférieur à cette limite.

| Situation | Décision proposée |
| --- | --- |
| DNS, TLS, connexion ou timeout en réception | Backoff borné avec jitter, reprise du curseur durable, pas de lecteur parallèle |
| HTTP 401/403, identité bot inattendue, webhook présent | Arrêt désarmé ; diagnostic local, aucune correction implicite |
| HTTP 409 / suspicion de lecteur concurrent | Arrêt ; contrôle humain des autres clients, aucune compétition d'offset |
| Réponse invalide, trop grande ou lot sans ID fiable | Quarantaine minimale et arrêt sans avancer |
| Réponse API négative cohérente à un envoi | `rejected`, code fixe, pas de renvoi automatique dans la première version |
| 429 cohérent | Enregistrer retry_after et une échéance durable ; suspendre le bot pendant ce délai, ne pas réessayer la réponse rejetée automatiquement |
| Réponse perdue après acceptation, 5xx, JSON tronqué, arrêt brutal après intention | `uncertain`, jamais de renvoi automatique |
| Succès reçu puis échec de sauvegarde | Arrêt ; intention reste ambiguë, aucun renvoi |
| Corruption, schéma inconnu, DB absente après initialisation, ACL modifiée | Arrêt fermé ; jamais de réinitialisation automatique |
| Verrou occupé | Quitter sans appel ; ne pas supprimer le fichier pour contourner le verrou |
| Disque plein / seuil atteint | Suspendre réception et sortie ; pas de nouvel offset non sauvegardé |

Reprise d'un envoi incertain : examen local, consultation manuelle éventuelle
du chat par le propriétaire, puis classement sans envoi. Bot API ne fournit
pas ici de clé d'idempotence client permettant de prouver exactly-once. Si le
propriétaire veut une réponse, il peut émettre une **nouvelle** commande ; le
renvoi ne doit jamais être maquillé en reprise garantie de l'ancienne tentative.

Débit proposé : un envoi au plus toutes les 2 s, sans rafale, et 10 réponses
par minute, quotas partagés par la session et persistés pour le redémarrage.
La FAQ recommande de ne pas dépasser un message par seconde dans un chat.
[FAQ Telegram](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this).
Backoff de réception proposé : 2, 4, 8… jusqu'à 60 s ; cinq échecs consécutifs
suspendent la session. Ne pas dormir puis poursuivre au-delà de son autorisation.

Bornes proposées C : long poll 10 s, connexion/TLS 5 s, lecture 15 s, budget
total 25 s pour réception ; budget total 20 s pour sortie. Le futur adaptateur
doit démontrer une interruption effective y compris DNS/lecture lente ; un simple
timeout socket ne suffit pas. Si nécessaire, isoler l'appel dans un worker
borné sans accès au magasin, contrôlé par le processus qui détient le verrou.
Un worker interrompu après une intention d'envoi entraîne `uncertain`. Ce point
doit être résolu et testé avant toute proposition C, sans modifier TelegramHTTP.

## 8. Invariants et tests d'acceptation pour B

1. Aucune requête sans verrou, config valide, génération cohérente et magasin sain.
2. Aucun offset envoyé ne confirme une mise à jour reçue non durablement classée.
3. Aucun snapshot consulté ni réponse divulguée avant authentification.
4. Aucune tentative sans intention durable préalable ; aucune ambiguïté rejouée.
5. Aucun message traité ne recrée une outbox, même après changement d'époque.
6. Aucun corps/token/ID personnel dans les logs ou livrables GitHub.
7. Aucune destination sortante autre que la paire privée approuvée.
8. Aucun accès en écriture à la production, au watcher ou au journal 1947.

Tests B exclusivement injectés, temps et identités synthétiques :

- Six commandes et filtres, non-régression complète du routeur, destination
  canal/groupe/username refusée, provenance business/forward/anonyme rejetée.
- Lots vides, doublons, trous d'IDs, ordre invalide, collision de contenu,
  types inconnus, ID absent/bool, clés JSON dupliquées, tailles excessives.
- Arrêt injecté avant/après chaque commit, avant/après acquittement, après
  intention d'envoi, acceptation simulée suivie de timeout, succès non sauvegardé.
- Prouver l'invariant curseur/inbox et l'unicité outbox par génération aléatoire
  de séquences crash/redémarrage, pas seulement par tests du chemin heureux.
- Deux processus sur un verrou local de test, réponse tardive après échéance,
  tentative de redémarrage avec dossier alternatif, base verrouillée ou absente.
- Première activation avec stock ancien ; événements encore retournés malgré
  le filtre ; limites 120 s/120 s dépassées, interruptions 3 h, 24 h et plusieurs
  jours, puis 7 jours ; ID aléatoire plus petit, horloge
  reculant, expiration des commandes et des réponses en attente.
- Erreurs HTTP/API, Retry-After, TLS refusé, proxy/redirect interdit, lecture
  lente, budget total et saturation des quotas, redémarrage pendant cooldown.
- Corruption, rollback après crash, disque plein, échec de commit et permissions
  Windows ; chemins reparse/héritage ACL/fichiers auxiliaires non sûrs refusés.
- Texte HTML avec caractères spéciaux/emoji et aller-retour des entités, payload
  trop long, aucune entité tronquée, retour Telegram mal associé refusé.
- Gardes interdisant réseau réel, credential réel, production et lancement
  d'assistant ; magasin de test dans un temporaire, entrées du routeur immuables.

Exécuter ensuite suite Python complète, Ruff, compileall, pip-audit et CI sur
le SHA publié de B. Aucun de ces résultats n'est revendiqué pour une
implémentation inexistante en A.

## 9. Récupération et risques résiduels

Un diagnostic d'intégrité ne justifie jamais un effacement. Stopper, conserver
une copie locale protégée, identifier la dernière transaction fiable et faire
approuver la récupération. Une restauration ancienne peut faire réapparaître
des messages déjà confirmés/envoyés : magasin restauré = sortie désarmée,
réconciliation manuelle obligatoire. Conserver l'identité du magasin et une
trace indépendante protégée d'initialisation pour distinguer premier démarrage
et perte de base ; une trace manquante ne prouve pas une première installation.

Risques restant acceptés ou à examiner : compte Telegram/token/poste compromis,
administrateurs locaux de confiance, erreurs d'enrôlement, lecteur distant
invisible au verrou local, dysfonctionnement matériel malgré fsync, perte
d'événements pendant une absence longue, messages sans réponse plutôt que
doublons, croissance des tombstones, journaux auxiliaires et sauvegardes privés.
Le filtrage d'updates concerne tout le bot : son usage par un autre client doit
être exclu. Un worker réseau n'établit pas une preuve de non-livraison.

Le PC doit être actif pendant la session. Rien ici ne résout la Phase 8.4.
Une nouvelle commande ne peut pas autoriser une fusion, un arbitrage 8.3 ou une
gestion d'effort 8.5. Le journal 1947 n'est jamais un outil de récupération privé.

## 10. Livraisons et arbitrages humains

| Livraison proposée | Contenu | Porte de validation |
| --- | --- | --- |
| A — ce rapport | Analyse et contrat de sécurité, aucune implémentation | Validation du propriétaire avant B |
| B1 — PR hors ligne | Schéma transactionnel, reçus, curseur, unicité, crashs et verrou | Revue des invariants, aucune méthode réseau réelle |
| B2 — PR hors ligne | Outbox simulée, budgets, quotas, reprise, raccord au routeur | Suite complète et CI ; toujours aucun credential réel |
| C — préparation | Adaptateur HTTPS privé séparé, procédure et plan borné | Autorisation spécifique avant accès credential/API |
| Essai C éventuel | Session manuelle avec échéance, quotas et arrêt automatique | Approbation du SHA, IDs privés locaux, méthodes, texte/commandes et budget exacts |

Pour un premier essai, proposer au maximum 5 minutes, 20 requêtes de réception
et 6 réponses, arrêt au premier dépassement, rejet grave ou ambiguïté. Les
vérifications d'identité/webhook disposent d'un budget séparé explicitement
approuvé ; une session ne s'étend pas automatiquement pour finir un backlog.
Préparer le plan concret et sa revue avant toute demande d'activation.

**Décisions favorables retenues pour la revue complète :** adaptateur indépendant,
routeur pur conservé, SQLite dédié, reçu durable avant offset, aucun renvoi
incertain, enrôlement manuel numérique, expiration sans perte de déduplication.

**Paramètres et questions à clore avant autorisation séparée de B :**

1. Approuver les paramètres SQLite/ACL, le chemin logique et les seuils d'espace,
   puis qualifier la durabilité sur le Windows cible en simulation B.
2. Confirmer le choix additionnel de ne pas réessayer automatiquement un rejet
   explicite, au-delà de l'interdiction déjà retenue des renvois incertains.
3. Valider le seuil exact de 120 s, la tolérance future de 30 s, les quotas et
   l'état de supervision après interruption ; formaliser la réconciliation d'époque.
4. Valider rétention, quotas des sauvegardes et procédure de restauration
   désarmée ; aucune maintenance ne doit effacer silencieusement la déduplication.
5. Relire la preuve locale d'enrôlement et la procédure d'exclusivité du bot ;
   aucun ID privé réel n'est demandé ou publié maintenant.
6. Arrêter le moyen de borner effectivement DNS et les appels réseau avant C ;
   les essais B utilisent seulement des transports et horloges simulés.

La publication de ce rapport ne vaut pas autorisation de développement. Le
travail s'arrête à la PR documentaire A. Aucune PR B/C, fusion, configuration
réelle ou activation n'est engagée.
