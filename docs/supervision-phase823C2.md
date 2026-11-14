# Phase 8.2.3-C2 — reprise fermée et intégration hors ligne

## Prérequis et portée

Vérifiés avant modification : PR #32 fusionnée dans main au SHA
`bc597005c3a60d067d848aef605019a27995d7b4` ;
[CI post-fusion 43622780203](https://github.com/Pince2Homard/news-telegram/actions/runs/43622780203)
terminée avec succès. Dernier origin/main récupéré, branche C2 dédiée.

C2 fournit des composants de fixtures et des preuves de refus ; **l'activation
réelle reste bloquée**. Aucune nouvelle boucle de collecte, aucun deuxième
orchestrateur, service, tâche ou mécanisme de basculement automatique.
Les contrôleurs lancés par les tests sont des processus synthétiques jetables.

## Deux générations différentes

La génération B2 est l'identité de déploiement épinglée dans SQLite. C2 ne la
change pas automatiquement : modifier cet UUID à l'ouverture reste refusé par
B2. La session C2 est un autre UUID, renouvelé seulement après clôture prouvée.
Le nonce de session nomme aussi le Job Object et accompagne les résultats de
fixture. Un ancien résultat n'est jamais promu dans la session suivante.

Le registre C2 et son témoin sont deux bases SQLite distinctes du magasin
B1/B2/C0, dans des emplacements explicites non imbriqués. Ils contiennent la
même histoire : séquence croissante, empreinte chaînée, identité des trois
chemins, identité privée injectée, génération B2, session, état, identité du
contrôleur, nom du job et checkpoint du magasin fermé.

Chaque base emploie DELETE, synchronous=EXTRA, trusted_schema=OFF,
temp_store=MEMORY, busy_timeout=100 ; chaque ajout utilise BEGIN IMMEDIATE.
Le témoin et le registre ont chacun un verrou OS, pris dans un ordre stable.
Le retour d'une transition exige les deux commits. **Ce ne sont pas des commits
distribués atomiques** : crash entre les commits = divergence = refus, sans
réparation, reset ou choix arbitraire du journal le plus récent.

Schéma, intégrité SQLite, continuité des séquences, chaîne d'empreintes,
égalité intégrale des histoires, identité et horloge sont contrôlés. Histoire
bornée à 1000 événements et fichiers à 4 Mio : saturation = arrêt, aucune purge
de preuve. Le magasin conserve ses quotas B1/B2 et ses propres contrôles.

## Protocole local et ordre des opérations

| État durable | Signification | Reprise autorisée |
| --- | --- | --- |
| sealed | Magasin fermé, empreinte connue, ancien job arrêté ou aucun job lancé | Nouvelle session synthétique |
| running | Session enregistrée avant ouverture du magasin/création réseau | Pas de nouvelle session concurrente |
| intent | Identité contrôleur + nom de job enregistrés avant création | Preuve indépendante nécessaire après crash |
| stopping | Terminaison demandée, récolte ou journalisation non terminées | Pas de reprise automatique |
| stopped | Job vide et root signalé ; clôture du magasin pas encore attestée | Pas de nouvelle session avant seal |
| retired | Arrêt OS prouvé, mais écriture magasin interrompue/non attestée | Refus persistant de begin |

Ordre attendu, qualifié par les tests :

1. Ouvrir FixtureFence avant le magasin. Comparer l'empreinte du fichier fermé
   au dernier checkpoint. Un journal SQLite résiduel empêche cette preuve.
2. begin écrit la session dans les deux journaux avant tout worker.
3. touch_store inscrit dirty=true **avant** l'ouverture B1/B2/C0 : leur reprise
   peut déjà écrire (expiration, intentions in_flight devenant uncertain).
4. Ouvrir le magasin sous son verrou existant, utiliser les raccords de fixtures.
5. Terminer/récolter le job, fermer le magasin ; seal vérifie que le verrou du
   magasin est disponible et écrit l'empreinte finale dans les deux journaux.
6. Une nouvelle session peut alors commencer ; elle conserve reçus, outbox et
   intentions terminales existantes. Aucune nouvelle tentative d'un uncertain.

La fermeture du contexte ne seal jamais implicitement. Elle arrête un job
encore actif. Si l'arrêt OS reste douteux, handles, connexions et verrous restent
retenus en quarantaine. Si seul le commit de preuve échoue après arrêt prouvé,
les verrous peuvent être fermés, mais le registre reste non sealed et bloque
la session suivante. Une erreur disque ne doit pas empêcher la terminaison OS :
TerminateJobObject précède les écritures stopping. La récolte de C1 reste requise.

## Suppression de la fenêtre de création C1

C1 créait suspendu, puis appelait AssignProcessToJobObject. Une mort entre ces
deux appels pouvait laisser un processus suspendu hors du job. C2 utilise
STARTUPINFOEX et PROC_THREAD_ATTRIBUTE_JOB_LIST avec CreateProcess ; aucune
création ordinaire suivie d'une affectation n'est utilisée par le chemin C2.
Pas de fallback si l'attribut est indisponible ou refusé.

Le processus naît suspendu dans le job ; IsProcessInJob est contrôlé avant
ResumeThread. Les limites C1 restent kill-on-close, plafond de huit processus,
sans breakaway, avec environnement sur liste blanche et handles non hérités.
Les descendants ordinaires appartiennent au même job. Le chemin historique C1
reste disponible pour ses fixtures ; **une future activation devrait imposer
le chemin atomique C2**, jamais le seul verrou C1.

Sources : [Microsoft, création directe dans un job](https://devblogs.microsoft.com/oldnewthing/20230209-00/?p=107812/),
[attributs de création](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute).

## Ce qui constitue une preuve de terminaison

Un verrou Windows redevenu disponible, l'absence d'un fichier, un PID absent ou
un timeout ne constituent pas une preuve. La perte du dernier handle d'un job
peut tuer l'arbre tout en faisant disparaître l'objet : **job introuvable =
preuve manquante**, pas « ancien worker certainement mort ».

WindowsObservation, réservé au banc, capture pendant que le contrôleur connu
est encore vivant :

- un handle de processus réel, avec PID **et heure de création** correspondant
  au registre ; ce handle conserve l'objet OS, même si le PID est réutilisé ;
- un handle du job nommé de la session, avec droits QUERY et TERMINATE.

Après la mort, le handle du contrôleur doit être signalé. L'observateur termine
le job retenu et exige ActiveProcesses=0. Tant que le contrôleur est vivant,
que l'identité diffère, qu'un handle manque ou que la terminaison/query échoue,
la reprise est refusée. La preuve n'est ni un JSON ni une attestation par PID.
Référence : [Microsoft GetProcessTimes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes).

**Retenir le handle du job change le comportement de kill-on-close.** Les tests
le montrent : après mort du contrôleur, les descendants peuvent survivre tant
que l'observateur garde le job. L'observateur doit donc les terminer et prouver
le job vide avant toute réconciliation. Cet observateur n'est pas déployé comme
assistant/service de secours et ne démarre aucun nouveau worker.

Si aucune ouverture/écriture magasin n'avait commencé, l'empreinte inchangée et
la preuve OS retenue permettent de clôturer la session, puis d'en créer une
nouvelle. Sinon recover_observed inscrit **retired**. Cette décision est
volontaire : la preuve de mort ne dit pas si une sauvegarde ancienne a effacé
une intention déjà acceptée. Un diagnostic sur une fixture intacte peut ensuite
constater in_flight -> uncertain ; cela ne réarme pas le registre retired.

## Contrats de réception et intégration

private_integration contient seulement deux fonctions de raccordement, sans
polling, timer, envoi, discovery de credential ou lecteur de production.
Elles exigent une session C2 active, touch_store durable et une correspondance
exacte chemin/binding/génération du magasin de fixture.

receive_fixture appelle private_contracts.validate_updates puis receive_batch
B1. Le reçu minimal `{update_id, disposition: rejected}` est **déjà compatible**
avec _project B1 : toute enveloppe sans message privé authentifié est projetée
en rejet avec message_id et digest NULL. Le schéma B1 et son algorithme ne sont
pas modifiés. L'intégration vérifie ce contrat explicitement.

Un lot admis peut contenir messages authentifiés et rejets minimaux ; ils sont
classés, puis reçus/messages/curseur sont commit ensemble par B1. Avant ce
commit aucune progression simulée n'est retournée. Lot malformé sans identité
fiable : refus global, aucune progression. Un même update_id d'abord rejeté
puis porteur d'un contenu admis est un conflit, jamais une promotion silencieuse.

prepare_fixture vérifie l'authentification et la fraîcheur AVANT d'appeler le
lecteur d'instantané injecté. Le routeur reste pur. La réponse déjà calculée
est réutilisée ; l'unicité business_results/outbox/attempts reste celle de B2.
Les envois de fixture restent ScriptedTransport, classe fermée existante.
Un worker local n'est pas branché à un émetteur réel : son interruption est
injectée après l'intention durable, avant toute consommation du simulateur B2.

## Preuves et matrice de qualification

26 nouveaux tests C2, dont huit tests natifs Windows (plusieurs sous-scénarios).
Les résultats complets, SHA publié et CI exactes sont joints à la PR.

| Cas | Preuve | Résultat attendu |
| --- | --- | --- |
| Crash avant job | Mort réelle du contrôleur Windows après intent | Aucun job capturable, begin refusé |
| Crash après job | Job retenu, aucun worker encore créé | Contrôleur signalé + job vide avant nouvelle génération |
| Crash après CreateProcess | Processus réellement suspendu déjà dans le job | Terminaison de l'arbre avant réconciliation |
| Crash pendant requête | Worker et descendant réels, handle du job conservé | Survivants observés puis terminés ; aucun verrou pris pour preuve |
| Crash pendant arrêt | Mort réelle après demande de terminaison | Reprise impossible avant preuve indépendante |
| Requête acceptée localement puis mort du contrôleur | POST /fixture reçu sur 127.0.0.1, réponse retenue, intention SQLite commit | Job vidé ; reçu/curseur conservés ; uncertain, une seule intention, retired persistant |
| Preuve d'arrêt non persistable | Erreur de témoin injectée | Job malgré tout terminé, génération non clôturée |
| Terminaison impossible | Erreur native injectée | Deux verrous et connexions gardés, pas d'arrêt sûr |
| PID réutilisable / ancien résultat | Heure de création modifiée ou nonce ancien | Refus |
| Reçu rejeté minimal / doublon / conflit | Contrats + vrai SQLite B1/B2 | Trace minimale, commit atomique, aucun second message/outbox |
| Acceptation simulée puis réponse perdue | ScriptedTransport timeout_after_accept | uncertain après redémarrage/changement d'époque, aucune nouvelle tentative |
| Quota, disque plein, horloge, corruption | Vrai SQLite et fautes injectées | Aucun avancement partiel, pas de reset |
| Sauvegarde/restauration C0 | Vrai SQLite, ACL synthétiques | Restauration désarmée, confirmed conservé, livraison refusée |
| Consommation de credentials/transport réel | Lecteur de token, constructeur réel, requests et sockets remplacés par erreurs dans l'intégration pure | Aucun de ces accès consommé |

Les tests natifs exécutent Windows Job Objects et handles réels. La suite Linux
ignore ces huit tests : elle prouve les contrats et la persistance portables,
pas le fencing OS Windows. Les ACL C0 de l'intégration sont **simulées** : aucun
nouveau contrôle positif d'ACL native n'est revendiqué et aucune ACL n'est modifiée.
Les tests C0 nécessitant l'ancienne autorisation de dossier racine restent exclus.

Toutes les destinations réseau de fixtures restent 127.0.0.1. Pas de DNS réel,
TLS Telegram, API, polling, webhook ou message distant. Les clés/identités sont
synthétiques, les emplacements sont des TemporaryDirectory sous le Temp courant.
Le certificat local et les garde-fous réseau C1 ne sont pas élargis.

## Limites qui maintiennent l'activation bloquée

Le témoin C2 est un fichier de fixture distinct, **pas** une ancre matérielle ou
un tiers distant non restaurable. Il détecte une restauration du magasin seul,
d'un journal seul ou un clone déplacé. Un rollback coordonné du magasin ET des
deux journaux aux mêmes chemins, ou un clone de machine préservant ces chemins,
ne peut pas être distingué d'un passé légitime par ces seuls fichiers.
Les empreintes détectent incohérences et accidents ; elles ne sont pas une
signature protégeant contre un administrateur capable de réécrire les preuves.

Un crash dirty reste donc retired même avec preuve OS. Aucune commande de reset
ou d'acquittement manuel ne transforme ce doute en permission de renvoyer.
La perte des handles après crash/reboot reste bloquante. Aucun observateur
durable autonome ou protocole OS post-reboot n'est fourni. Une session running
interrompue avant même l'intention de job reste également bloquée sans preuve.

La durabilité dépend de SQLite/NTFS et du matériel ; pas de preuve de coupure
électrique réelle, caches disque ou kernel en panne. Les I/O de preuves du
contrôleur ne sont pas des opérations temps réel : C2 demande la terminaison
avant leur écriture à l'arrêt, mais ne promet pas de borner un fsync kernel.
La boucle locale ne qualifie pas DNS/WAN ni un futur canal de secret.

## PC Windows / Raspberry Pi : protocole documentaire et simulé

1. Un registre d'autorité indépendant des sauvegardes doit attribuer un seul
   hôte et une génération croissante. Son indisponibilité interdit le démarrage.
2. Geler l'ancien hôte, empêcher toute relance de son processus et retirer sa
   capacité réseau/secret par un fencing externe réellement vérifiable.
   L'absence de heartbeat ou l'expiration d'un bail ne prouvent pas cet arrêt.
3. Établir l'arrêt de tout ancien lecteur/envoi et réconcilier l'état durable,
   y compris les intentions uncertain. Une réponse perdue n'autorise pas un retry.
4. Transférer une image cohérente désarmée, vérifier identité, génération,
   checkpoint et reçus conservés, puis qualifier ACL/permissions du nouvel hôte.
5. Autorisation humaine distincte, enrôlement numérique manuel vérifié,
   absence d'autre lecteur/webhook, budget/quotas/arrêt révocable qualifiés.
   Si une preuve manque : rester arrêté, jamais démarrer automatiquement le Pi.

Les simulations de clones, générations périmées et témoins divergents couvrent
les décisions de refus. Elles ne prouvent ni un fencing matériel PC/Pi, ni un
consensus intermachines. Aucun Pi n'est contacté ou déployé.

Avant un premier essai Telegram : revue C2, mécanisme externe anti-rollback et
exclusivité prouvés, protocole de reprise dirty/post-reboot revu, qualification
du stockage/ACL réel, canal secret sans logs/argv/environnement, adaptation
DNS/TLS/WAN au budget et autorisation d'essai limitée distincte. Aucun exactly-once
réel n'est revendiqué.

## Validation et invariants de périmètre

Commandes Python 3.12 : unittest discover complet, Ruff, compileall de poster.py,
cadence_simulator.py, newsbot, tools et tests ; pip-audit production + développement.
Les résultats Windows et les CI du SHA exact figurent dans la PR de livraison.

Validation locale finale : Windows 10 build 19045 AMD64, Python 3.12.10,
SQLite 3.49.1. Suite complète : **364 tests en 69.005 s, OK, 4 exclus**
(trois fixtures C0 natives non réautorisées et un cas POSIX). Les 26 tests C2,
dont huit natifs Windows, passent sans exclusion sur cet hôte. Ruff et
compileall réussissent ; pip-audit ne trouve aucune vulnérabilité connue sur
les dépendances de production et développement. Contrôle du diff : pas de
credential, clé privée réelle, chemin de profil ou endpoint Telegram ajouté.

```text
python -m unittest discover -s tests -q
python -m ruff check .
python -m compileall -q poster.py cadence_simulator.py newsbot tools tests
python -m pip_audit -r requirements.txt -r requirements-dev.txt --progress-spinner off
```

Watcher de production, state.json, workflows, sources, FIFO, cadence de 15 s et
journal confirmé 1947 inchangés. Aucun accès à un token/ID personnel, aucune
modification d'ACL, aucun nouveau dossier sous C:\, aucune activation ou fusion.
L'autorisation temporaire C0 n'est pas réutilisée. Arrêt du travail après PR.
