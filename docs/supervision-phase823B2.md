# Phase 8.2.3-B2 — outbox privée et livraison de fixtures

Prérequis vérifiés avant modification : PR #29 fusionnée au SHA
`476c0be32fbf21a7efa80ed461aafa0bd8f730da` ; CI post-fusion
[43603869360](https://github.com/Pince2Homard/news-telegram/actions/runs/43603869360)
terminée avec succès. Ce SHA est le dernier origin/main récupéré pour B2.

Cette livraison est exclusivement simulée. Aucun adaptateur HTTP, réception
Telegram, token, polling, service, tâche ou installation n'est ajouté. Le
routeur, les protections ACL et transports existants restent inchangés. Le
watcher réel n'est jamais consulté : temps et instantané sont fournis en mémoire.

## Architecture et contrat

`private_outbox.DeliveryStore` spécialise le magasin de fixtures B1, son verrou,
ses transactions et ses contrôles d'intégrité. Il fournit des opérations
unitaires ; aucune boucle ou second orchestrateur. Les seuls changements du
magasin B1 sont un point de reprise après ouverture et des paramètres internes
de validation de schéma. `SimulationStore` continue à produire/valider le
schéma v1 : tous les tests B1 restent inchangés.

```python
# À utiliser uniquement dans une fixture isolée, avec identités synthétiques.
with DeliveryStore(directory, config, bot_id, now, create=True,
                   generation=fixture_uuid) as session:
    session.receive_batch(simulated_updates, expected_offset=0, now=now)
    session.prepare(message_id, snapshot, now)
    outcome = session.deliver(message_id, ScriptedTransport(), now,
        permission=SimulationPermission(bot_id, owner_id, chat_id,
                                         fixture_uuid, enabled=True))
```

Les identifiants et UUID de cet exemple sont des variables de fixture, pas des
valeurs à demander au propriétaire pendant B2. L'UUID de génération est injecté
puis épinglé. Aucun mode réel ni mécanisme d'enrôlement réel n'est exposé.

## Schéma v2 et migration

Le schéma v2 conserve les quatre tables B1 **sans modification** et ajoute :

| Table | Rôle et unicité |
| --- | --- |
| delivery_identity | singleton : génération, liaison d'identité, cooldown durable |
| business_results | PRIMARY KEY message_id, réponse structurée figée, empreinte, date de préparation |
| outbox | PRIMARY KEY message_id, FK vers le résultat, destination, génération, échéance, création, empreinte des champs figés, état |
| attempts | PRIMARY KEY message_id, FK vers l'outbox, intention/quota, état, fin, code fixe, ID de succès synthétique unique |

Une seule tentative par outbox est volontaire : aucun retry automatique pour
rejected/uncertain. Une nouvelle commande crée une autre réservation, jamais
une nouvelle tentative de l'ancienne réponse. Les identités bot/chat sont
immuables dans le magasin ; message_id est ainsi unique pour cette liaison.

Ouverture d'un magasin v1 : `migrate=True` est indispensable. Le schéma, les
reçus, empreintes, curseurs et quotas B1 sont validés **avant** tout DDL. Sous
le verrou et BEGIN IMMEDIATE, création des quatre tables, liaison de génération
et passage de PRAGMA user_version à 2 forment une transaction unique. Aucun
DROP, effacement, recréation ou transformation des données B1. Une création
B2 neuve initialise d'abord B1 puis effectue cette migration ; une création
interrompue ne devient pas une permission de réinitialiser le dossier.

Sans permission de migration, ou pour une version inconnue/incompatible,
l'ouverture échoue. Un crash avant son commit laisse v1 ; après commit, v2
complet. Une réouverture v2 ne remigre pas. L'ancien lecteur B1 refuse v2 sans
le rétrograder. Le résultat et l'outbox sont contrôlés par empreintes, liaison
épinglée, cohérence des dates, contraintes et état de tentative. Ces empreintes
détectent les incohérences ; elles ne résistent pas à un administrateur capable
de réécrire ensemble les données et leurs preuves.

Pas de migration automatique de production : ce contrat concerne seulement des
fixtures privées. Avant une migration d'installation réelle future, sauvegarde
cohérente, procédure de restauration désarmée et contrôle des ACL restent requis.

## Consommation atomique et confidentialité

`prepare(message_id, snapshot, now)` vérifie le magasin et l'identité avant
toute lecture de l'instantané. Un refus B1 n'a pas de réservation exploitable.
Une réservation historique/expirée ou absente n'appelle pas le routeur. Les
messages pending admissibles passent à nouveau l'authentification stricte
existante avant l'appel du routeur pur aux six commandes et filtres existants.

Une transaction enregistre ensemble le résultat métier et l'outbox. Le résultat
unique est la preuve de consommation : il n'est pas nécessaire de modifier le
statut de réception B1. `pending` dans les diagnostics B1 n'est donc pas une
invitation à retraiter : il faut vérifier l'absence de business_results. Une
préparation répétée renvoie le résultat enregistré et ne recalcule rien, même
avec un nouvel instantané. Si le processus tombe avant commit, le calcul pur
peut être refait ; après commit, la réponse est figée. Aucun instantané brut,
corps de commentaire, credential ou chemin externe n'est persisté.

Seule la réponse HTML bornée du routeur est admise. Dans le contrat actuel,
elle contient du texte échappé, sans balisage actif ; le contrôle d'aller-retour
escape/unescape rejette les entités malformées ou un balisage inattendu. La
taille reste au plus 2048 caractères échappés. Le simulateur confirme le texte
après décodage des entités : comparaison au texte rendu, pas au HTML échappé.

La destination est prise dans la liaison privée du magasin, jamais dans le
texte d'une commande ou un champ du snapshot. Génération, destination,
échéance `message.date+120`, création et réponse sont figées et vérifiées.
Une mutation d'identité, configuration, génération ou données figées provoque
un refus fermé, sans appeler le simulateur. Aucun identifiant personnel réel
ni contenu de réponse n'est écrit dans un log ; le module n'émet aucun log.

## Tentatives et reprise

| État | Sémantique |
| --- | --- |
| prepared | Résultat/outbox durables ; première tentative non commencée |
| in_flight | Intention et réservation de quota commitées avant le simulateur |
| confirmed | Retour simulé cohérent et succès enregistré durablement |
| rejected | Rejet explicite cohérent ; terminal sans retry |
| uncertain | Acceptation non déterminable ou succès non durable ; terminal sans retry |
| expired | Échéance dépassée avant la première tentative ; traces conservées |
| cancelled | Annulation locale de fixture avant tentative ; traces conservées |

Avant une tentative, une permission de simulation explicite doit correspondre
exactement au bot, au propriétaire, au chat privé et à la génération ; enabled
et simulation_only doivent être des booléens vrais. Ce n'est jamais une
autorisation de contacter Telegram. Les contrôles de fichiers, schéma, horloge,
continuité, expiration et quotas précèdent la transaction d'intention.

L'appel au simulateur intervient après COMMIT de l'intention et en dehors de
toute transaction. Au redémarrage sous verrou, chaque in_flight devient
uncertain, avec état de tentative concordant. Il ne redevient jamais prepared.
Un succès reçu puis non commité conserve l'intention ; la session se ferme et
la reprise classe cette intention incertaine. Les erreurs de stockage ou
d'intégrité désarment la session via les mécanismes B1 et peuvent nécessiter
une réconciliation de fixture avant de nouveaux travaux.

Confirmed exige un résultat synthétique positif, le chat privé exact et le
texte rendu exact. Les réponses incohérentes deviennent uncertain. Un timeout
après acceptation simulée illustre le risque : renvoyer pourrait produire un
doublon. Le fait que l'outbox soit unique localement ne prouve **aucun
exactly-once sur Telegram réel**. Un confirmed déjà durable n'est pas rejoué.

## Débit et budget simulés

Toutes les heures proviennent des paramètres injectés. Aucune lecture d'horloge
système, sleep, attente réelle ou rattrapage automatique :

- Au moins 2 secondes entre les débuts de tentatives ; pas de rafale accumulée.
- Au plus 10 tentatives dans la fenêtre glissante `(now-60, now]`, y compris
  rejets et incertitudes. À exactement 60 s, la première sort de la fenêtre.
- Quotas déduits des intentions persistantes ; redémarrer ne les remet pas à
  zéro. Si limité, retour rate_limited, outbox encore prepared et aucune intention.
- Un rejet 429 simulé peut fixer un cooldown durable ; il n'autorise pas le
  retry de cette réponse rejected. Les autres prepared gardent leur échéance.
- Âge exactement 120 s admissible ; au-delà, prepared devient expired. Préparer
  tard ne redonne pas 120 secondes. Un appel commencé à temps finit selon son
  résultat, même si la fenêtre s'est fermée entre-temps.

`ScriptedTransport` est une machine de fixtures fermée injectable, avec une
liste d'issues synthétiques : succès, rejet, timeout, acceptation puis timeout,
réponse malformée ou discordante. Seule cette classe exacte est acceptée ;
aucun callback de transport arbitraire, adaptateur HTTP ou token n'est admis.
Les requêtes/acceptations sont des preuves de test en mémoire, pas des logs.

Une durée synthétique permet de tester un budget sortant **simulé** de 20 s.
Une issue tardive devient uncertain et le temps logique de fin est borné à
ce budget. Cela n'est pas un mécanisme d'interruption d'appel réseau : DNS,
socket, worker et budget réseau global réel restent entièrement hors de B2.
La clock injectée doit avancer jusqu'à la fin simulée avant l'opération suivante ;
un recul est refusé par B1.

## Validation et limites

Les tests B1 et des phases antérieures sont conservés sans changement. Les
tests B2 couvrent six commandes/filtres, confidentialité avant snapshot,
unicité/réouverture, refus d'identité/génération/destination, HTML, expiration,
annulation, quotas persistants et cooldown, mauvaises réponses et timeouts.

Des processus sont tués avant préparation, après résultat métier mais avant
outbox, avant/après commit de préparation, avant/après intention et avant/après
commit du résultat. Les tests vérifient l'absence de résultat isolé, le maintien
du curseur B1 et l'absence d'un second appel après une intention interrompue.
La migration est interrompue à quatre points ; les reçus et réservations B1
restent identiques. SQLITE_FULL réel est provoqué par max_page_count ; aucune
préparation partielle n'est conservée. Un succès simulé suivi d'erreur de commit
reste incertain. Une séquence reproductible de préparations/livraisons et
réouvertures vérifie la non-duplication.

Gardes de tests contre réseau, fichiers externes, environnement sensible,
horloge système, processus et sleep pendant préparation/livraison ; aucune
mutation de l'instantané injecté. Les tests de crash lancent uniquement leurs
processus de fixture, jamais un assistant.

```text
python -m unittest discover -s tests -v
python -m ruff check --no-cache .
python -m compileall -q poster.py cadence_simulator.py newsbot tools tests
python -m pip_audit -r requirements.txt -r requirements-dev.txt --progress-spinner off
```

Limites Windows inchangées : SQLite DELETE/EXTRA et ses paramètres effectifs
sont vérifiés en tests ; ce n'est pas une preuve de résistance à une panne
électrique. Le diagnostic ACL hérité reste non qualifié sur les temporaires de
test (acl_ready=false), bloquant pour une future activation réelle, sans empêcher
les fixtures. Aucun droit n'est élargi pour le contourner. Restent à qualifier
avant C : ACL à la création de tous les annexes, stockage/VFS, TOCTOU, identité
indépendante du magasin, sauvegarde/restauration désarmée et protection contre
une restauration ancienne ou un autre lecteur distant.

Avant C : revue des résultats B2, autorisation distincte du propriétaire,
enrôlement manuel local numérique, exclusivité du bot/webhook, adaptateur privé
HTTPS séparé et budget total effectivement interrompable, quotas de session et
plan d'arrêt borné. Aucun de ces mécanismes réels n'est implémenté ici. Le journal
1947, state.json, workflows, transports existants, sources, FIFO, cadence et
installation de production restent inchangés. La PR B2 est livrée sans fusion
ni activation ; aucune Phase 8.3–8.5 n'est commencée.
