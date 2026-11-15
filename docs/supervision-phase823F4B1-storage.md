# F4-B1 — plan compte/stockage et autorisations futures

**Proposition uniquement. Aucun compte, répertoire cible ou ACL créé/modifié.**
Le [diagnostic runtime et la matrice GO/NO-GO](supervision-phase823F4B1-runtime.md)
font partie de la même livraison. Exploitation réelle et F4-B2 restent interdits.

## Schéma minimal proposé

Un compte Windows local dédié **NewsTelegramPrivate**, non administrateur,
un contrôleur et un lecteur. Nom proposé, pas un compte dont l'existence ou
l'absence a été recherchée ici. Le jour de l'autorisation : vérifier précisément
l'absence avant création ; s'il existe déjà, arrêter plutôt que le modifier.
L'opérateur conserve les actions d'administration nécessaires hors de cette
session ; aucun service, tâche, démarrage au boot ou compte cloud ajouté.

Sous la session de ce compte, définir R = `%USERPROFILE%\NewsTelegramPrivate`.
USERPROFILE doit être résolu localement en un chemin absolu du **nouveau compte**,
vérifié avec ses parents et inscrit dans la fiche approuvée. Ne pas supposer que
Windows donnera un nom de profil particulier. Si le chemin résolu diffère de
la fiche, ne pas créer ou adopter un autre dossier sans nouvelle vérification.

```text
R\
  runtime\cpython-3.15.2-amd64\   runtime privé à acquérir/qualifier sous autorisation
  venv\                          dépendances isolées, sans system-site-packages
  app\<SHA-approuvé>\             code privé versionné, aucun état dans le dépôt
  data\
    store\                       base, marqueur, verrou et journal B1/B2/C0
    fence\                       journal C2 local
    witness\                     témoin C2 séparé des sauvegardes du magasin
    trial\                       autorisation et compteurs C3
    control\                     ticket et résultats du worker protégés
    backups\                     au plus deux images cohérentes identifiées
    logs\                        codes et compteurs expurgés, rotation bornée
  fixtures\                      seules aires natives synthétiques autorisées
```

Cette arborescence est une proposition de cible, pas un argument permettant
de faire passer des chemins réels aux modules actuels de fixtures. Le futur
stockage opérationnel nécessite une interface approuvée. Pour les essais ACL,
réutiliser QualificationArea(parent=parent_approuvé) avec une nouvelle aire
`phase823-c0-<UUID>` sous fixtures, pas l'option historique qui crée sous C:.

Les trois emplacements C2 restent distincts, non imbriqués : data\store,
data\fence, data\witness. Séparer un témoin de la rotation des backups n'en fait
pas une ancre anti-rollback. Aucun clone, déplacement de chemins ou ancienne
base n'est adopté par création d'une nouvelle identité de confort.

Temp, workspace, LocalAppData et ProgramData comme racines de données privées
restent exclus : F4-A les a refusés comme feuilles. Le profil courant qui passait
ses ACL ne qualifie pas un futur profil. Aucun nouveau dossier sous C: ni
changement de parent existant n'est nécessaire à cette proposition.

## Propriétaire et droits attendus

Pour les seuls nouveaux dossiers vides autorisés, propriétaire attendu : SID
du compte dédié. DACL protégée de l'héritage ; seuls ce SID, SYSTEM et le groupe
Administrateurs de confiance reçoivent FullControl avec héritage OI/CI vers les
nouveaux enfants. Aucun Everyone, Users, Authenticated Users ou autre compte
dans les autorisations de la feuille. Les identifiants réels restent locaux.

Réutiliser les contrôles C0 existants sans élargir leurs exceptions. Avant toute
création de base, inspecter propriétaire/DACL de tous les parents, droits de
remplacement et suppression d'enfant, liens/réanalyses et héritage. Parent non
acceptable ou non lisible : refus ; ne pas le réécrire pour poursuivre.
Pas de prise de possession, privilège de restauration, changement SACL ou
suppression d'ACE sur un répertoire préexistant. Les modifications nécessaires
sur les seuls objets nouvellement créés doivent être explicitement autorisées.

Les fichiers naissent sous une ACL héritée sûre ; ne pas écrire du contenu puis
le protéger après coup. Inspecter ensuite chaque fichier et ses parents,
dont base, -journal DELETE présent pendant transaction, marqueur, verrous,
configuration, résultats, ticket, logs, backup et manifeste. Les dossiers peuvent
avoir leur DACL protégée ; les fichiers peuvent hériter des seuls droits sûrs.
Un -wal/-shm inattendu, hardlink, reparse, fichier substitué ou ACL dérivante
bloque l'opération, sans effacement ou correction automatique.

