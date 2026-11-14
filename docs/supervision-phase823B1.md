# Phase 8.2.3-B1 — persistance transactionnelle privée simulée

Base : `b7cf175053a83afbbd06b2af2bb8b1ecb135c86d`, dernier origin/main récupéré
après fusion de la conception A. Le checkout initial et son rapport local sont
préservés ; le développement utilise un worktree isolé. Aucun état de production
n'est copié dans les fixtures ni modifié.

## Périmètre et utilisation

`tools.windows_watcher.private_store.SimulationStore` reçoit un chemin de fixture,
la configuration de supervision simulée, l'identité synthétique du bot et une
heure epoch explicite. Il ne découvre aucun emplacement, credential ou variable
d'environnement. Il n'a ni mode réel, ni CLI, ni transport, ni routeur métier,
ni outbox. Aucun point d'entrée du watcher ou de l'installation ne l'appelle.

```python
# Uniquement dans un TemporaryDirectory de test ; les IDs sont synthétiques.
config = dict(mode="simulation", send_enabled=False,
              notifications_channel_id=None, owner_user_id=12345,
              arbitration_private_chat_id=12345)
with SimulationStore(fixture_path, config, 54321, now, create=True) as session:
    result = session.receive_batch(simulated_updates, expected_offset=0, now=now)
    # result n'est disponible qu'après le COMMIT ; il n'acquitte rien à distance.
```

`create=True` exige un dossier inexistant, crée un marqueur d'identité et une
base `fixtures.sqlite3`. Un dossier existant, même vide ou partiellement
initialisé, n'est jamais recréé. `create=False` exige marqueur et base existants,
avec liaison exacte au bot, au propriétaire et au chat. Les dossiers nommés de
production connus (NewsTelegramWatcher, telegram-once, credentials, .git) sont
refusés avant inspection. Cela ne prouve pas qu'un chemin arbitraire est une
fixture : l'appelant doit fournir uniquement son répertoire de test isolé.

Le marqueur, la base et le verrou résident dans la fixture. La liaison est
immuable ; une table de messages indexée par message_id est donc équivalente
à la clé `(bot_id,chat_id,message_id)` dans ce magasin. Pas de support multi-bot
ou multi-chat. Aucun identifiant privé réel n'est demandé.

## Invariants transactionnels

- Paramètres effectifs vérifiés à chaque ouverture : journal DELETE,
  synchronous EXTRA (=3), foreign_keys ON, temp_store MEMORY, trusted_schema
  OFF, busy_timeout 100 ms. Toutes les mutations utilisent BEGIN IMMEDIATE.
- Une transaction de lot contient les reçus, les réservations de messages et
  le nouvel offset. Aucune valeur candidate n'est retournée avant COMMIT.
  Un échec de lot rollbacke l'ensemble et suspend la session.
- PRIMARY KEY `(epoch,update_id)` dans la liaison de bot immuable. Rejeu
  identique : aucun reçu/message ajouté. Un trou croissant est accepté ; un
  nouvel ID inférieur au curseur, un lot hors ordre ou des IDs répétés à
  l'intérieur du même lot sont refusés, sans saut d'offset.
- Une réservation globale de message ne dépend pas de l'époque : même message
  revu dans une nouvelle époque = reçu duplicate_message, sans réservation
  supplémentaire. Une clé connue avec une projection différente est un conflit.
- L'offset est `max(offset durable,max(update_id)+1)`, sans inventer de reçus
  pour les trous. Un lot vide garde le curseur. L'offset attendu doit correspondre
  exactement au magasin sous verrou. L'ouverture contrôle aussi l'égalité du
  curseur de chaque époque avec le maximum des reçus correspondants.
- Les types d'update non pris en charge et les messages refusés avec ID fiable
  ont un reçu minimal rejected : aucune identité tierce, texte ou empreinte de
  son corps n'est conservé. Sans ID exploitable, tout le lot est refusé.

Le magasin ne traite pas de JSON brut : il reçoit une liste d'objets simulés,
bornée à 20 éléments. Les contrôles futurs du parsing réseau (dont clés JSON
dupliquées) ne sont pas implémentés dans B1. L'authentification pure existante
précède la projection et la conservation des messages privés. Seuls les champs
nécessaires au futur traitement sont persistés. Un résultat `new_messages`
compte des **réservations**, y compris historiques/expirées ; il ne signifie pas
que des commandes ont été exécutées. Le rejeu ne produit aucune nouvelle
réservation. La consommation métier transactionnelle des réservations reste B2.

