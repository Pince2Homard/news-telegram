# Phase 8.2.3-C0 — qualification préalable hors ligne

Base : `da2e3c13dfc76a391e13e685dfa9e107c3ee3961`, fusion de #30. Avant toute
modification, fusion et CI post-fusion
[43606390066](https://github.com/Pince2Homard/news-telegram/actions/runs/43606390066)
vérifiées avec succès. Aucune connexion Telegram, lecture de token, installation,
redondance, configuration réelle ou activation n'est autorisée par C0.

## Livrables et frontière de sécurité

`private_qualification.py` ajoute des primitives de fixtures : aire privée,
inspection ACL, magasin qualifié, sauvegarde cohérente, restauration désarmée
et mesure d'interruption d'un worker artificiellement bloqué. Aucun transport
HTTP, client API, tâche, service ou boucle de réception n'est ajouté.

Le verrou, les transactions, reçus, curseurs, réservations et outbox B1/B2 sont
réutilisés. Le magasin qualifié applique un schéma v3 ; les lecteurs B1/B2
refusent cette version au lieu de la rétrograder. Deux petits points
d'intégration sont nécessaires : inspection du bit de protection d'héritage
dans credential_acl, sans changer son validateur, et hook avant création des
fichiers B1 pour protéger le dossier avant toute donnée.

## Aire Windows expressément approuvée

Le propriétaire a autorisé une unique aire nouvelle sous
`C:\phase823-C0-\<identifiant aléatoire>`, exclusivement pour ces essais.
L'emplacement, absent avant création, est créé avec refus d'écrasement. Le
conteneur et l'aire interne appartiennent à cette seule session ; aucun dossier
préexistant n'est repris. Les dossiers de production, le profil, le dossier
Temp système, le workspace et leurs parents ne subissent aucun changement ACL.

L'essai exploratoire a constaté que les parents du Temp système et du worktree
accordaient des droits de mutation à des identités hors liste de confiance.
Le vérificateur a refusé ces chaînes ; aucune exemption n'a été ajoutée pour
les rendre acceptables. Une DACL privée sur un seul enfant ne corrige pas un
parent qui permet son remplacement. Ce constat explique le choix du nouvel
emplacement approuvé directement sous C:.

Préparation native du seul dossier nouveau et vide :

- Vérifier que le propriétaire actuel appartient à la liste existante de
  confiance : compte Windows exécutant, SYSTEM ou Administrateurs.
- Charger et persister uniquement la section DACL avec DirectorySecurity.
  Aucun changement du propriétaire, du groupe ou de la SACL ; pas de demande
  de privilège de restauration du propriétaire pour contourner un échec.
- Protéger la DACL contre l'héritage, retirer les anciennes ACE du nouveau
  dossier vide, ajouter FullControl héritable seulement pour ces trois identités.
- Relire les ACL effectives et valider la chaîne entière avec le validateur
  existant : propriétaire, DACL non nulle, absence de reparse, droits tiers et
  protection contre remplacement/suppression via les parents.
- Vérifier le bit AreAccessRulesProtected du dossier et les fichiers créés,
  notamment base, marqueur, verrou, journal DELETE, sauvegarde et manifeste.
  Aucun -wal/-shm inattendu n'est supprimé pour contourner un refus.

Les fichiers héritent de la DACL sûre avant leur contenu ; les temporaires
SQLite sont en mémoire. Les SID Windows sont indispensables au contrôle local,
mais ne figurent pas dans les rapports ; les identités Telegram sont uniquement
synthétiques. Le validateur conserve notamment son exception TrustedInstaller
strictement limitée au propriétaire de la racine du volume, sans l'élargir.

### Résultats Windows natifs

Exécution explicite avec `C0_APPROVED_WINDOWS_FIXTURE=1`, à ne pas répéter sans
une nouvelle aire autorisée :

- Répertoire privé, propriétaire de confiance, DACL, héritage et parents : validés.
- Journal SQLite présent pendant BEGIN IMMEDIATE : droits effectivement validés,
  avec base, marqueur et verrou ; contrôle aussi après sauvegarde/restauration.
- ACE Everyone/Read ajoutée sur un nouveau fichier de test : refus effectif.
- Héritage réactivé sur un nouveau sous-dossier vide de test : refus effectif.
- Seuls les objets négatifs créés par les essais, identifiés et vérifiés, sont
  retirés ; aucun dossier/fichier préexistant n'est modifié.

Avant le nettoyage final : identité du conteneur et de l'aire (volume/inode),
UUID du marqueur, identité des sous-dossiers, ACL, absence de liens et inventaire
fermé vérifiés. Un fichier inconnu, marqueur changé ou identité différente fait
échouer cleanup avec CLEANUP_REFUSED, sans suppression récursive. Les tests
vérifient effectivement ce refus. Le nettoyage n'est pas une récupération de base.

**L'aire Windows autorisée a passé ces contrôles puis a été supprimée. L'absence
finale de `C:\phase823-C0-` a été vérifiée.** Aucun droit de son parent C: n'a
été modifié. Une défaillance de suppression OS doit interrompre le nettoyage et
être signalée ; la suppression de fichiers ne constitue pas une transaction
annulable ni une garantie d'effacement physique des supports.

## Sauvegarde cohérente et restauration toujours désarmée

Sous le verrou du magasin, sans transaction source ouverte, l'API SQLite backup
copie un instantané cohérent vers un nouveau dossier de fixture. Pas de copie
isolée du fichier principal pendant une écriture. Le chemin de destination doit
être réservé dans l'aire approuvée et absent ; jamais d'écrasement.

Le magasin v3 ajoute `qualification(id, store_id, restored, parent_id)` au
schéma B2. La migration valide B2 avant le DDL, ajoute cette table et versionne
user_version dans BEGIN IMMEDIATE. L'identité locale de magasin est un UUID
injecté, distinct de la génération B2. Une identité ou génération incohérente
est refusée. Aucun reset ni perte des tables B1/B2.

La copie destinée à une sauvegarde est elle-même rendue inutilisable pour une
livraison avant publication du manifeste :

1. Mode DELETE/EXTRA et transaction de désarmement sur la copie uniquement.
2. Barrière restored=1, état de supervision recovery_required.
3. Toute intention in_flight devient uncertain, tentative concordante ; aucune
   intention, quota, preuve de déduplication ou confirmation n'est effacée.
4. Les outbox prepared de l'image deviennent cancelled. Une ancienne image ne
   peut pas renvoyer une réponse qui a été confirmée après la sauvegarde.
5. Contrôle d'intégrité, fermeture de l'image, empreinte SHA-256 et manifeste
   figé : version, source UUID, génération et statut désarmé. Aucune identité
   personnelle, token, commande ou chemin absolu dans le manifeste.

Une interruption pendant copie ou avant publication du manifeste laisse une
image non utilisable par restore_fixture ; aucun marqueur de succès n'est
inventé. La source conserve ses données et son curseur.

La restauration exige manifeste, empreinte, version, source et génération
concordants, et un autre dossier neuf. La copie cohérente reçoit un nouvel UUID
avec filiation vers la source ; la barrière et les états désarmés sont conservés.
Les reçus, curseurs, messages et résultats disponibles à la date de sauvegarde
restent présents. Les événements postérieurs à une sauvegarde ancienne ne sont
pas reconstruits ni prétendus récupérés.

`QualifiedStore` interdit alors préparation/livraison, réception et reprise
d'époque, même avec la permission simulée B2. Aucun réarmement n'est exposé.
Les lecteurs B1/B2 refusent le schéma v3 : ils ne peuvent pas servir de voie
normale de contournement. Un administrateur réécrivant ensemble version, données
et preuves reste hors de cette garantie ; il ne s'agit pas d'une protection
cryptographique contre l'administrateur du poste.

Rétention bornée : au plus deux images par aire, y compris les images incomplètes
encore présentes ; une troisième est refusée sans éviction. Les tailles de
magasin B1 restent bornées. Les fixtures ne sont ni synchronisées ni sauvegardées
dans le cloud. Une politique de rotation/externalisation d'installation réelle
devra être approuvée séparément ; C0 n'efface pas automatiquement les preuves.

## Intégrité, crashs et horloges

Les tests C0 vérifient : image ancienne préparée puis confirmation dans la source,
restauration interdite de renvoi ; sauvegarde d'une intention incertaine ; arrêt
de processus après intention ; crash pendant backup et avant manifeste ; base
corrompue, image modifiée, mauvais UUID ou génération, heure reculant ; absence
de réinitialisation et conservation des reçus/curseurs.

Les états confirmed restent confirmés, uncertain restent non rejouables et les
quotas ne sont pas effacés. Une restauration tardive ne redonne pas 120 secondes
à une commande. Le retour de status signale next_offset indisponible quand une
reprise serait dangereuse ; stored_offset reste une information diagnostique.

Les tests B1/B2 de crash autour des commits, disque saturé, migration, identité,
quotas, reprises 24 h/plusieurs jours et non-duplication sont conservés. Un fsync
accepté par SQLite ne démontre pas la résistance du disque ou du contrôleur à
une panne électrique ; une interruption de processus n'est pas une panne de courant.

## Exclusivité et basculement éventuel vers Raspberry Pi

Procédure proposée pour une future autorisation, non exécutée :

1. Tenir un registre privé de l'unique lecteur autorisé : hôte, bot numérique,
   UUID du magasin, génération et période d'autorisation ; aucun secret GitHub.
2. Vérifier humainement les autres clients et mécanismes de démarrage. Un verrou
   Windows protège seulement les processus coopérants sur ce même stockage.
   Ni un fichier copié, ni un heartbeat absent ne prouvent l'exclusion distante.
3. Avant changement d'hôte, arrêter l'ancien lecteur, vérifier son arrêt et le
   clôturer ou le mettre hors réseau. Si son arrêt ne peut pas être prouvé,
   **ne pas promouvoir** l'autre hôte ; pas de compétition d'offset.
4. Transporter une sauvegarde cohérente désarmée, vérifier l'historique et les
   intentions ambiguës, qualifier les permissions POSIX et le stockage du Pi.
   Les ACL Windows ne constituent pas une qualification du système distant.
5. Une future procédure de promotion devra émettre une nouvelle autorisation et
   génération après réconciliation. Le réarmement d'une copie restaurée n'existe
   pas dans C0. L'ancien hôte ne redémarre jamais sur une image ou génération ancienne.
6. Lors d'une future vérification autorisée, webhook présent, conflit de lecteur
   ou suspicion d'acquittement distant => arrêt désarmé. Ni purge, suppression de
   webhook, rotation de token ou basculement automatique.

Aucune redondance, lease distribuée, connexion au Pi, installation ou copie de
credential n'est implémentée. Disponibilité et basculement restent des travaux
ultérieurs ; la sûreté prime sur la disponibilité en cas d'exclusivité douteuse.

## Budget global réellement interruptible : contrat avant C

Le timeout socket existant ne borne pas toutes les résolutions DNS ou lectures
lentes. Le contrat proposé est un worker réseau isolé du magasin, sous contrôle
d'un parent détenant le verrou et l'intention durable. **Il n'est pas implémenté
comme transport dans C0.**

- Échéance monotone unique fixée avant lancement : 20 s pour une sortie, 25 s
  pour une réception incluant au plus 10 s de long poll. Ce sont des propositions
  à approuver, sans cumul de budgets ni remise à zéro par phase/retry/redirect.
- Chaque phase reçoit min(plafond local, temps restant) : DNS, TCP/TLS, écriture,
  en-têtes, corps et validation finale partagent cette même échéance.
- Sous Windows, créer le worker suspendu, l'affecter à un Job Object avec
  kill-on-close avant reprise, transmettre uniquement les handles de pipes
  nécessaires. Aucun handle SQLite, chemin de magasin ou secret en arguments,
  environnement ou logs. Pas de processus enfant libre ou lancement de shell.
- À échéance ou révocation : TerminateJobObject, attendre la fin du worker dans
  une marge de récolte bornée, ignorer les réponses tardives. La mort du parent
  doit fermer le job et empêcher un worker orphelin de poursuivre.
- Si le job ne peut pas être affecté, interrompu ou récolté, arrêter sans
  nouveau worker. Une intention possiblement émise devient uncertain ; une
  annulation ne prouve jamais l'absence d'acceptation distante.

La qualification C0 lance seulement un programme de fixture qui annonce un
nom de phase puis se bloque artificiellement, sans socket. Pour dns/tls/read/write,
le contrôleur mesure une échéance réelle monotone, tue le processus et confirme
sa fin ; le test vérifie que la phase avait commencé et borne le délai observé.
Cela prouve l'interruption d'un processus de fixture, **pas** celle d'une future
pile HTTP/DNS ni le kill-on-close d'un Job Object. La qualification de ce job,
de la mort du parent, des canaux secrets et des stalls système reste un verrou
avant C. Le contrôleur n'est pas un ordonnanceur temps réel : latence OS et
paquets déjà confiés au réseau doivent rester compatibles avec l'état uncertain.

## Enrôlement et plan d'essai futur, limité et révocable

Après autorisation distincte seulement : ouvrir le bot attendu depuis la session
Telegram de confiance, observation locale bornée et défi ponctuel privé, revue
manuelle de la paire numérique from.id/chat.id et de l'ID numérique du bot.
Épingler ces valeurs hors dépôt avec génération, UUID de magasin et SHA approuvé.
Pas d'enrôlement au premier /start, de confiance fondée sur un pseudonyme, de
capture/ID personnel dans GitHub ni de bot tiers de découverte. Configuration
absente ou non vérifiée = arrêt fermé. Aucun ID réel n'est demandé en C0.

Le futur plan soumis à approbation doit préciser :

- SHA et hôte uniques, identité privée vérifiée localement, version du magasin,
  permissions/stockage qualifiés et condition de révocation accessible au propriétaire.
- Méthodes précisément autorisées, absence de webhook et exclusivité vérifiées
  sans reconfiguration implicite, politique du stock ancien, curseur et refus.
- Une session manuelle au plus 5 minutes, 20 lectures et 6 réponses ; quotas
  B2 persistants, fenêtre 120 s, aucune rafale ou reprise automatique incertaine.
- Un budget distinct pour les vérifications d'identité, sans extension de session
  pour finir un backlog ; une autorisation explicite avant tout accès credential/API.
- Révocation, dépassement, doute d'identité/lecteur, résultat ambigu, problème
  ACL/disque/horloge : arrêt, worker interrompu, conservation des preuves,
  prepared annulés selon la procédure et aucune réactivation automatique.

Le fichier du journal confirmé 1947 n'intervient jamais dans cette procédure.
Ni cette documentation ni la qualification de fixtures n'autorisent un essai réel.

## Validation, preuves et limites restantes

Suite Windows avec activation explicite des trois tests natifs : **316 tests,
OK, 1 test POSIX ignoré**. Les trois tests ACL natifs ont réussi, avec restauration
désarmée native et suppression de l'aire confirmée. Les autres tests C0 Windows
utilisent des métadonnées ACL synthétiques pour isoler les transactions et les
cas négatifs ; ils ne sont pas présentés comme preuves ACL natives. Sous Linux,
CI vérifie les modes privés et les comportements transactionnels ; elle ne
qualifie pas Windows. Ruff, compileall et pip-audit font partie des contrôles.
Le contrôle final de non-régression compte également 316 tests, OK avec 4
ignorés : les trois essais natifs ne sont pas relancés après suppression de
l'unique aire autorisée, et le test POSIX reste ignoré sous Windows. La seule
vérification fonctionnelle ajoutée après l'essai natif refuse une annexe SQLite
inattendue dans une sauvegarde ; le code ACL qualifié est inchangé. Les essais
d'horloge injectent les dates : aucune modification de l'horloge Windows.

```text
python -m unittest discover -s tests -v
python -m ruff check --no-cache .
python -m compileall -q poster.py cadence_simulator.py newsbot tools tests
python -m pip_audit -r requirements.txt -r requirements-dev.txt --progress-spinner off
```

L'option native Windows n'est pas activée automatiquement en CI et n'autorise
aucune nouvelle création à la place d'un dossier préexistant. Les preuves
observées ici ne qualifient pas un emplacement de production ou ses ACL réelles.
Les outils ne journalisent pas de secret ; une future interface opérateur devra
conserver l'expurgation des erreurs OS et ne pas publier de chemins personnels.

Restent bloquants avant C réel : autorisation spécifique, qualification du
répertoire réel sans toucher à la production durant C0, sauvegarde et plan de
réconciliation opérationnels, dispositif d'identité indépendant contre les
restaurations/clones hors API, exclusivité distante, enrôlement, adaptateur privé
HTTPS et Job Object globalement interruptible qualifiés. Pas de preuve exactly-once
distante, de résistance à un administrateur compromis ou de durabilité matérielle
universelle. Aucune activation, fusion ou Phase 8.3–8.5 dans cette livraison.
