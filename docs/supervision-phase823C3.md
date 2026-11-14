# Phase 8.2.3-C3 — préparation opérationnelle hors ligne

## Décision et prérequis

**Aucun essai Telegram réel n'est prêt à être autorisé sur les seules preuves C3.**
Le mécanisme d'autorité externe anti-rollback, le stockage réel du credential et
la pile réseau distante ne sont pas qualifiés. C3 les laisse bloquants.

Avant modification : #33 fusionnée au SHA
`78adc18e89deb1b63534698350092ddfe8d7f6fa`,
[CI post-fusion 43629189871](https://github.com/Pince2Homard/news-telegram/actions/runs/43629189871)
verte ; origin/main récupéré au SHA
`1b00db2eae16a05b4b8250af064d7217c5e13294`. Branche C3 indépendante,
checkout initial propre et fichiers locaux préservés.

## Architecture initiale minimale

Un seul hôte Windows, un contrôleur lancé manuellement, un seul lecteur privé
et un bot privé dédié distinct de News Telegram, conformément à 8.2.3-A.
Le transport de publication existant reste inchangé. Pas de Pi, service,
observateur de secours déployé, tâche, démarrage au boot ou orchestrateur ajouté.

C3 ajoute seulement :

- private_trial : plan désactivé, enrôlement numérique injecté et compteurs de
  fixture persistants ; aucune boucle ni destination réseau ;
- private_credential : DPAPI en mémoire et pipe anonyme vers le worker fixe
  C1/C2, avec format exclusivement synthétique.

Le worker conserve le banc 127.0.0.1 et la création atomique dans le Job Object.
Les nouvelles méthodes d'attributs de création servent au seul héritage borné
du pipe, sans dupliquer l'implémentation CreateProcess de C2.

Les fonctions privées et les contextes sont appelés explicitement par les tests.
Importer/ouvrir le programme n'arme rien. Les tests CI peuvent autoriser des
**fixtures synthétiques**, jamais un essai réel : telegram_enabled doit être
exactement false et aucun adaptateur Telegram n'est créé.

## Autorité indépendante : choix à valider, blocage actuel

| Option | Apport | Décision C3 |
| --- | --- | --- |
| Verrou OS + deux journaux locaux C2 | Concurrence locale et restauration partielle détectées | Conservés ; insuffisants contre rollback coordonné |
| DPAPI / Credential Manager | Confidentialité du stockage sous le compte Windows | Ne constituent ni compteur monotone ni autorité de génération |
| TPM / compteur matériel | Ancre potentiellement indépendante des images disque | Non qualifié ; complexité disproportionnée à ajouter dans C3 |
| Autorité externe sérialisant générations, quotas et checkpoints | Peut refuser un ancien état même si tous les fichiers locaux sont restaurés | Nécessaire à étudier/qualifier séparément, aucun client/service ajouté |
| Registre d'autorité tenu humainement hors des sauvegardes du PC | Candidat simple pour un essai manuel pas à pas | Proposition opératoire, aucune garantie technique revendiquée |

Pour un premier essai, la proposition minimale est une session **sans reprise**,
avec autorité humaine indépendante conservant hors des sauvegardes locales le
dernier numéro d'essai, génération, hôte, SHA, checkpoint et état des intentions.
Chaque permission de lecture/envoi devrait être consommée de façon irréversible
avant l'opération, puis le checkpoint attesté après commit. La simple signature
d'un vieux document n'établit pas qu'il est encore le dernier : il faut vérifier
le dernier état auprès de l'autorité, sans cache local de permission réutilisable.

Le support, la protection, le protocole d'accès et la fiabilité de cette autorité
ne sont pas choisis ou prouvés dans C3. Une attestation humaine peut aussi être
erronée. Aucun booléen « approuvé » local ne remplace cette preuve pour le réel.
Si cette sérialisation externe ne peut pas être établie, **ne pas démarrer**.
Un rollback coordonné du magasin, des journaux C2 et du budget C3 reste
indétectable par ces seuls fichiers. Un clone de machine n'est pas exclu par
le host_id injecté : cet UUID est un binding de fixture, pas une preuve matérielle.

## Reprise, reboot et intervention humaine

| Situation | Conduite obligatoire |
| --- | --- |
| Reboot/crash avec essai actif | Le budget rouvert devient recovery_required ; aucune reconstitution d'échéance monotone, aucun réarmement automatique |
| Registre C2 non sealed | begin refusé même si les verrous OS sont disponibles |
| Ancien worker douteux | Exiger preuve OS indépendante C2 ; PID seul, heartbeat absent et job introuvable ne suffisent pas |
| Handles perdus après reboot | Pas de preuve automatique fournie ; rester arrêté et réconcilier avec l'autorité externe et le fencing du credential |
| retired | Refus durable d'une nouvelle session ; prouver la mort ne prouve pas le checkpoint magasin |
| uncertain | Conserver l'intention et ses preuves ; ne jamais l'envoyer de nouveau, même après rotation de secret ou génération |
| recovery_required B1 ou C3 | Inspection humaine après arrêt prouvé ; ne pas remettre normal en effaçant un drapeau |
| Checkpoint, témoin ou budget perdu/corrompu | Refus, aucun fichier recréé par l'ouverture ordinaire, pas de reset |
| Checkpoint externe perdu | Aucun dernier état fiable ; annuler l'essai, conserver les preuves locales et rester désarmé |
| Clôture propre | Conserver reçus, curseur, outbox et quotas ; le même essai C3 reste closed et non réarmable |

Une intervention humaine peut constater un état, classer un incident, révoquer
le credential privé et autoriser une nouvelle **analyse**. Elle ne peut pas
transformer une intention ambiguë en nouvelle tentative ni choisir arbitrairement
le checkpoint le plus récent. L'arrêt/fencing de tout ancien détenteur doit être
établi hors de l'image restaurée. Aucune reprise intermachines n'est déployée.

## Secret : interface qualifiée et stockage proposé

SyntheticVault n'accepte que le format fermé `C3-SYNTHETIC-` suivi de 32 chiffres
hexadécimaux. Il ne peut pas accueillir un token Telegram. Aucun lecteur de
credential réel, recherche de fichier ou énumération Credential Manager n'est
exposé par C3.

Windows CryptProtectData/CryptUnprotectData sont réellement exercés en mémoire,
avec UI_FORBIDDEN, sans LOCAL_MACHINE, prompt, description ou audit de contenu.
Le buffer d'entrée appartenant à l'interface est effacé après protection.
Les sorties natives sont effacées puis LocalFree ; le plaintext temporaire de
use est effacé même si le consommateur échoue. Le blob est effacé et fermé
après transfert ou arrêt ; toute nouvelle utilisation est refusée.
DPAPI utilise les services Windows du compte courant, sans appel CredRead ni
consultation d'un token du projet.

Références : [CryptProtectData](https://learn.microsoft.com/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata),
[CryptUnprotectData](https://learn.microsoft.com/windows/win32/api/dpapi/nf-dpapi-cryptunprotectdata).

Pour le premier essai réel, le candidat à revoir est une entrée privée ciblée
du Credential Manager sous le compte dédié au contrôleur, de durée de session,
sans découverte/énumération et sans repli vers News Telegram. Cette option n'est
**ni implémentée ni testée** dans C3 : aucun objet existant du magasin Windows
n'a été lu/écrit. Une entrée de fixture unique devrait d'abord être qualifiée
sous autorisation distincte, puis le fournisseur réel revu avant usage.
Le candidat correspondrait à CRED_PERSIST_SESSION, dont la portée reste à
qualifier sur le compte prévu : [Microsoft CREDENTIALW](https://learn.microsoft.com/en-us/windows/win32/api/wincred/ns-wincred-credentialw).

Un blob DPAPI persistant dans un fichier ne serait acceptable qu'après preuve
des ACL du fichier et de toute sa chaîne de parents ; cette preuve positive
n'est pas disponible pour un nouvel emplacement réel. Cette alternative n'est
pas utilisée pour contourner le contrôle ACL et n'est pas choisie ici.

## Transfert vers le worker et nettoyage

Le parent crée un pipe anonyme de 4096 octets. Seule son extrémité de lecture
est rendue héritable et inscrite dans PROC_THREAD_ATTRIBUTE_HANDLE_LIST, en
plus de JOB_LIST. Les handles de job, SQLite, verrous et sortie du parent ne
figurent pas dans cette liste. L'environnement reste la liste blanche C1.
Les arguments contiennent un numéro de handle et le chemin de fixture,
**jamais la valeur synthétique**.

Le processus est encore suspendu et déjà dans son job lorsque le parent écrit
une seule petite trame bornée dans le pipe vide. Les deux handles parent sont
fermés et le vault effacé avant ResumeThread. Le worker lit longueur <=128 et
format synthétique, enlève l'héritabilité, efface son buffer et ferme le handle
avant toute connexion. Un format invalide n'atteint même pas le serveur local.
L'accusé contient seulement le code fixe synthetic-consumed ; aucun secret ou
empreinte de secret n'est placé dans le résultat.

Source : [Microsoft, héritage borné de handles](https://learn.microsoft.com/en-us/windows/win32/procthread/creating-processes).

Après arrêt sûr, l'arbre du job est vide et les handles de pipe parent fermés.
Un échec de transfert interdit ResumeThread, consomme le quota et désarme l'essai.
Un échec d'arrêt conserve les verrous/quarantaine ; le parent efface néanmoins
son vault. Un secret éventuellement encore présent dans un processus non arrêté
ne peut pas être déclaré nettoyé. Aucune promesse d'effacement de toutes les
copies internes à Python, DPAPI, au kernel, au pagefile ou aux crash dumps :
seuls les buffers maîtrisés sont vérifiés. Compte Windows compromis, administrateur
ou code hostile du même utilisateur restent hors des garanties qualifiées.
Le futur contrôleur doit être dédié, sans créations de processus non contrôlées
concurrentes pendant la brève fenêtre d'héritabilité du pipe.

## Enrôlement et permission séparée du credential

ManualFixtureEnrollment exige des IDs numériques positifs bot/propriétaire/chat,
une génération de déploiement, un host_id et un trial_id explicites. Aucun
constructeur n'apprend ces valeurs à partir d'un message. Un dictionnaire entrant,
pseudonyme, premier /start ou simple token ne vaut pas enrôlement.

Proposition réelle, non exécutée : le propriétaire contrôle son bot dédié dans
son application de confiance, un défi privé court peut faire apparaître un
candidat dans une session d'observation **séparément autorisée**, puis il valide
manuellement les deux IDs et la provenance directe. Le candidat n'est jamais
enrôlé automatiquement. Épingler aussi l'ID numérique du bot et le vérifier sous
le budget de préflight autorisé ; toute divergence désarme. Les IDs réels et
la preuve restent locaux, jamais dans GitHub, les rapports ou ce chat.

authorize_fixture nécessite le plan de fixture explicitement activé, l'enrôlement
exact et une approbation synthétique distincte. Le vault ne fournit aucune de
ces permissions. Le code C3 ne comporte pas d'équivalent d'autorisation réelle.

## Plafonds testés, sans activation réseau

Valeurs proposées, toujours à approuver humainement pour le réel :

| Paramètre | Plafond de fixture |
| --- | --- |
| Durée | 300 secondes, horloge monotone réelle et horloge civile contrôlées |
| Lectures | 20 tentatives maximum |
| Réponses | 6 tentatives maximum |
| Hôte / génération / lecteur | Binding exact, un worker actif par session |
| Réseau Telegram | Toujours désactivé, même avec fixture_enabled=true |

FixtureTrial utilise SQLite DELETE/EXTRA, BEGIN IMMEDIATE, schéma et identité
stricts, fichier borné et verrou de session. Les compteurs sont commit **avant**
la tentative, sans remboursement après erreur. Une panne entre les commits C3
et B2 peut surcompter ; elle ne doit pas créer une permission supplémentaire.
Pas de transaction distribuée revendiquée entre ces bases.

La date limite monotone native part avant le commit d'autorisation. Le worker
est borné par le minimum de son budget C1 et de cette échéance, sans rallonge
pour finir un backlog. Recul d'horloge = recovery_required ; expiration = refus.
Premier dépassement = exhausted ; erreur/retour incomplet = suspension, aucun retry.
emergency_stop termine/récolte le worker, nettoie le vault puis persiste revoked.
Une défaillance disque ne permet pas de réutiliser un essai actif après reboot.

Ce registre de quotas reste local et restaurable : ses garanties supposent
l'absence de rollback coordonné. Il ne lève pas le blocage d'autorité externe.
La commande humaine réelle d'arrêt d'urgence et son canal de contrôle ne sont
pas installés ; les tests appellent la primitive d'arrêt, et C2 couvre la mort
du contrôleur. Aucun bouton/CLI d'activation réelle n'est livré.

## Qualification et résultats

22 nouveaux tests C3, dont **10 natifs Windows**, passent. Suite complète Python
3.12 : **386 tests en 78.763 s, OK avec 4 exclus** (3 C0 natifs non réautorisés
et 1 POSIX). Ruff, compileall et pip-audit réussissent ; aucune vulnérabilité
connue sur les dépendances production/développement. Les CI du SHA livré sont
liées dans la PR. La suite existante de publication est exécutée sans changement
de ses sources, paramètres ou permissions.

| Niveau | Preuves | Limites |
| --- | --- | --- |
| Windows natif | DPAPI/integrité du blob, pipe hérité whitelisté, TLS local, nettoyage des buffers maîtrisés, révocation en requête, arrêt impossible/quarantaine, durée native bornée | Pas d'autre compte, Credential Manager, stockage réel, pagefile ou dump qualifiés |
| Intégration native locale | Réception synthétique, authentification, reçu/curseur commit, préparation, intention, retour local, interruption et mort réelle du contrôleur | Requête POST /fixture avec contenu factice ; aucun adaptateur Bot API |
| Résultat B2 | Réponse locale validée par contracts ; succès enregistré par ScriptedTransport fermé, ou uncertain après panne injectée | Le futur commit d'une réponse réelle n'est pas implémenté |
| Portable/simulé | 20/6/300, défaut désactivé, IDs manuels, quotas persistants, corruption, commit refusé, horloges, reboot simulé après os._exit | Pas de coupure électrique ou reboot Windows physique |
| C1/C2 conservés | Expiration 120 s, anciens résultats/générations, doublons/époques, job/descendants et reprise refusée | Perte de handles et rollback complet restent bloquants |
| CI Linux | Tests portables et banc local antérieur | Les 10 tests C3 natifs y sont ignorés |

Le test de crash C3 reçoit une requête sur 127.0.0.1 après intention SQLite
commit, conserve indépendamment les handles C2, tue le contrôleur et prouve le
job vide avant réconciliation : une seule intention uncertain, C2 retired,
budget C3 recovery_required, aucune reprise au reboot simulé.
La révocation réelle du token chez le fournisseur n'est pas simulée par ce
simple état local : elle reste une action extérieure distincte à qualifier.

## Procédure proposée pour le premier essai, non autorisée

1. Fermer les blocages ci-dessous. Opérateur présent sur le seul Windows approuvé,
   aucun boot automatique, CI ou lancement par le watcher. Relever SHA, génération,
   essai, checkpoint et dernier état auprès de l'autorité extérieure.
2. Attester l'arrêt de tout ancien lecteur, l'absence de détenteur distant et
   le fencing des anciens secrets. Aucune reprise d'un essai interrupted/retired.
3. Vérifier le stockage privé, son intégrité, les reçus et les permissions.
   Confirmer manuellement les trois IDs numériques du bot privé dédié.
4. Autorisation explicite du SHA/hôte/génération/essai et chargement du credential
   dédié par le fournisseur revu ; la présence de l'entrée seule ne démarre rien.
5. Préflight proposé : une vérification d'identité et une de webhook, budgets
   séparés de 10 s maximum chacun, incluses dans les 300 s et, par prudence,
   dans les 20 lectures (donc au plus 18 réceptions ensuite). Aucune suppression
   automatique de webhook : s'il existe ou si conflit, arrêter.
6. Commandes proposées pour ce premier essai : /help et /status seulement,
   réponses figées/échappées <=2048 caractères, maximum 6 tentatives. Chaque
   admission et permission d'opération doit être durable avant progression.
   La restriction de commandes réelle reste à implémenter/revoir avant activation.
7. Arrêt au premier doute, dépassement, identité incohérente, réponse tardive,
   erreur de preuve/stockage ou révocation. Ni 429, ni redémarrage, ni uncertain
   n'autorisent un renvoi automatique ou une prolongation.
8. Arrêter/récolter, désarmer, décharger le secret, attester le checkpoint externe
   et révoquer le credential privé selon le plan approuvé. Rapport expurgé sans
   token, IDs personnels, contenu privé ou chemin sensible. Aucun effet News Telegram.

## Autorisations et preuves encore nécessaires — aucune demandée/exécutée ici

1. **Autorité/exclusivité** : approuver et qualifier un support/protocole externe
   réellement indépendant des images locales, son dernier checkpoint, sa gestion
   des permissions et le fencing d'un ancien hôte/secret. Sinon rester arrêté.
2. **Stockage Windows** : autoriser un emplacement privé précisément nommé et
   les seules créations nécessaires, qualifier ses ACL/parents/annexes ; puis,
   si Credential Manager retenu, autoriser un test d'entrée synthétique unique
   et son nettoyage. Aucune autorisation implicite de changer des ACL existantes.
3. **Enrôlement** : autoriser séparément, si nécessaire, une observation privée
   limitée pour vérifier manuellement bot/propriétaire/chat, sans envoi ni
   publication des candidats ; approuver les IDs localement.
4. **Credential réel dédié** : autoriser explicitement le fournisseur et la cible
   exacte de lecture/écriture, le transfert revu et le plan de révocation,
   jamais un credential de News Telegram ou une énumération du magasin.
5. **Transport réel** : autoriser son développement/revue puis sa qualification
   DNS/TLS/WAN, canal secret, délais globaux, arrêt d'urgence opérateur et réponse
   réelle -> commit B2. Les preuves de boucle locale ne remplacent pas cette étape.
6. **Essai réel final** : après toutes les preuves, approuver le SHA exact, hôte,
   génération, trial_id, checkpoint, méthodes, textes/commandes, plafonds
   300 s/20 lectures/6 réponses, préflight et opérateur d'arrêt. Toute modification
   invalide cette autorisation ; pas de réutilisation après crash/reboot.

Ces autorisations peuvent faire l'objet d'une revue future, mais la publication
ou fusion de C3 n'en accorde aucune. Aucun secret réel n'est demandé maintenant.

## Périmètre préservé

Aucun appel Telegram, credential/ID personnel réel, webhook/polling réel,
modification de state.json, watcher, workflows, production, sources, FIFO,
cadence de 15 s ou journal confirmé 1947. Aucun changement d'ACL, dossier nouveau
directement sous C:\, service, tâche, Pi, activation ou fusion. Les secrets
de credential synthétiques C3 restent en mémoire/pipe. Les autres données et
les certificats/clefs TLS synthétiques du banc C1 restent dans les seuls
TemporaryDirectory. Aucun token n'y est placé. L'autorisation C0 n'est pas réutilisée.
Le travail s'arrête après publication de la PR et du rapport C3.