Les schémas et contraintes SQLite sont comparés au schéma attendu ; version,
integrity_check, foreign_key_check, métadonnées, curseurs, empreintes et liens
reçus/messages sont contrôlés avant reprise. Une anomalie n'autorise jamais un
reset. Les empreintes détectent des incohérences ; elles ne protègent pas contre
un administrateur capable de réécrire ensemble base et empreintes.

## Expiration et continuité

Chaque entrée/reprise reçoit l'heure explicitement. Une commande d'âge supérieur
à 120 secondes devient expired ; la frontière à 120 s est incluse. Les messages
antérieurs à la création du magasin sont historical. L'expiration des pending
est matérialisée à l'ouverture, à resume et avant les lots. Reçus, projections,
clés et curseurs ne sont pas effacés ; aucune purge n'est exposée par B1.
Une date future de plus de 30 s ou une horloge reculant déclenche une suspension.

| Reprise | État et résultat |
| --- | --- |
| 3 heures | normal si aucun incident connu ; réservations anciennes expirées |
| 24 heures exactement | gap_possible, perte potentielle signalée ; aucun événement absent inventé |
| Plusieurs jours, moins d'une semaine | gap_possible persistant, sans prétention de récupération complète |
| 7 jours sans nouvel update enregistré | recovery_required, next_offset indisponible |
| Conflit de lot/stockage | Session inutilisable ; recovery_required enregistré si le disque le permet |

Les lots vides actualisent la dernière réception, mais ne rajeunissent pas la
dernière nouvelle mise à jour. Une nouvelle réception ne referme pas une lacune.
`status()` distingue stored_offset diagnostique et next_offset, nul en
recovery_required. B1 n'envoie jamais l'un ou l'autre à un réseau.

`recover_epoch(..., approval="reviewed-simulation-recovery")` est une opération
explicite de **fixtures**, pas une authentification humaine réelle. Elle exige
recovery_required, une heure cohérente et un lot non vide. Nouvelle époque,
réservations, reçus historiques et nouvel offset sont commités ensemble ; le
maximum de l'ancienne époque n'est pas repris. Les anciens reçus et clés de
messages subsistent. Un rollback conserve l'ancienne époque. La lacune reste
gap_possible. Aucun reset ou changement d'identité automatique.

## Verrouillage, quotas et sécurité des fichiers

Le verrou OS existant du watcher est réutilisé, sur fixture.lock, pour toute
la session. Un second processus échoue sans ouvrir SQLite. La base n'est pas
maintenue dans une transaction pendant un autre travail. Le verrou est local ;
il ne prouve pas l'exclusivité d'un bot distant, qui reste sans objet ici.

Liens symboliques, reparse points sur la chaîne des parents, fichiers à plusieurs
liens et fichiers annexes inattendus sont refusés. Seuls base, journal DELETE,
marqueur et verrou sont admis. -wal/-shm inattendus bloquent l'ouverture sans
suppression automatique. Les tailles physiques sont bornées avant ouverture.

Quotas : 100000 reçus, 1000 messages pending, 128 Mio de base (pages incluses),
512 Mio de dossier. Les seuils peuvent uniquement être abaissés pour les tests.
Contrôle avant travail et avant COMMIT : tout dépassement rollbacke le lot,
sans éviction de tombstones ni purge de preuves. B1 n'écrit aucun log ; les
exceptions contiennent seulement des codes fixes. Une erreur disque empêchant
la persistance de la suspension ferme quand même la session fautive ; aucun
succès ou nouvel offset n'est retourné. Ne pas transformer cette erreur en retry
automatique dans un futur appelant.

### Observations Windows et limites

Essais réels du **moteur local**, sans API Telegram : Python 3.12.10, SQLite
3.49.1. Les valeurs relues sont DELETE, synchronous=3, foreign_keys=1,
temp_store=2, trusted_schema=0, busy_timeout=100. Les arrêts forcés de processus,
le verrou à deux processus, le journal annexe et SQLITE_FULL sont testés sous
Windows. Cela ne simule pas une panne électrique du système de fichiers ou du
contrôleur ; aucune garantie de durabilité matérielle universelle n'est donnée.

