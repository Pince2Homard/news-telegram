# C4 — arbitrage avant premier essai privé

## Décision proposée : NO-GO actuel

Recommander **B, une autorité technique externe minimale**, si l'exigence reste
la résistance à la restauration coordonnée/clonage avec permissions consommées
avant les opérations. Ne pas la développer ni la déployer dans C4. L'autorité
humaine A reste nécessaire pour approuver et arrêter, mais son registre ne
constitue pas aujourd'hui une preuve technique de sérialisation.

Si le coût de B est jugé excessif, rester hors ligne. Choisir A demanderait un
nouvel arbitrage explicite du modèle de menace et une démonstration de son
protocole ; ne pas convertir une exigence non satisfaite en risque simplement
« accepté » pour obtenir GO. Une session unique de 300 s sans reprise réduit
l'exposition, sans résoudre à elle seule clonage, double consommation ou ancien
worker vivant. Aucun exactly-once distant n'est démontré par A ou B.

C4 est exclusivement documentaire. Préconditions vérifiées avant modification :
#34 fusionnée au SHA `bf46219ab17af3fd8f18d0ceddf3d8414242506b`,
[CI post-fusion 43634970280](https://github.com/Pince2Homard/news-telegram/actions/runs/43634970280)
terminée avec succès ; dernier origin/main récupéré à ce SHA, branche C4 dédiée,
checkout initial propre. Aucun composant fonctionnel modifié.

## A / B : comparaison concrète

| Dimension | A — autorité humaine indépendante | B — autorité technique externe |
| --- | --- | --- |
| Confiance | Opérateur unique, registre conservé hors PC/sauvegardes et accès physique maîtrisé ; pas de copies concurrentes | Service indépendant, authentification, état sérialisé durable, administration et sauvegardes séparées du PC |
| Permission | L'opérateur note une permission consommée AVANT une opération et refuse sa réutilisation | Transaction atomique compare génération/checkpoint/quota/état, consomme un identifiant unique et le rend non réutilisable |
| Restauration accidentelle | Registre externe permet de constater un désaccord seulement si l'opérateur consulte réellement son dernier état | Ancien checkpoint local refusé lors d'une validation fraîche auprès du service |
| Restauration frauduleuse | Faux dernier registre, copie, signature ancienne, opérateur trompé ou complice : pas de protection démontrée | Administrateur/service compromis ou rollback de l'autorité elle-même : sécurité perdue ; sa restauration exige une ancre indépendante ou un protocole de désarmement prouvé |
| Double traitement | Vérification manuelle avant chaque étape, mais aucun mécanisme actuel ne lie irrévocablement la permission à une seule exécution Windows/clonée | Consommation unique d'une intention ; une réponse perdue ne permet jamais de réattribuer cette intention. Déduplication B1/B2 conservée |
| Lecteur exclusif | Présence physique et inventaire opérateur ; ne prouvent pas l'absence d'un clone ou détenteur distant du secret | Un propriétaire de génération à la fois ; un ancien détenteur doit aussi être réellement empêché d'agir, pas seulement marqué expiré |
| Sans réseau | Registre humain consultable hors ligne ; aucune preuve automatique ne naît de cette disponibilité | Autorité injoignable = aucune nouvelle permission ; pas de lot de permissions hors ligne ou de cache réutilisable |
| Panne / reboot | Annuler l'essai, conserver les preuves, ne pas reprendre ; erreur humaine de réarmement reste possible | Essai non repris ; réconciliation avec état externe, ancienne génération bloquée, intentions ambiguës terminales |
| Complexité | Peu de logiciel, protocole humain coûteux à répéter correctement pour 20 lectures/6 réponses et checkpoints | Petit service transactionnel mais vrais problèmes d'identité, durabilité, disponibilité, fencing et exploitation |
| Coût et maintenance | Temps d'opérateur à chaque opération, conservation et contrôle du registre ; pas de coût chiffré établi | Hébergement/stockage indépendants, supervision, correctifs, certificats et exercices de récupération ; aucun devis ou fournisseur choisi |
| Dépendances | Opérateur disponible, registre unique et procédure vérifiable, canal d'arrêt | Réseau vers autorité, authentification, horloges/budgets, stockage indépendant et mécanisme de fencing effectif |
| Limite actuelle | Protocole non démontré ; aucune garantie technique de sérialisation | Aucun service ni protocole de consommation/fencing/restauration qualifié dans le dépôt |

### Contenu minimal de B à spécifier séparément

Un seul essai et un seul propriétaire Windows actifs, sans redondance Pi.
L'état externe doit identifier essai, génération, hôte, SHA approuvé, dernier
checkpoint, opérations déjà consommées, compteurs et révocation terminale.
Les IDs privés et secrets restent hors des rapports publics ; ne pas stocker le
token dans l'autorité si elle n'en a pas besoin.

Une opération compare atomiquement le dernier état attendu et consomme une
permission unique AVANT l'accès Telegram. Une requête répétée de validation
ne doit jamais produire une seconde permission d'exécuter : après perte de la
réponse d'autorisation, abandonner l'opération ou prouver sa non-exécution, sans
rejouer une autorisation issue d'un cache. Le résultat/checkpoint est enregistré
après commit local ; toute fenêtre intermédiaire inconnue bloque la suite.
Surcompter une tentative abandonnée est admissible ; accorder une tentative
supplémentaire ne l'est pas.

**Un service qui délivre des tickets ne suffit pas.** Un ticket cloné peut être
présenté deux fois par deux copies du PC. La liaison à l'exécution unique et
l'arrêt de l'ancien détenteur doivent être prouvés : contrôle effectif de la
capacité réseau/credential, ou autre mécanisme de fencing qualifié. Si cette
preuve exige un intermédiaire réseau, en faire une proposition séparée avec
son coût et son périmètre secret ; ne pas ajouter discrètement un proxy.
Une expiration de bail n'annule pas une requête déjà partie. Une opération
acceptée à distance puis perdue demeure uncertain, même si B sait qu'elle a
consommé une seule permission.

La qualification doit inclure partitions, validations simultanées, permission
rejouée/clonée, réponses perdues, restauration de l'autorité et PC ancien encore
vivant. Sa restauration ne doit jamais réhabiliter un essai révoqué. Sans preuve
de son état le plus récent, arrêt fermé. B est donc une recommandation de
conception ciblée, **pas une garantie acquise ou une autorisation de service**.

## Windows cible : fiche à qualifier avant toute création réelle

- Un compte Windows dédié, sans privilège administrateur pour l'exploitation,
  utilisé par le seul contrôleur approuvé. Pas de session interactive hostile,
  plugin arbitraire ou processus concurrent susceptible d'hériter des handles.
  Administrateur, système ou même compte compromis restent des menaces à traiter
  explicitement, pas des adversaires contenus par DPAPI.
- Un volume local NTFS, hors dépôt, Temp, synchronisation cloud et chemins de
  production. Nom/emplacement exacts à faire approuver plus tard ; aucun chemin
  utilisateur réel n'est recueilli ici. Vérifier owner, DACL non nulle,
  héritage maîtrisé et droits minimaux du compte, SYSTEM et administrateurs
  autorisés. Aucun autre principal ne doit pouvoir lire/modifier/remplacer les
  fichiers ou supprimer/remplacer un parent. Inspecter toute la chaîne de
  parents, reparse points et liens ; ne jamais assouplir une ACL refusée.
- Même protection pour config, identité, génération, base, verrou, journaux,
  checkpoints, sorties et fichiers SQLite auxiliaires, y compris journal DELETE
  transitoire et éventuels WAL/SHM inattendus. Conserver DELETE/EXTRA,
  BEGIN IMMEDIATE, contrôles d'intégrité et quotas existants ; mode inattendu =
  refus. Les mesures sur fixtures ne prouvent pas la résistance à une coupure
  électrique du matériel cible ou aux caches disque.
- Journaux à codes fermés, sans token, IDs privés ou corps ; bornage/rétention
  sans suppression de preuves de déduplication. Sauvegardes cohérentes C0,
  protégées à destination et toujours restaurées désarmées. Comparer le
  checkpoint externe avant toute permission ; une sauvegarde récente n'est pas
  une preuve suffisante du dernier état.
- Credential Manager : candidat CRED_TYPE_GENERIC, cible dédiée précisément
  épinglée au bot privé, portée CRED_PERSIST_SESSION à qualifier sous le compte
  prévu. Pas d'énumération, wildcard, repli vers News Telegram ou entrée réelle
  créée/lue dans C4. Une fin d'essai doit supprimer/décharger sa capacité locale
  même si la session Windows reste ouverte ; une révocation fournisseur est
  distincte. La portée session n'est ni l'échéance de 300 s ni un anti-rollback.
- Reboot/crash/restauration : démarrage désarmé, aucun service/tâche/autorun,
  contrôle du dernier état externe, pas de reconstruction optimiste de handles
  ou d'échéance monotone. Checkpoint perdu ou génération restaurée = NO-GO.

## Revue des interfaces C1/C2/C3, sans correction fonctionnelle

| Interface / code relu | Preuve disponible | Écart et suite séparée nécessaire |
| --- | --- | --- |
| private_credential → private_fenced_job | Secret synthétique, HANDLE_LIST limité, JOB_LIST atomique, processus suspendu avant transfert | Handle de lecture temporairement héritable dans le parent ; whitelist de cet enfant ne contrôle pas un autre CreateProcess concurrent avec héritage large. Qualifier contrôleur dédié et discipline globale de création ; sinon corriger séparément |
| private_local_worker | Lecture bornée, effacement du buffer maîtrisé, handle fermé avant réseau local | Aucun credential réel accepté ; future interface réelle et portée de tous les handles descendants à revoir |
| private_trial → CredentialFixtureJob | Quota consommé avant tentative, deadline native minimale, un job actif dans la fixture | Pas de transaction atomique commune quota/intention/autorité externe ; crash peut surcompter. Rollback local coordonné non empêché ; pas de permission réelle |
| private_integration → B1 | Contrats puis commit reçu/message/curseur, rejet minimal compatible, authentification avant chargement de vue | Le futur acquittement distant doit rester strictement après commit ; aucun acquittement réel qualifié |
| B2 deliver | Intention commit avant échange fermé ; résultat et outbox commit ensemble ; unicité de tentative | Accepte uniquement ScriptedTransport. Le hook réseau local C3 ne constitue pas un adaptateur de résultat réel vers B2 |
| C0 / C2 | Restauration désarmée, génération, double journal et preuve OS par handles conservés | Handles perdus au reboot : preuve automatique absente ; deux journaux restaurés ensemble ne prouvent plus le dernier état |
| C3 état d'essai | disabled par défaut, terminal après fermeture/révocation, actif rouvert → recovery_required | /help et /status ne sont pas encore imposés comme whitelist d'essai : le routeur existant conserve ses six commandes. Restriction réelle à implémenter et tester séparément |
| Arrêt / mémoire | Terminaison job et ActiveProcesses=0, root signalé avant arrêt sûr ; buffers détenus effacés | Pas de preuve d'effacement de copies Python/DPAPI/kernel/pagefile/dumps ; ni UI opérateur réelle ni test DNS/TLS/WAN |

### Succès de transport et confirmation durable

Ni HTTP 200, ni JSON ok, ni sortie normale du worker ne suffisent. Il faut une
réponse authentifiée/strictement validée, cohérente avec destination et texte
figés, puis le commit du résultat dans attempts ET outbox. Si l'acceptation
distante est possible mais la réponse ou le commit local est perdu, garder
l'intention in_flight pour sa conversion en uncertain, sans renvoi.
Si le résultat avait été commit puis le contrôleur est mort, relire confirmed
et ne pas envoyer de nouveau. Les tests C3 valident le retour local puis
utilisent encore le modèle ScriptedTransport pour le commit B2 : ils ne prouvent
pas cette jonction pour une vraie réponse Telegram.

Corrections/qualifications indispensables à proposer sous autorisation séparée :
fournisseur réel ciblé et cycle de secret, protection globale de l'héritage,
transport DNS/TLS/WAN interruptible, jonction résultat réel/commit B2, whitelist
d'essai /help et /status, interface opérateur d'arrêt, autorité B et fencing.
Ne pas remplacer les gardes synthétiques ni élargir ScriptedTransport dans C4.

## Matrice des risques résiduels

Tous les risques marqués bloquants interdisent GO tant que leur preuve manque.

| Risque | Décision / preuve exigée | Statut C4 |
| --- | --- | --- |
| Restauration/clonage des permissions, quotas et magasin | Dernier état indépendant + consommation unique liée à exécution + fencing | Bloquant |
| Autorité elle-même restaurée, indisponible ou compromise | Protocole de récupération monotone et de révocation ; indisponible = aucune opération | Bloquant |
| Ancien lecteur vivant / requête en vol | Fin prouvée et révocation externe des capacités anciennes ; requête ambiguë jamais rejouée | Bloquant |
| Reboot et handles perdus | Annuler l'essai ; procédure indépendante de fencing/réconciliation prouvée avant nouveau projet d'essai | Bloquant |
| Pipe hérité par un processus concurrent | Contrôleur dédié et héritage global maîtrisé ou correction qualifiée | Bloquant |
| ACL, parents, backups et credential cible non qualifiés | Preuves sur emplacement et compte exacts, essais d'accès refusé et restauration désarmée | Bloquant |
| DNS/TLS/WAN et secret réels | Validation du transport, destination/TLS fixes et budget interruptible sur Windows | Bloquant |
| Succès reçu mais commit perdu | Adaptateur de confirmation durable testé ; uncertain terminal et aucune réémission | Bloquant |
| Commandes supplémentaires / arrêt humain absent | Whitelist imposée et canal opérateur testé sur worker bloqué | Bloquant |
| Mémoire résiduelle, dumps et compte compromis | Périmètre des buffers démontré, politique cible de dumps/pagefile et modèle de menace revus ; aucune promesse d'effacement total | Revue de sécurité nécessaire, GO interdit sans décision explicite |

## Procédure manuelle proposée, à qualifier puis autoriser

### Avant la fenêtre

1. Toutes les lignes bloquantes doivent être résolues par des preuves revues,
   avec SHA exact et configuration désactivée. Un seul PC Windows et bot privé
   distinct, un seul lecteur, aucun ancien détenteur distant/clone autorisé.
2. Opérateur présent, compte/emplacement qualifiés, entrée dédiée et IDs bot,
   propriétaire et chat validés manuellement. Aucun apprentissage depuis /start.
3. Autorité consultée fraîchement : hôte, génération, essai unique, checkpoint,
   quotas et intentions concordants ; uncertain reste terminal. Ne pas charger
   le token si les autres préconditions échouent. Token présent ≠ autorisation.
4. Permission humaine explicite, nominative localement, révocable et liée au
   SHA/hôte/génération/essai ; pas de boot, CI ou simple ouverture déclenchant
   l'essai. Autorisation distincte pour le canal secret et les accès API réels.

### Pendant la fenêtre autorisée ultérieurement

Maximum 300 s dès autorisation, 20 lectures et 6 **tentatives** de réponse,
jamais 6 seuls succès. Proposition conservatrice : inclure une vérification
d'identité et une de webhook dans les 20 lectures (reste au plus 18 réceptions),
avec budgets séparés approuvés, tous inclus dans les 300 s. Aucun webhook
modifié ; webhook présent ou conflit = arrêt. Ces appels ne sont pas exécutés C4.

Avant chaque opération : validation/consommation externe unique et quota local
durable ; une seule opération en vol. Reçus durablement enregistrés avant
progression/acquittement. Seules /help et /status pourront produire une réponse
de l'essai ; les autres commandes doivent être refusées par une garde testée.
Texte figé/échappé borné, destination privée épinglée, pas de commande système.

Arrêter immédiatement sur identité/config modifiée, 409/concurrent, perte de
l'autorité, incohérence, panne de stockage, quota, délai, résultat tardif,
révocation ou doute. Aucun retry automatique, même pour une intention incertaine
ou une permission dont la réponse de validation s'est perdue.

### Action opérateur et vérification d'arrêt

L'interface réelle reste à fournir séparément : commande locale dédiée
« ARRÊT ESSAI », accessible pendant wait bloquant et qualifiée sur Windows.
Elle doit fermer l'admission de permissions, demander TerminateJobObject,
attendre job vide et root signalé, fermer les handles/secret puis persister
revoked et notifier l'autorité. Une écriture disque bloquée ne doit pas retarder
la demande de terminaison. Le nom du bouton ou un code retour seul ne prouve rien.

Si le contrôleur ne répond pas : l'opérateur annule immédiatement toute nouvelle
permission auprès de l'autorité et utilise le dispositif indépendant de fencing
préqualifié. Un outil d'arrêt indépendant devrait détenir les handles capturés
du bon contrôleur/job avant incident ; ce n'est pas un second lecteur ni un
orchestrateur de secours. Il doit terminer le job et attendre ActiveProcesses=0
ainsi que le contrôleur signalé. Conserver un handle de job retarde kill-on-close :
il faut effectivement terminer le job. Un taskkill par PID seul ou l'absence de
fenêtre ne constitue pas la preuve exigée. Aucun tel outil n'est déployé C4.

Vérification finale : admission révoquée extérieurement, job vide et handles
signalisés, fermeture du canal secret et du fournisseur local, checkpoint
concordant après fermeture du magasin, tentatives terminales conservées.
La révocation du token privé chez son fournisseur, par une action humaine
distinctement autorisée, est un fencing supplémentaire ; elle ne prouve pas
qu'aucune requête antérieure n'avait déjà été acceptée.

Si les handles ont été perdus, le poste redémarré ou l'arrêt ne peut être
prouvé, **ne pas déclarer un arrêt sûr**. Révoquer les permissions externes,
couper la capacité d'action ancienne par le mécanisme approuvé, classer l'essai
interrompu et rester NO-GO. Aucune nouvelle génération ni reprise de backlog.

### Après l'essai ou l'incident

Essai unique terminé, jamais repris. Conserver received/offset/outbox, uncertain,
retired et recovery_required ; ne pas effacer un drapeau pour repartir. Analyser
hors ligne après terminaison prouvée, sauvegarder désarmé et produire un rapport
expurgé. Toute nouvelle tentative d'essai nécessite de nouvelles preuves et une
nouvelle autorisation ; elle ne réémet aucune intention ambiguë ancienne.

## Preuves acquises et manquantes

Sources locales : [conception A](supervision-phase823A-design.md),
[C1](supervision-phase823C1.md), [C2](supervision-phase823C2.md),
[C3](supervision-phase823C3.md) et sources privées relues ci-dessus.
Les résultats hérités C3 sont 386 tests, quatre exclus sur Windows, dont 22 tests
C3 et dix natifs Windows réussis ; CI Linux verte avec 36 exclusions.
Ce sont des preuves historiques de fixtures sur l'hôte de qualification, pas une
preuve de configuration du futur compte/emplacement ni de réseau Telegram.

Acquis : Job Object réel, création atomique C2, descendants, handles conservés,
timeouts/retards loopback, DPAPI synthétique, pipe, quotas 300/20/6, restauration
C0 désarmée, reçus minimaux et absence de nouvelle tentative incertaine en fixture.
Simulés : reboot logique après crash, états restaurés, identités, certaines fautes
disque/horloge et ACL d'intégration. Non acquis : autorité externe, résistance au
rollback de cette autorité, fencing de clones, Credential Manager, ACL cible,
reboot physique, UI d'arrêt, whitelist d'essai réelle et jonction transport réel.

C4 ne crée aucun nouveau test fonctionnel et ne réexécute aucune qualification
native nécessitant des permissions nouvelles. Contrôler le diff documentaire,
l'absence de secrets/IDs réels/chemins personnels et les CI du SHA publié ;
leurs résultats seront joints à la PR. Aucun succès CI documentaire ne ferme
un des écarts de sécurité ci-dessus.

## Autorisations à demander, dans cet ordre

1. Arbitrage A/B et modèle de menace : approuver la recommandation B ou demander
   une autre démonstration ; maintien du NO-GO dans l'intervalle.
2. Autoriser séparément la spécification/qualification de l'autorité et du fencing,
   de leur récupération et de leurs permissions ; déploiement seulement après
   revue concrète et autorisation distincte, aucun serveur implicitement permis.
3. Approuver compte/emplacement/ACL et cible Credential Manager précis ; autoriser
   les seuls tests synthétiques et créations nécessaires, puis revoir les preuves.
4. Autoriser les corrections privées listées, leur intégration hors ligne, l'arrêt
   opérateur et le traitement succès/commit ; revoir leurs PR sans activation.
5. Autoriser, si nécessaire, une observation d'enrôlement limitée, puis valider les
   IDs localement. Autoriser le fournisseur/cible exacts du credential réel dédié,
   son chargement et sa révocation ; aucune permission sur News Telegram.
6. Après satisfaction de tous les critères, autoriser la qualification distante
   précisément bornée puis le premier essai final : SHA, hôte, génération,
   checkpoint, méthodes, 300 s/20 lectures/6 tentatives, /help et /status, opérateur
   et procédure d'arrêt. Aucun cumul implicite avec les autorisations précédentes.

Ces demandes sont une liste à examiner ultérieurement, pas des questions ou
permissions accordées/exécutées pendant C4.

## Porte de décision GO / NO-GO

GO seulement si **tous** sont démontrés et approuvés : autorité indépendante
anti-rollback et consommation liée à une seule exécution ; exclusivité/fencing
de l'ancien détenteur ; compte/ACL/stockage/credential exacts ; enrôlement manuel ;
transport réel DNS/TLS/WAN interruptible ; confirmation durable et absence de
réémission ; quotas/whitelist ; arrêt opérateur et preuve d'arrêt ; restauration
désarmée ; SHA et autorisation finale explicites.

Critère absent, seulement simulé là où une preuve réelle est nécessaire, douteux
ou non approuvé = **NO-GO**. Un avertissement, une signature sur un vieux registre,
un test Linux, un token disponible ou la fusion de C4 ne remplace aucun critère.
Aujourd'hui plusieurs critères manquent : **NO-GO, activation réelle interdite**.

## Périmètre

Un seul nouveau document dans cette PR. Aucun accès Telegram, credential/ID privé
réel, ACL, Credential Manager, emplacement de production, state.json, workflow,
FIFO, cadence, source, watcher ou journal confirmé 1947 modifié. Aucun service,
tâche, Pi, autorité déployée, correction fonctionnelle, activation ou fusion.
Arrêt du travail après publication et rapport.
