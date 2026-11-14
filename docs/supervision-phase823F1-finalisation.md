# F1 — architecture Windows simplifiée et plan de finalisation

## Décision proposée : développement local, exploitation NO-GO

Retenir un seul PC Windows, un contrôleur, un lecteur actif et un bot privé dédié
distinct du bot de publication. Aucun Pi, basculement, redémarrage automatique
ou autorité externe requis pour cette première version. L'opérateur autorise,
surveille et arrête chaque essai.

Il s'agit du réarbitrage demandé par la mission F1, pas d'une preuve que les
risques C4/C5 ont disparu. Leur acceptation finale reste explicitement humaine.
Les protections existantes restent toutes conservées. **Mise en service NO-GO ;
F2 recommandé sous autorisation distincte, non démarré.**

Prérequis vérifiés avant modification : #36 fusionnée au SHA exact
`ade04de19b8dc88020affb5b88bd7d4591758635`,
[CI post-fusion exigée 43640631838](https://github.com/Pince2Homard/news-telegram/actions/runs/43640631838)
terminée avec succès ; dernier origin/main récupéré à ce SHA. Checkout initial
propre, branche `codex/phase-f1-finalisation`, fichiers locaux préservés.

## Architecture cible et réemploi

Les fichiers ci-dessous sont dans `tools/windows_watcher/`. Aucun n'est modifié
par F1. Une seule chaîne privée réutilise les primitives existantes ; aucun
second moteur de collecte GitHub ou de publication n'est ajouté.

| Module | Réemploi | État actuel / limite |
| --- | --- | --- |
| private_router | Authentification, /help, /status pur | Six commandes disponibles ; garde d'essai à restreindre sans retirer les autres usages |
| private_store (B1) | Reçus/messages/curseur atomiques, unicité et expiration | Simulation ; cible réelle non qualifiée |
| private_outbox (B2) | Réponse figée, intention et résultat persistants | deliver accepte exactement ScriptedTransport ; pont réel absent |
| private_qualification (C0) | ACL, identité, sauvegarde cohérente, restauration désarmée | Aire native historique supprimée ; RESTORE_BARRIER conservé |
| private_contracts | Validation stricte et projection minimale | Contrats purs, pas preuve de provenance réseau seuls |
| private_job | Structures WinAPI, budgets, récolte | Chemin C1 historique réservé aux fixtures |
| private_fenced_job | Création atomique JOB_LIST, preuve OS | Chemin cible obligatoire ; aucun repli création puis affectation C1 |
| private_recovery | Génération, checkpoints et refus de reprise douteuse | Double journal et retired conservés ; pas d'anti-rollback hostile |
| private_trial | Autorisation distincte, quotas, échéance, révocation | Fixtures seulement ; pas d'interface réelle |
| private_credential | Cycle de buffer DPAPI/pipe borné | Format synthétique fermé ; fournisseur réel non qualifié |
| private_integration | Authentifier avant lecture de vue, réception/préparation | Raccord de fixtures, sans boucle ni transport réel |

`private_authority` et tous ses tests C5 restent conservés comme expérimentation,
sans devenir une dépendance de la version Windows proposée. Aucun nettoyage,
renommage, suppression ou réorganisation cosmétique de code dans F1.

### Chaîne privée proposée

1. Lancement manuel désactivé : vérifier compte, stockage, identité numérique
   bot/propriétaire/chat, unicité locale et autorisation d'essai.
2. begin puis touch_store avant ouverture du magasin : la reprise B2 écrit déjà.
   Refuser les états restaurés ou non réconciliés, sans effacer leurs preuves.
3. Réserver durablement un quota de lecture ; lancer le futur worker privé
   sous Job Object atomique, canal secret borné et deadline monotone globale.
4. Valider le retour, authentifier avant consultation des données et classer
   chaque update. Commit B1 reçus/messages/curseur AVANT toute prochaine lecture
   qui acquitterait cet offset. Lot douteux : aucun avancement autorisé.
5. Filtrer /help et /status pour l'essai ; conserver la fraîcheur 120 s de B1.
   Préparer/figer la réponse et son outbox atomiquement dans B2.
6. Réserver une tentative ; commit in_flight avant tout envoi. Les transactions
   quota/intention distinctes peuvent surcompter après panne, jamais rembourser
   pour rejouer une intention ambiguë.
7. Worker sans SQLite : exécuter l'adaptateur privé, valider destination/texte
   figés puis commit résultat attempts/outbox ensemble. HTTP 200 seul ne vaut
   pas confirmed. Réponse ou commit perdus : uncertain sans réémission.
8. Fermer l'admission, terminer/récolter le job, décharger le secret et désarmer.
   Clôturer seulement si l'arrêt est prouvé ; aucune reprise au boot.

/status utilise l'instantané déjà disponible ; son absence/péremption ne
déclenche pas un nouveau collecteur. Le futur adaptateur privé reste indépendant
de la publication et de ses routines ponctuelles, token, chat et calendrier.
Aucune information privée ne doit être injectée dans le canal de publication.

## Garanties non négociables conservées

| Garantie | Protection disponible | Manque avant réel |
| --- | --- | --- |
| Propriétaire/chat numériques stricts | authenticate, contracts, projection B1 | Enrôlement privé manuel et validation réseau |
| Bot dédié épinglé | Binding bot_id et génération | Préflight réel sous autorisation, jamais apprentissage automatique |
| Reçus/messages/curseur/outbox durables | Schémas B1/B2/C0 et transactions | Qualification stockage cible et raccord complet |
| Intention avant tentative | Commit in_flight B2 | Même frontière sur adaptateur réel |
| uncertain non réémis | Unicité attempts et reprise fermée | Tests perte retour/commit de la vraie jonction |
| confirmed/retired/recovery_required conservés | Gardes et restauration désarmée | Diagnostic humain, aucun reset de confort |
| Worker arrêté et récolté | JOB_LIST, kill-on-close, job vide/root signalé | DNS/TLS/WAN et contrôle opérateur |
| Une instance locale | Verrous OS et générations | Compte et tous les points d'entrée à qualifier |
| NTFS/secrets privés | Vérificateurs C0, DPAPI/pipe synthétiques C3 | Chemin/parents et fournisseur réels |
| Quotas/expiration | SQLite C3 300/20/6, fraîcheur B1, deadlines | Compter tous les appels y compris préflight |
| Arrêt d'urgence | stop/emergency_stop et observation de fixtures | Commande opérateur indépendante du worker |
| Aucun redémarrage automatique | États désarmés après interruption | Entrée réelle et reboot/restauration à qualifier |
| Séparation publication | Modules séparés, aucun raccord actuel | Tests négatifs croisés à l'intégration |
| FIFO/cadence inchangées | Aucun fichier de production modifié | Non-régression à chaque étape ; 15 s conservées |

## Matrice des risques

Probabilités qualitatives de planification, non mesurées sur le futur poste.
« Hors modèle » ne signifie jamais « improbable » ou « techniquement résolu ».

### Risques à maîtriser techniquement

| Risque | Impact | Probabilité | Protection disponible | Limite résiduelle | Mesure opérationnelle |
| --- | --- | --- | --- | --- | --- |
| Crash processus | Tentative ambiguë/verrou libéré tôt | Plausible | Intention, C2, Job Object | Handles perdus, arrêt non attestable | Ne pas relancer ; diagnostic après preuve d'arrêt |
| Reboot imprévu | Perte horloge monotone/handles | Plausible | Actif rouvert → recovery_required | Reboot physique non qualifié | Annuler essai ; pas de reprise automatique |
| Interruption réseau | Retour partiel ou perdu | Attendue à terme | Deadline et contrats bornés | WAN/DNS non qualifiés | Suspendre, conserver intention |
| Confirmation perdue | Effet accepté non confirmé localement | Plausible | in_flight → uncertain | Aucun exactly-once distant | Ne jamais réémettre |
| Double traitement local | Réponse doublée | Plausible après panne | Unicités reçus/message/outbox/attempt | Jonction réelle absente | Un lecteur, restauration jamais réarmée |
| Corruption stockage | Perte de preuves | Possible | Schéma/intégrité/quota/backup C0 | Matériel et images anciennes | Arrêt fermé, pas de reset |
| Autre compte Windows non autorisé | Lecture/remplacement/token exposé | Possible selon ACL | Owner/DACL/parents contrôlés C0 | Cible non qualifiée | Compte dédié, refuser chaîne dangereuse |
| Exposition accidentelle token | Usage non autorisé du bot | Possible à l'intégration | Pipe/DPAPI fake, erreurs fermées | Fournisseur réel/dumps/pagefile | Pas d'argv/env/log ; révocation dédiée en incident |
| Quotas dépassés | Essai non borné | Plausible sans raccord | Compteurs persistants, états terminaux | Tous les accès doivent être comptés | 300/20/6, aucun retry/prolongement |
| Worker bloqué non arrêtable | Action continue après arrêt demandé | Rare mais critique, non mesurée | TerminateJobObject, récolte/quarantaine | UI et défaillance kernel non qualifiées | Ne pas déclarer arrêt sûr ; bloquer nouveau lancement |

La restauration accidentelle ordinaire reste à maîtriser : sauvegarde cohérente,
restauration désarmée, conservation des états et interdiction de relance. Elle
n'est pas assimilée à un rollback coordonné indétectable exclu ci-dessous.

### Risques hors modèle initial, acceptation explicite à formaliser

| Risque réarbitré | Impact | Probabilité | Protection disponible | Limite résiduelle | Mesure/acceptation humaine |
| --- | --- | --- | --- | --- | --- |
| Administrateur malveillant | Réécriture des protections | Non évaluée | Moindre privilège/ACL ordinaires | Admin peut contourner | Accepter explicitement ; suspendre si poste suspect |
| Clone intentionnel complet | Copies d'identité/capacité concurrentes | Non évaluée | Checkpoints et procédure un hôte | Clones coordonnés non distingués | Aucun clone autorisé ; accepter la limite |
| Restauration coordonnée indétectable | Permissions/quotas réhabilités | Non évaluée | C2 détecte certains retours partiels, C0 désarme | C5 reproduit l'ancien état valide | Accepter explicitement ; aucun snapshot pour reprendre |
| Attaquant possédant déjà le token | Appels directs hors contrôleur | Non évaluée | Protection/révocation et conflits | Quotas/verrous locaux ne bloquent pas cette capacité | Révoquer en incident ; concurrent observé = arrêt |
| OS complètement compromis | Contrôles et secret compromis | Non évaluée | Durcissement pour pannes ordinaires | Pas de confinement de l'OS | Poste sain requis, annuler au soupçon |
| Basculement automatique seconde machine | Lecteurs concurrents | Exclu du périmètre | Aucun mécanisme déployé | Promotion non qualifiée | Ne pas déployer ; nouvelle décision si besoin |

Accepter ces limites n'autorise pas à envoyer malgré une ACL refusée, une
corruption, un état uncertain, un conflit constaté ou un worker vivant.

## Inventaire des fonctionnalités manquantes

Difficulté relative M = moyenne, E = élevée, sans engagement de durée.
Fichiers existants dans `tools/windows_watcher/` ; aucun futur fichier créé ici.

| Point | Existe / manque | Fichiers | Tests nécessaires | Difficulté | Dépendances | Autorisations humaines |
| --- | --- | --- | --- | --- | --- | --- |
| 1 Stockage Windows | Validateur/preuve C0 historique ; cible compte/chemin absente | private_qualification, private_store, credential_acl | Owner/DACL/parents, annexes, accès autre compte refusé, backup/restore désarmé | E | Compte, chemin et politique backup | Cible/fixtures précisément approuvées, pas de réutilisation de l'aire C0 supprimée |
| 2 Credential dédié | DPAPI/pipe fake ; fournisseur réel absent | private_credential, private_fenced_job, private_local_worker | Entrée synthétique unique, refus autre cible, héritage concurrent, nettoyage maîtrisé, absence fuite | E | Stockage sûr, création processus maîtrisée | Fixtures séparément autorisées ; lecture réelle seulement F4 |
| 3 Adaptateur Bot API | Contrats et HTTP/TLS local ; sérialisation réelle désactivée manquante | private_contracts, private_local_worker, private_integration ; adaptateur privé à proposer | Requêtes locales, JSON tronqué, erreurs 4xx/429, no redirect, aucun trafic externe | E | Secret et budget revus | F2 hors ligne uniquement |
| 4 DNS/TLS/WAN | Stalls locaux/DNS simulé ; pile distante non qualifiée | private_job, private_fenced_job, futur adaptateur | DNS bloqué, TLS refusé, write/read lents, budget total, récolte/retards | E | F2 prêt, F3 arrêt fiable | Local F2/F3 ; destinations réelles exactes F4 |
| 5 Confirmation B2 | Intention et résultat simulés ; pont réel absent | private_outbox, private_contracts, private_integration | Crash autour des deux commits, retour cohérent/tronqué, perte résultat, aucune seconde tentative | E | Adaptateur et contrat résultat | F3 ; garder simulation fermée par défaut |
| 6 Whitelist | Six commandes routeur ; garde essai absente | private_router, private_integration | Refuser autres commandes/arguments avant vue ; conserver tests existants | M | Admission et plan | F3, périmètre approuvé |
| 7 Arrêt indépendant | Primitives natives/observation ; commande opérateur absente | private_trial, private_fenced_job, private_recovery ; entrée privée à proposer | Arrêt pendant wait/DNS/écriture, contrôleur défaillant, job vide/root signalé | E | Worker F2, entrée unique F3 | Interface/fixtures autorisées ; pas de service/tâche |
| 8 Lecteur/webhook | Verrou local ; préflight réel absent | private_recovery, private_trial, futur adaptateur | Deux instances, conflit synthétique, webhook → refus, pas de purge | M/E | Identité et opérateur | Attestation unique hôte et préflight F4 |
| 9 Doublons après interruption | B1/B2/C0/C2 ; jonction complète à éprouver | private_store, private_outbox, private_qualification, private_recovery | Redémarrage manuel, époques, 120 s/24 h/jours, ancienne image, états conservés | E | F3 et backup sûr | Procédure diagnostic ; aucun réarmement automatique |
| 10 Premier essai | Plafonds C3 ; entrée/runbook cible absents | private_trial, private_integration, documentation | Défaut désactivé, aucune action import/CI/boot, quotas et révocation de bout en bout | M/E | Tous les autres écarts | GO humain final lié au SHA/hôte/IDs locaux/méthodes/arrêt |

### Écarts reproductibles à traiter séparément

- private_outbox.deliver refuse tout type différent de ScriptedTransport.
  Les tests C3 et test_private_authority_integration montrent la distinction
  confirmation externe/local uncertain. Spécifier le pont réel en F3 ; ne pas
  utiliser un hook de panne comme interface de production.
- FixtureFence.begin exige sealed. Le crash dirty réconcilié donne retired
  même après preuve d'arrêt ; reproduction dans test_private_recovery.
  Définir diagnostic et fin d'essai en F3, sans forcer sealed pour repartir.
- C0 restauré refuse réception/préparation/livraison, même avec permission de
  simulation ; ne pas contourner sa barrière par les lecteurs B1/B2.
- Le pipe C3 est temporairement héritable. Une création concurrente à héritage
  large peut faire fuiter un handle ; qualifier/fermer cette fenêtre en F2
  avant fournisseur réel, pas seulement valider la liste de cet enfant.

Ces écarts ne sont pas déclarés corrigés par F1. Aucun patch fonctionnel ici.

## Plan après F1 : trois jalons, pas trois PR garanties

### F2 — stockage privé et adaptateur réseau désactivé

Entrée : plan accepté et autorisation F2 hors ligne. Choisir compte, chemin
logique et fournisseur dédié ; obtenir toute autorisation de fixture ACL ou
d'entrée Credential Manager avant création. F1 n'autorise aucune de ces mutations.

Réutiliser C0 pour fixtures de stockage et C3 pour le canal secret. Développer
un adaptateur privé testable exclusivement sur serveur local, avec réseau réel
impossible par défaut et sans token réel. Conserver JOB_LIST et budget global.
Qualifier tailles/headers/JSON, TLS local, erreurs et absence de fuite. Parents
NTFS dangereux : arrêter et faire approuver une autre cible, sans assouplissement.

Sortie : preuves synthétiques, adaptateur local désactivé et suite/CI revus.
Aucun credential réel, DNS distant ou essai Telegram.

### F3 — intégration, confirmations et arrêt opérateur

Entrée : F2 validée et contrat du résultat/admission approuvé. Raccorder au même
contrôleur privé B1, whitelist, B2, quotas et résultat validé/commit durable.
Conserver la version pure/simulée pour ses usages et tests existants.

Fournir une commande d'arrêt accessible pendant le worker bloqué et un secours
d'arrêt limité indépendant de ce worker : aucun lecteur Telegram de secours,
aucune relance. Préparer handles connus et identité du processus/job, terminer
puis exiger job vide et root signalé. PID seul insuffisant. Après reboot ou perte
des preuves : annuler l'essai et diagnostiquer ; ne pas effacer retired ou
recovery_required et ne pas réarmer automatiquement.

Sortie : tests Windows de panne/arrêt, restauration désarmée, quotas couvrant
tous les appels, séparation des données privées et non-régression publication.
Runbook fixé. Worker impossible à arrêter/récolter : aucune qualification réelle.

### F4 — qualification cible et éventuel essai réel

Deux portes : autorisation d'inspection/qualification de la cible, puis nouvelle
décision GO pour l'essai. Accès credential, identité/webhook et destinations
nécessitent des permissions explicites ; une validation F2/F3 ne suffit pas.

Opérateur présent sur un seul Windows, bot privé et IDs validés localement,
absence de lecteur/webhook concurrent vérifiée sans suppression implicite.
Proposition : 300 s totales, 20 lectures incluant deux préflights (au plus
18 réceptions ensuite), 6 tentatives de réponse, /help et /status seulement.
Budgets exacts des préflights à approuver. Aucun retry, backlog prolongeant
l'essai ou redémarrage ; expiration 120 s avec traces conservées.

Au premier doute : fermer admission, demander terminaison OS avant écritures
de clôture, récolter, décharger secret/handles, persister le désarmement. Si
l'arrêt ne peut être prouvé, utiliser le dispositif qualifié, rester arrêté
et révoquer le credential privé selon autorisation opérateur. Une révocation
n'annule pas l'acceptation distante antérieure : uncertain reste non réémis.
Aucun effet sur le bot de publication.

Les jalons peuvent nécessiter plusieurs PR et sous-qualifications. Pas de date
promise : refus ACL, choix fournisseur, perte des handles au reboot, DNS non
borné, confirmation/commit ou ambiguïté de restauration peuvent allonger le
projet ou empêcher F4. Ne pas reporter ces problèmes après le GO.

## Arbitrages humains et critères GO/NO-GO révisés proposés

À décider dans l'ordre :

1. Confirmer le modèle Windows local et accepter explicitement les risques hors
   modèle, dont rollback coordonné et token déjà compromis. La mission F1 permet
   cette proposition, sans donner de GO d'exploitation.
2. Valider F2/F3/F4, puis autoriser F2 hors ligne sur des fixtures précisément
   définies et avec permissions nécessaires distinctes.
3. Revoir F2 et autoriser F3, sa jonction transactionnelle et son arrêt, sans réel.
4. Approuver l'inspection cible F4 et les accès privés/réseau préalables exacts.
5. Après preuves, GO distinct lié au SHA, compte/hôte, génération/essai, IDs
   locaux, méthodes/textes/quotas/budgets, opérateur et mécanisme de révocation.

Révision proposée de C4/C5 : autorité externe et résistance au rollback hostile
ou clone complet ne seraient plus des conditions automatiques, **uniquement
après validation explicite du réarbitrage**. Les résultats négatifs C5 restent
vrais, conservés et visibles. Aucun ancien rapport n'est réécrit en GO.

Tous les autres critères restent obligatoires : auth/bot, ACL/secrets cibles,
unicité locale et lecteur unique attesté, garde webhook/conflit, reçu avant
offset, intention avant envoi, confirmation durable, non-réémission, quotas,
expiration/whitelist, arrêt opérateur prouvé, reboot/restauration désarmés,
séparation publication et autorisation finale. Un manque n'est pas un simple
avertissement : **NO-GO**. Aujourd'hui plusieurs critères manquent : NO-GO maintenu.

## Validation et références

Références relues : [C0](supervision-phase823C0.md), [C1](supervision-phase823C1.md),
[C2](supervision-phase823C2.md), [C3](supervision-phase823C3.md),
[C4](supervision-phase823C4.md), [C5](supervision-phase823C5.md), et composants
B1/B2/C0/C1/C2/C3 cités. La preuve ACL native C0 concernait son aire supprimée,
pas le futur compte/emplacement. Aucune nouvelle création de cette aire permise.

Validation locale avant interruption de quota : Windows 10 build 19045,
Python 3.12.10 / SQLite 3.49.1, **411 tests en 93.306 s, OK avec 4 exclusions**
(trois C0 natives non réautorisées et un cas POSIX). Le journal a été retrouvé
et son résumé vérifié à la reprise. Les tests natifs C1/C2/C3 présents sont
exécutés, sans requalifier une cible réelle ou le WAN. Les contre-exemples C5
restent des limites, pas des garanties. Ruff et compileall passent ; pip-audit
production/développement ne trouve aucune vulnérabilité connue.

```text
python -m unittest discover -s tests -q
python -m ruff check .
python -m compileall -q poster.py cadence_simulator.py newsbot tools tests
python -m pip_audit -r requirements.txt -r requirements-dev.txt --progress-spinner off
```

Les CI du SHA livré seront jointes à la PR. Linux n'y démontre pas les APIs
Windows natives ni les ACL cibles. Aucun test fonctionnel ajouté pour ce seul
document ; non-régression publication et tests C5 conservés.

## Recommandation finale et périmètre

**Recommander une autorisation F2 hors ligne**, après revue humaine de F1 : la
base transactionnelle et le confinement Windows sont utiles, les écarts sont
identifiés sans imposer un serveur externe au bot personnel. Priorité au
stockage/secret et adaptateur local désactivé, pas à l'activation. F2 ne démarre
pas ici ; fusion et acceptation des risques restent exclusivement humaines.

Un seul document ajouté. Aucun Telegram, credential/ID privé réel, ACL,
Credential Manager, dossier nouveau sous C:\, infrastructure, production,
state.json, watcher, workflow, source, FIFO, cadence ou journal confirmé 1947
modifié. Aucun Pi, service, tâche, activation, fusion ou début de F2.
Arrêt après livraison de l'unique PR F1.
