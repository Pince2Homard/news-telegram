# F3 — intégration privée et arrêt opérateur, exclusivement hors ligne

## Verdict et prérequis

**F3 VALIDÉE POUR POURSUIVRE HORS LIGNE** dans le périmètre de fixtures qualifié
ci-dessous. **Exploitation réelle : NO-GO.** F4 n'est pas engagé ni autorisé.

Avant modification : PR #38 fusionnée au SHA exact
`5f5483c7504e31915efa416f3964481d3c6e9575` ;
[CI post-fusion 43653164245](https://github.com/Pince2Homard/news-telegram/actions/runs/43653164245)
terminée avec succès sur ce SHA. Dernier origin/main récupéré à ce commit,
checkout initial propre, branche `codex/phase-f3-private-integration`.
Rapports F1/F2 et contrats B1/B2/C0/C1/C2/C3 relus.

## Architecture minimale et fichiers

FixtureIntegration prolonge private_integration : c'est le raccord explicite de
l'unique contrôleur de fixtures, pas une nouvelle boucle ou un second moteur.
L'appelant fournit déjà magasin, fence, autorisation, adaptateur, gate d'arrêt,
heure et instantané. Aucun emplacement ou credential n'est découvert.
L'usage reste séquentiel dans le thread qui possède les connexions SQLite.

| Fichier dans tools/windows_watcher/ | Rôle F3 |
| --- | --- |
| private_integration.py | Deux préflights requis, réception locale vers B1, préparation restreinte, livraison F2 vers B2 et désarmement |
| private_outbox.py | Transactions d'intention et résultat partagées ; option trial_only ; deliver reste fermé à ScriptedTransport |
| private_recovery.py | next_operation seulement après récolte attestée dans la même session ; aucun reset de reprise |
| private_stop.py | Événement Windows latché, ticket de routage de fixture et commande de secours locale |
| private_job.py | Hook minimal avant ResumeThread ; comportement historique par défaut conservé |
| private_credential.py | Gate d'arrêt avant quota/création, publication de l'identité du worker suspendu et contrôle avant reprise |
| private_bot_adapter.py | Passage explicite du gate et rejet des retours après demande d'arrêt |

Tests : tests/test_private_f3.py. Aucun changement du routeur à six commandes,
des schémas SQLite, des règles ACL, du fournisseur F2 ou du transport de
publication. Aucune dépendance ajoutée, aucune autorité C5 raccordée.

Le contrôleur commence par fence.begin puis touch_store AVANT l'ouverture du
magasin, dont la reprise B2 peut écrire. Une fois le trial explicitement autorisé,
il exécute getMe puis getWebhookInfo locaux, comptés dans les lectures C3.
Réception et envoi exigent ces deux confirmations et les identités épinglées.
Chaque opération recontrôle la cible, l'enrôlement et la génération ; aucune
identité n'est apprise depuis un message reçu.

Une nouvelle opération dans cette même session C2 exige phase stopped, job.safe
et zéro processus actif. Les anciens handles sont déjà récoltés. next_operation
ajoute une transition dans les deux journaux, conserve dirty et la session,
et ne permet pas begin depuis retired ou un registre non sealed. La réutilisation
du nom du job est refusée par Windows si un ancien détenteur le conserve encore.

## Frontières transactionnelles et incertitudes

Toutes les transactions magasin restent BEGIN IMMEDIATE, DELETE/EXTRA, sous le
verrou B1/B2. Les bases C2 et C3 gardent leurs transactions distinctes ; aucune
atomicité distribuée entre magasin, quotas et témoins n'est revendiquée.

| Frontière | Avant commit / interruption | Après commit / interruption |
| --- | --- | --- |
| Session C2 et touch_store | Aucun worker permis | Session dirty ; absence de clôture fiable bloque la reprise |
| Quota de préflight/lecture C3 | Aucun worker repris | Compteur consommé, même si création ou transport échoue |
| Réception locale validée | Aucun offset candidat utilisé pour une nouvelle lecture | Passage à B1 seulement si retour à temps, job récolté et génération valide |
| Lot B1 reçus/messages/curseur | Rollback global, ancien offset durable | Reçus et nouvel offset disponibles ensemble ; doublons sans nouvelle réservation |
| Préparation résultat/outbox | Aucun résultat partiel ; calcul pur éventuellement refait | Texte/destination/génération figés ; aucune recomposition d'une réponse déjà préparée |
| Intention B2 attempts/outbox | Aucun envoi local avant retour du commit | in_flight durable dans les deux tables, une seule tentative possible |
| Quota de réponse C3, puis création worker | Pas de reprise du worker sans quota | Aucun remboursement ; même refus certain après intention devient conservativement uncertain |
| Acceptation locale, retour perdu ou tardif | Intention conservée | uncertain, jamais renvoyé ; suspension C3 et admission fermée |
| Réponse cohérente | HTTP 200 seul insuffisant : destination, texte, ID, génération et délai doivent correspondre | Décision de transport seulement, pas encore une confirmation B2 |
| Résultat attempts/outbox | Échec avant commit : in_flight persiste, devient uncertain à l'ouverture de diagnostic | Deux états terminaux atomiques et ID distant unique ; crash après commit conserve ce résultat |
| Arrêt | Signal OS puis terminaison/récolte avant toute écriture de clôture | Intentions restantes uncertain, prepared annulés, trial désarmé ; fermeture du magasin puis seal explicite |

Le prochain getUpdates local prend uniquement store.status().next_offset après
commit B1. Aucun offset en mémoire issu d'une réponse non sauvegardée n'est
réutilisé. Les reçus minimaux rejetés de validate_updates restent compatibles
avec B1, sans stockage du contenu de tiers.

Pour l'envoi, les helpers privés _begin_attempt et _finish_attempt sont communs
à B2 et au raccord F3. private_outbox.deliver accepte toujours exactement
ScriptedTransport. F3 n'y injecte ni callback réseau ni transport arbitraire.
FixtureIntegration appelle seulement le LocalBotAdapter F2 exact et exige un
PinnedSyntheticProvider, l'enrôlement C3 et le gate Windows explicites.

La réponse validée F2 est confrontée à la requête figée B2 et à la session C2.
Le résultat final et l'ID sont vérifiés de nouveau par le magasin. Un rejet
cohérent est terminal rejected, son cooldown est durable et aucun retry n'est
exécuté. Perte, contradiction, arrêt, timeout ou résultat inconnu sont uncertain.
Une erreur de stockage ferme l'admission et désarme le trial ; si le disque ne
permet plus de committer uncertain, l'intention in_flight reste une ambiguïté
durable, sans permission de renvoi. La reprise B2 la classe uncertain sans
réarmer C2/C3. Un commit final réellement réussi reste terminal après crash.
Il n'existe aucune garantie d'exactly-once distant.

## Commandes, authentification et instantané

Le contexte d'essai autorise **exactement** /help et /status. Espaces de bord,
suffixe @bot, changement de casse, newline, arguments et quatre autres commandes
sont refusés avant consultation de l'instantané. Le routeur historique et ses
six commandes restent inchangés pour les autres contextes.

B1 authentifie propriétaire/chat privés avant conservation ; F3 réauthentifie
avant snapshot et avant intention. Les événements non autorisés gardent seulement
leur reçu minimal. Les commandes refusées déjà authentifiées gardent leur trace
B1, sans outbox créée par F3. /help liste seulement les deux commandes et ne lit
aucune vue. /status emploie uniquement le snapshot injecté, sans collecte.
Le résultat est figé une fois ; aucun nouveau chargement lors d'une répétition.
Une ancienne aide préparée pour le contexte à six commandes n'est ni réécrite
ni promue en réponse d'essai : elle est refusée par le contrôle trial_only.

Expiration B1 à 120 s conservée, sans effacement des reçus ou du curseur.
Les plafonds C3 restent 300 s / 20 lectures / 6 tentatives ; les deux préflights
consomment deux lectures, donc au plus 18 réceptions ensuite. Les budgets natifs
F2 sont réduits à l'échéance C3 et à la fraîcheur restante de l'envoi.
Une tentative B2 peut être conservativement marquée uncertain alors que le quota
a empêché sa création réseau : elle ne peut jamais devenir une seconde tentative.

## Procédure opérateur et secours local

NativeStopGate crée pour la session un événement Windows manuel, non héritable,
initialement non signalé. Il n'a aucune fonction ResetEvent. Une collision de
nom est refusée sans signaler l'événement préexistant. Le gate est vérifié à
l'admission, avant création/transfert et immédiatement avant ResumeThread.
Le worker est toujours créé suspendu atomiquement dans son Job Object C2.
Le protocole F2 de duplication ciblée non héritable du secret reste inchangé.

L'appelant fournit un nouveau répertoire de contrôle de fixture existant.
stop-ticket.json est créé avec refus d'écrasement ; il contient uniquement
session, noms d'objets OS et identités PID/heure de création, aucun secret.
L'identité du worker est publiée par remplacement atomique avant sa reprise.
Ce ticket est un **indice pour ouvrir les objets OS**, jamais une preuve d'arrêt.

Arrêt primaire dans le thread contrôleur : appeler FixtureIntegration.shutdown
dans le finally d'exploitation de fixture. Il ferme l'admission et signale
l'événement AVANT SQLite, termine/récolte le job, efface le secret maîtrisé,
persiste uncertain/cancelled et le désarmement. recovery_required, expired ou
exhausted ne sont pas remis à revoked pour masquer leur cause. Puis fermer le
magasin et appeler fence.seal seulement si la clôture est effectivement sûre.

Si ce thread est bloqué, depuis un autre terminal du même compte approuvé :

```text
python -m tools.windows_watcher.private_stop --fixture-stop "<fixture>/control/stop-ticket.json"
```

Cette commande ne démarre aucun lecteur et n'utilise aucun credential. Elle :

1. Valide le ticket de fixture, ouvre l'événement et le signale pour fermer
   l'admission. Elle ne dépend pas d'une boucle ou coopération du worker.
2. Ouvre le job nommé avec QUERY/TERMINATE, puis un handle réel du worker.
   Vérifie heure de création et appartenance au job, jamais le seul PID.
3. Demande TerminateJobObject, exige ActiveProcesses=0 puis la racine signalée
   sur le handle capturé. Même une preuve racine manquante n'empêche pas de
   demander la terminaison du job connu.
4. Ferme ses handles après contrôle et renvoie worker_stopped=true seulement
   si la preuve complète est acquise. controller_closure reste **false**.

Le contrôleur doit encore effectuer sa propre récolte, fermer ses handles,
persister le désarmement et clôturer son magasin. Un retour positif du secours
n'atteste donc pas le commit SQLite ou la fermeture du contrôleur. La commande
ne force aucune transition C2 et ne transforme pas une preuve manquante en GO.
Ticket absent/périmé, objet introuvable, heure incorrecte ou erreur WinAPI :
STOP_UNPROVEN, aucun arrêt sûr déclaré, aucune nouvelle session autorisée.

Entre deux workers, l'événement signalé interdit la prochaine reprise. Une
création concurrente déjà suspendue doit être arrêtée sans ResumeThread. Le
secours qui ne peut capturer une racine valide ne revendique pas une preuve
globale fondée sur un job vide ou un PID absent. Si le contrôleur ne peut
terminer/récolter : quarantaine, handles et verrous conservés, admission fermée.
Aucun taskkill par PID, service, tâche planifiée ou secours Telegram parallèle.

## Preuves et validation

Hôte natif : Windows 10 build 19045, Python 3.12.10, SQLite 3.49.1.
Suite complète finale : **441 tests en 138,077 s, succès, 4 exclusions**.
Les 16 tests F3 (**10 portables/modèles et 6 natifs Windows**, avec plusieurs
sous-scénarios) sont inclus et passent. Les CI du SHA publié sont consignées
dans la PR.
Ruff, compileall et audits production puis production+développement réussissent ;
aucune vulnérabilité connue signalée.

| Niveau | Preuves obtenues |
| --- | --- |
| Windows natif complet | getMe/getWebhookInfo locaux, reçu et offset B1, aide restreinte, intention B2 visible avant transport, réponse TLS locale, confirmation B2 durable et refus d'un second envoi |
| Windows natif des résultats | Acceptation sans retour, mauvais chat, retour après budget global, rejet 429 ; uncertain/rejected durables et une seule tentative |
| Windows natif de l'arrêt | CLI dans un autre processus pendant lecture, écriture de 32 Mio, handshake TLS et worker suspendu ; job vide/racine signalée, récolte avant libération du verrou, secret fermé |
| Windows natif négatif | TerminateJobObject refusé : quarantaine/verrous gardés ; mauvaise heure de création : aucun succès revendiqué ; collision d'événement sans modification de l'existant |
| Mort réelle du contrôleur | POST local accepté après intention ; handles C2 capturés avant mort ; contrôleur tué, job terminé, dirty réconcilié retired ; une intention uncertain, offset conservé, trial désarmé au diagnostic |
| Crashes SQLite portables réels | 13 frontières avant/après réception, préparation, intention et résultat ; os._exit, réouverture et vérification des commits/rollbacks et de l'absence de seconde tentative |
| Modèles sans socket | Whitelist, authentification avant loader, déduplication, 20 lectures incluant préflights, 6 réponses, refus du septième, 300 s, expiration, cible/génération changée |
| Restauration C0 | Sauvegarde/restauration SQLite réelle, ACL simulées ; RESTORE_BARRIER conservée et préparation F3 refusée sans snapshot |
| Non-régression | Suite publication et B1–C5 conservée ; tests F2 de duplication ciblée, erreurs/interruptions et héritage concurrent inchangés |

Les modèles substituent explicitement l'échange F2 ; leurs compteurs ne sont pas
une preuve réseau. Les tests natifs d'arrêt isolent certaines frontières avec
préflights déjà attestés par la fixture ; le test complet exerce aussi ces deux
préflights réellement sur boucle locale. Aucun reboot Windows physique, coupure
électrique, autre compte ou permission de production n'est testé.

Les trois tests C0 nécessitant une nouvelle autorisation ACL native restent
exclus, ainsi que le cas de modes POSIX sur Windows. L'autorisation historique
de dossier racine C0 n'est pas réutilisée. La CI Ubuntu exclut les tests natifs
Windows et ne les remplace pas par une simulation présentée comme une preuve OS.
Toutes les nouvelles fixtures sont dans TemporaryDirectory, identités et
credentials synthétiques, destinations 127.0.0.1 exclusivement. Les règles F2
contre DNS, proxy, redirection et retry ne sont pas élargies.

## Risques résiduels et conditions d'entrée en F4

| Risque / manque | Conséquence obligatoire |
| --- | --- |
| Compte, chemin, parents NTFS et Credential Manager réels non qualifiés | Autorisation distincte d'emplacements/fixtures précisément nommés ; aucun credential réel avant qualification |
| DNS/TLS/WAN réel absent | Boucle locale insuffisante ; aucune extension automatique vers Telegram |
| Fsync ou contrôleur bloqué | Secours OS peut arrêter le worker, pas garantir un commit ; conserver NO-GO si clôture non attestée |
| Perte du ticket/handles après reboot ou crash | Aucune recréation de preuve ou reprise automatique ; retired/recovery_required et diagnostic humain |
| Restauration coordonnée, clone, compte/admin/OS hostile | Limites F1 toujours présentes ; aucune autorité C5 ni promesse anti-rollback ajoutée |
| Exclusivité distante du bot | Verrou/event Windows ne prouve pas l'absence d'un lecteur sur une autre machine ; vérification future séparée |
| Mémoire/dumps/pagefile | Effacement des buffers maîtrisés seulement, pas d'effacement absolu démontré |
| Effet accepté puis confirmation perdue | uncertain terminal ; aucune consultation manuelle future ne permet un renvoi automatique |

Pour envisager F4 : revue humaine de cette PR et autorisation F4 distincte,
approbation du modèle Windows F1 et des limites, autorisation précise des fixtures
du compte/stockage/credential cible, plan de qualification réseau et d'arrêt
sur ce poste. Ensuite seulement, sous autorisations séparées, enrôlement réel
numérique manuel, identité/webhook/exclusivité et éventuel essai limité lié au
SHA, hôte, génération, méthodes, quotas et opérateur. Une entrée en qualification
F4 ne vaut pas autorisation d'essai Telegram. Un critère manquant reste NO-GO.

Aucun token ou identifiant privé réel, Telegram, ACL/Credential Manager réel,
production, state.json, workflow, watcher, FIFO, source, cadence ou journal
confirmé 1947 touché. Aucun Pi, déploiement, service, tâche ou démarrage
automatique. Une seule PR F3, sans fusion ni activation. Arrêt après livraison.