Les fichiers POSIX sont créés privés et des modes trop ouverts sont refusés.
Sous Windows, la simulation hérite des ACL du répertoire de test ; elle ne les
modifie pas et ne prétend pas rendre ce dossier prêt pour une activation.
`windows_acl_readiness()` appelle le vérificateur existant, sans en modifier
les règles, sur le dossier et tous ses fichiers, journal compris s'il existe.
Une inspection indisponible ou un seul refus donne acl_ready=false.
Sur le répertoire temporaire de cet essai Windows, le résultat observé est
**false**. B1 ne constitue donc pas une validation ACL d'installation réelle.

Une future activation doit préparer et recontrôler des ACL restrictives dès
la création de tous les fichiers, y compris les auxiliaires. Les limites TOCTOU,
restauration de marqueur, protection contre un dossier alternatif et qualification
du stockage devront être revues ; aucune de ces protections n'est contournée
en B1. Un chemin d'accès synthétique fourni par un appelant de test n'est pas
une autorisation de toucher à un répertoire réel.

## Tests et récupération

`tests/test_private_store.py` ajoute 23 tests ciblés :

- Lots vides/répétés/non contigus, formes invalides, ordre, collisions d'update
  et de message, curseur attendu périmé, refus confidentiels et identités.
- Processus tué par os._exit avant BEGIN, après un reçu, juste avant COMMIT
  et après COMMIT. Avant commit : zéro reçu, curseur initial ; après commit :
  reçu et curseur présents, rejeu sans nouvelle réservation.
- Erreur OSError injectée pendant un lot et SQLITE_FULL réel via max_page_count,
  vérifié par son code moteur : aucune progression de curseur, aucun reçu partiel.
- Redémarrages et séquence reproductible de 30 lots nouveaux/rejoués ; reprise
  après 3 h, 24 h, 3 jours et 7 jours ; changement manuel d'époque et rollback
  de récupération ; expiration à 120 s et au-delà, futur et recul d'horloge.
- Base corrompue, schéma incompatible, base absente, curseur incohérent,
  empreinte modifiée et marqueur différent : refus sans réinitialisation.
- Deux processus, reparse points, hardlinks, fichiers annexes, ACL simulées
  refusées/acceptées, inspection Windows sur la fixture et modes POSIX en CI.
- Gardes sur les accès fichiers de fixtures, réseau, processus, environnement
  et horloge ; aucune lecture de token, aucun accès aux données de production,
  entrées non mutées.

Après anomalie : fermer, préserver les preuves dans la fixture, examiner les
transactions et la cause, puis récupérer explicitement sur une copie de test.
Aucune restauration, sauvegarde automatique, migration, purge ou recréation de
base n'est implémentée. Une base restaurée ou dont le marqueur indépendant est
perdu n'est pas prouvée sûre par une simple vérification SQLite ; le parcours
réel reste interdit. La primitive de sauvegarde cohérente et le plan opératoire
de restauration de A restent à livrer séparément avant C.

Commandes de validation (Python 3.12 de développement) :

```text
python -m unittest discover -s tests -v
python -m ruff check --no-cache .
python -m compileall -q poster.py cadence_simulator.py newsbot tools tests
python -m pip_audit -r requirements.txt -r requirements-dev.txt --progress-spinner off
```

## Suite recommandée

B2 doit ajouter, après autorisation distincte, la consommation atomique des
réservations, l'outbox et les tentatives simulées, les quotas sortants, les
budgets et la sémantique confirmed/rejected/uncertain, sans réseau réel ni
retraitement des réservations. Les 120 s seront revérifiées avant routage et
tentative simulée ; aucune nouvelle fenêtre à la préparation d'une réponse.
Les schémas B1 ne doivent pas être modifiés silencieusement : future migration
versionnée et testée, jamais reset de données.

Les transports existants, le journal confirmé 1947, l'installation Windows,
state.json, workflows, sources, FIFO et cadence de 15 secondes sont inchangés.
Le module n'est raccordé à aucun orchestrateur. Aucun B2, C ou Phase 8.3–8.5
n'est implémenté ; la PR doit rester non fusionnée et sans activation.