Un compte administrateur/SYSTEM de confiance n'est pas un bon sujet pour prouver
un refus tiers. Désigner un compte standard tiers réellement hors confiance et
approuver le lancement des seules sondes sur les fixtures. S'il n'en existe pas,
la création d'une identité de test supplémentaire requiert une autorisation
distincte. Aucun accès au profil ou aux données privées d'un autre compte.

## Isolation du runtime et de la publication

Le 3.15.2 actuellement enregistré appartient à un contexte utilisateur existant.
Il ne faut ni ouvrir ses ACL au nouveau compte, ni copier aveuglément son dossier
ou les paquets de publication. Une installation privée neuve, si nécessaire,
sera acquise sous autorisation avec identité du distributeur, hash, signature
et SBOM vérifiés avant usage ; le numéro TLS réellement chargé doit correspondre
au build retenu et maintenu. Aucune installation n'est faite en F4-B1.

Préférer l'installation utilisateur standard x64 avec GIL, dans le seul chemin
runtime approuvé, sans changement du PATH global, association de fichiers,
launcher par défaut, politique de sécurité ou environnement de publication.
Si un prérequis système exige une installation supplémentaire, arrêter et le
faire approuver séparément. Ne pas remplacer les DLL OpenSSL/SQLite du Python
existant ou employer un build free-threaded non qualifié.

Créer ensuite un venv neuf dans R\venv, sans system-site-packages, avec ses
propres dépendances épinglées. Utiliser les chemins absolus du Python du venv et
de son sys._base_executable. Le worker F2 doit continuer à lancer cette base
exacte en mode isolé : le launcher venv ne doit pas perdre le handle dupliqué.
Aucun chargement de .env, credential ou état du bot de publication.
Si un gestionnaire peut installer/mettre à jour automatiquement, ne pas le
laisser sélectionner une version lors du lancement d'exploitation.

La première qualification après installation reste synthétique : sondes stdlib,
suite complète du dépôt et tests natifs F2/F3 sous ce runtime et ce compte,
sans Telegram ou Credential Manager. Un échec ne permet ni de desserrer les
gardes de type, ni de revenir silencieusement au Python de publication.

## Budget de disque et rétention

Conserver les plafonds B1 existants : 100000 reçus, 1000 messages pending,
128 Mio maximum de base et 512 Mio pour son répertoire contrôlé. Une proposition
plus restrictive pour cette première cible est **database_bytes=64 Mio**, paramètre
déjà disponible et inférieur au maximum ; à confirmer par la qualification.
Ne pas changer les constantes globales ou les limites du bot de publication.

Budget proposé pour l'ensemble data : 512 Mio, couvrant base, journal, témoins,
trial, résultats, logs et au plus deux sauvegardes. Avec une base limitée à
64 Mio, deux images de même borne, journal et marge de travail doivent rester
dans ce budget ; vérifier les tailles réellement atteintes, pas un simple calcul
de pages SQLite. Logs proposés : au plus cinq fichiers de 1 Mio. Runtime, venv,
code et installateur ont un budget séparé à mesurer, pas une exemption illimitée.
Garder par proposition au moins 2 Gio libres sur le volume avant admission,
puis vérifier que cette marge suffit aux pics mesurés. Ce seuil est une proposition
d'exploitation, pas un contrôle déjà livré ni une réservation OS.

Les quotas B1 par répertoire ne constituent **pas un plafond agrégé automatique**
pour toute cette nouvelle arborescence. Son contrôle et son échec fermé doivent
être qualifiés avant activation, sans nouveau service de monitoring. Un simple
espace libre initial n'est pas une garantie de capacité future. Pas de purge de
reçus/tombstones, d'intention uncertain ou de checkpoint pour faire de la place.
Les journaux C2 bornés à 1000 événements/4 Mio par base s'arrêtent à saturation.

Sauvegarde par API SQLite cohérente sous verrou, worker arrêté et récolté ; aucune
copie du seul fichier principal pendant transaction. Destination neuve et absente,
identité/bot/génération/empreintes et intégrité vérifiés. Retirer une vieille
image seulement après validation de la nouvelle et autorisation de rotation sur
ces seuls objets identifiés. Une image ancienne reste désarmée : RESTORE_BARRIER,
recovery_required, prepared annulés et in_flight devenu uncertain, jamais une
permission de renvoi. Pas de restauration pour déplacer discrètement un état
dans un autre compte ou contourner retired.

## Plan de qualification ACL native à faire approuver

1. **Avant création** : approuver SID et chemin absolu résolu du compte cible,
   parent de fixture, UUID et inventaire prévu. Vérifier que compte et chemins
   destinés à une création n'existent pas ; collisions ou parents refusés = arrêt.
2. **Naissance sûre** : ne créer que les dossiers autorisés et encore vides.
   Appliquer la DACL restrictive sur ces nouveaux objets, puis relire toute
   la chaîne avant marqueur ou SQLite. Ne pas réutiliser C0_APPROVED_WINDOWS_FIXTURE.
3. **Essai positif** : ouvrir un magasin synthétique neuf sous la même identité,
   vérifier propriétaire/héritage et les fichiers annexes avant/pendant/après
   BEGIN IMMEDIATE, après arrêt, sauvegarde et restauration désarmée.
4. **Essais négatifs bornés** : sur un fichier ou sous-dossier synthétique nommé
   dans l'inventaire seulement, introduire une ACE Everyone/Read ou réactiver
   l'héritage ; exiger le refus. Ces mutations sont des ACL réelles de fixtures,
   donc nécessitent une autorisation explicite avant exécution. Ne pas appliquer
   ces essais aux parents ou objets existants.
5. **Accès tiers** : depuis l'identité standard approuvée, tenter lecture,
   remplacement, suppression et changement de DACL des seuls objets de fixture ;
   exiger les refus. Aucune authentification, collecte ou donnée réelle.
6. **Intégrité** : liens/hardlinks, substitutions de marqueur, schéma inconnu,
   journal inattendu, quota/espace simulé et échec de permissions doivent bloquer,
   conserver la base et les preuves et ne jamais créer une base vide de repli.
7. **Nettoyage** : vérifier volume/file IDs du conteneur et des enfants, UUID du
   marqueur, contenu synthétique et inventaire fermé, absence de liens. Supprimer
   seulement les objets dont la propriété d'essai est prouvée. Fichier inconnu,
   identité modifiée, accès refusé ou autre doute : arrêter sans suppression
   destructive et le signaler. Aucun dossier préexistant supprimé récursivement.

Ce nettoyage ne supprime pas automatiquement compte, profil, runtime, venv ou
futurs états d'exploitation. Leur éventuelle suppression/désinstallation demande
une décision distincte avec cibles et conséquences vérifiées. Les suppressions
logiques ne garantissent pas l'effacement physique du disque, pagefile ou dumps.

## Autorisations opérateur à demander, dans l'ordre

Ces demandes sont préparées, **pas posées ni exécutées dans F4-B1** :

| Ordre | Autorisation exacte future | Limite de portée |
| --- | --- | --- |
| A | Vérifier localement l'éligibilité/couverture ESU et classer les opérations différées ; proposer les remédiations nécessaires | Pas de changement Windows Update, licence ou reboot sans accord spécifique supplémentaire |
| B | Créer le seul compte local standard NewsTelegramPrivate s'il est absent et initialiser son profil par connexion manuelle | Mot de passe saisi dans un canal Windows approprié, jamais argv/env/log ; aucun compte cloud, groupe Administrateurs, service ou tâche |
| C | Approuver le chemin R résolu et créer uniquement cette nouvelle racine/ses nouveaux enfants avec DACL restrictive | Aucun parent existant modifié ; propriété vérifiée avant contenu ; noms/SID exacts conservés localement |
| D | Acquérir/vérifier le build officiel 3.15.2 x64 avec GIL retenu, puis installer uniquement dans R\runtime et créer R\venv avec dépendances propres | Aucun paquet/binaire de publication modifié ; paramètres PATH/associations/launcher global exclus ; toute exigence système imprévue bloque |
| E | Exécuter la suite/native et le plan ACL sur une seule nouvelle aire UUID sous R\fixtures avec identités synthétiques | Autoriser explicitement les mutations négatives et le nettoyage de cette seule aire après contrôle ; aucun ancien dossier C0 adopté |
| F | Si nécessaire, identifier/autoriser un compte standard tiers pour les sondes, ou approuver séparément sa création | Aucun accès à ses données ; aucune suppression ultérieure de compte implicite |
| G | Après résultats, arbitrer l'environnement et les opérations d'une nouvelle mission | Ne vaut pas autorisation Credential Manager, réseau distant ou essai Telegram |

Le SHA/version exact doit être réexaminé si une mise à jour de sécurité apparaît
avant acquisition. Aucune commande d'installation ou de mutation n'est exécutée
par la lecture de ces documents. Le runbook F4-A 300 s/20 lectures/6 réponses et
ses barrières reste applicable, sans autorisation réelle nouvelle.
