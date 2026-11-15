# F4-B2A — protocole futur de qualification isolée

**Plan non exécuté. Autorisation opérateur distincte indispensable.**
Ce plan qualifie le runtime existant sous l'identité actuelle sans toucher au
bot de publication. Il ne remplace pas la future qualification compte/stockage
F4-B1 et ne lance pas F4-B2B. Voir le
[rapport de compatibilité](supervision-phase823F4B2A-compatibility.md).

## Objets et limites à approuver avant toute création

Proposition minimale pour un diagnostic synthétique : un seul conteneur neuf
**Q = `%USERPROFILE%\NewsTelegramPrivate-Qualification-<UUID>`**, sous le profil
du compte opérateur retenu. Résoudre le chemin absolu et l'approuver localement ;
aucun chemin personnel ne doit entrer dans le rapport public. Jamais C:\,
Temp, workspace ou répertoire applicatif de production comme cible privée.

Vérifier l'absence de Q et chacun des objets à créer ; collision = arrêt, pas
d'adoption ni de nettoyage d'un dossier préexistant. Contrôler NTFS, propriétaire,
DACL et chaîne des parents, liens/reparse/hardlinks, espace libre et identité.
Si une protection suffisante exige de modifier un parent existant : arrêter.
La validation positive de l'ancien profil F4-A ne vaut pas validation de Q.

| Objet neuf proposé | Usage borné |
| --- | --- |
| Q\ownership.json | UUID, SID local, volume/file ID du conteneur, inventaire des créations, SHA source ; aucun secret |
| Q\src\ | Export des seuls fichiers source/tests/configurations approuvés du SHA livré ; aucune donnée de production |
| Q\venv\ | Environnement 3.15.2 neuf, sans system-site-packages, ni lien vers les paquets 3.12 |
| Q\wheelhouse\ | Roues exactes approuvées et vérifiées ; pas de cache global partagé |
| Q\manifests\ | Lock Windows x64/GIL, hashes, métadonnées de provenance, inventaires avant/après |
| Q\fixtures\ | Fixtures synthétiques UUID ; store/fence/witness/trial/control distincts dans chaque aire |
| Q\reports\ | Résultats expurgés, versions, statuts, preuves d'arrêt ; aucun argv/env/dump de secret |
| Q\tmp\ | Fichiers temporaires bornés de ces seuls processus de qualification |
| Q\build\ et Q\build-venv\ | Seulement si construction source séparément autorisée, sinon absents |

Ne créer aucun compte ni entrée Credential Manager. Pour qualifier une DACL
native, demander une autorisation visant **ces seuls objets neufs**, propriétaire
du compte opérateur, DACL protégée limitée à ce SID/SYSTEM/Administrateurs,
héritage OI/CI sur les enfants. Ne pas modifier les parents, SACL, privilèges ou
ACL préexistantes. Les essais négatifs introduisant une ACE dangereuse exigent
leur propre périmètre précis ; jamais C0_APPROVED_WINDOWS_FIXTURE sur la racine C:.
Sans ces autorisations, garder les trois tests ACL natifs exclus et le stockage
cible NO-GO ; une fixture temporaire ordinaire ne devient pas un stockage sûr.
Ces trois tests appellent actuellement approved_windows_fixture(), qui vise
un conteneur directement sous C:. Ne pas activer leur variable d'environnement
pour les faire exécuter sous Q : elle ne change pas ce chemin. La future preuve
ACL sous Q demandera un banc ciblé utilisant QualificationArea(parent=...) et
les contrôles existants, autorisé et revu séparément ; aucune adaptation faite ici.

## Séquence reproductible après autorisation seulement

1. **Épingler les entrées.** Arrêter le SHA testé, l'identité Windows, le chemin
   absolu du runtime déjà présent et Q. Relever les hashes/signatures du rapport,
   vérifier versions 3.15.2/x64/GIL/OpenSSL/SQLite, absence de substitution et
   provenance accessible. Si binaire changé, provenance insuffisante pour le
   périmètre approuvé ou correctif nécessaire : arrêter, faire arbitrer ; aucun
   remplacement/download du runtime implicite. Ce runtime utilisateur reste
   une base de qualification, pas une installation approuvée du futur compte.
2. **Créer le conteneur autorisé.** Contrôles d'absence/parents ci-dessus,
   protection uniquement des nouveaux objets si autorisée, marqueur d'identité
   puis inventaire fermé. Exporter depuis Git les entrées du SHA : poster.py,
   cadence_simulator.py, newsbot, tools, tests, requirements.txt,
   requirements-dev.txt et ruff.toml. Ne copier ni site-packages, .env, état local,
   state.json ni journal confirmé 1947. Ne pas effectuer d'installation editable.
3. **Créer le venv par chemin absolu.** Avec le runtime épinglé, utiliser
   `-I -m venv --copies <Q>\venv`, sans --clear, --upgrade, --upgrade-deps ou
   --system-site-packages. Le bootstrap ensurepip est une installation dans ce
   venv neuf et doit figurer dans l'autorisation. Ne pas activer le venv ou
   modifier PATH/launcher/associations. Toutes les commandes utilisent ensuite
   `<Q>\venv\Scripts\python.exe`. Relever pyvenv.cfg, sys.executable,
   sys._base_executable et sys.path ; refuser site utilisateur, chemins de paquets
   3.12, .pth inattendu ou base différente. Un venv partage la stdlib de sa base :
   ce n'est pas une sandbox ou une nouvelle installation CPython indépendante.
4. **Figer les dépendances avant installation.** Conserver les cinq pins directs
   du dépôt. Résoudre pour CPython 3.15.2 Windows x64 avec GIL, sans extras inutiles,
   dans un périmètre d'acquisition explicitement approuvé. Produire un lock
   complet des transitives et outils de bootstrap/build nécessaires ; vérifier
   Requires-Python, tags de roues, absence de yank et avis de sécurité. Pas de
   réemploi d'un freeze 3.12 ni de sélection silencieuse de versions différentes.
   Soumettre ce lock, les URLs officielles/PyPI exactes, tailles et SHA-256 à revue
   avant installation. Une dépendance introuvable/incompatible arrête le plan.
5. **Traiter sgmllib3k explicitement.** La 1.0.0 n'a qu'une sdist annoncée, hash
   `7868fb1c8bfa764c1ac563d3cf369c381d1325d36124933a726f29fcdaa812e9`.
   Soit autoriser sa construction contrôlée à partir de cette source inspectée,
   outils de build eux-mêmes épinglés/hachés dans Q\build-venv, soit arrêter.
   Ne pas autoriser un build pip implicite avec téléchargement libre des outils.
   La roue obtenue doit être inspectée, son contenu/provenance et son hash
   consignés, puis approuvés dans le lock. Le hash PyPI de la source ne prouve
   pas celui de la roue locale ni la reproductibilité bit à bit de son build.
6. **Installer uniquement l'ensemble approuvé.** Acquisition des seuls artefacts
   du lock dans Q\wheelhouse, sans extra-index ou configuration pip héritée.
   Vérifier chaque hash avant usage, puis installation sans index, avec
   `--require-hashes --only-binary=:all: --find-links <Q>\wheelhouse -r <lock>`.
   Lancer le Python absolu du nouveau venv avec -I, pip avec
   --disable-pip-version-check et --no-cache-dir. Fournir au seul processus
   enfant un environnement construit explicitement : SystemRoot, TEMP/TMP vers
   Q\tmp et **PIP_CONFIG_FILE=NUL** (os.devnull Windows) ; aucune variable pip,
   proxy ou Python héritée. Ce réglage désactive les fichiers de configuration
   pip, sans les lire ou les modifier. Ne pas confondre le seul --isolated de
   pip avec la suppression de toute configuration globale.
   Vérifier que l'ensemble inclut toutes les transitives : aucun accès réseau
   de résolution pendant cette installation. Aucun upgrade du pip 3.12. Si le
   bootstrap du nouveau venv exige un pip différent, arrêter pour approbation
   de sa version et de sa roue ; jamais « latest » automatique.
7. **Vérifier l'environnement effectif.** pip check, inventaire importlib.metadata,
   comparaison exacte au lock et aux hashes des artefacts, chemins des modules
   importés. Les hashes de roues identifient les archives acquises ; vérifier
   aussi leurs RECORD/fichiers installés, pas seulement pip freeze. Conserver le
   rapport d'installation expurgé et distinguer déclarations d'éditeur, attestations
   éventuellement vérifiées et simples hashes reçus du même index.
8. **Exécuter les validations décrites ci-dessous.** Pas de Telegram ni de
   Credential Manager, aucune sonde de production. Toute exclusion est comptée
   et justifiée ; une condition critique non testée reste NO-GO.
9. **Clôturer désarmé.** Aucun worker vivant ni handle inutile, preuves d'arrêt
   et état de fixture conservés jusqu'à contrôle. Aucun passage au transport
   distant ou à une génération d'exploitation, même après réussite totale.

Ce protocole suit l'isolation par défaut de
[venv](https://docs.python.org/3.15/library/venv.html) et les exigences de pins,
hashes et roues de [pip](https://pip.pypa.io/en/stable/topics/secure-installs/),
avec sa [configuration neutralisée](https://pip.pypa.io/en/stable/topics/configuration/).
La résolution/build reste une opération d'acquisition à autoriser : les deux
requirements actuels seuls ne constituent pas un lock reproductible.

## Plan de tests et critères d'arrêt

Depuis Q\src, utiliser exclusivement le Python du nouveau venv et sa base
épinglée. Environnement des contrôleurs borné, pas d'héritage de credential ni
de configuration réseau ; workers gardent les contrats d'environnement existants.
Interdire les destinations autres que les ports 127.0.0.1 explicites des fixtures,
DNS externe, proxies, redirections et retries comme en F2. L'acquisition/audit
publics éventuels ont lieu dans une étape séparée du banc local.

| Vérification future | Critère exigé |
| --- | --- |
| Sondes F4-B1 et syntaxe | Trois sondes natives, compilation de tous les modules, mêmes ABI/PRAGMA |
| Suite complète | unittest discover -s tests -v sous 3.15, imports réels, aucun faux paquet ou test remplacé |
| Qualité et audit | Ruff épinglé, compileall mêmes cibles, pip check, audits du lock complet et inventaire ; aucun --fix |
| Création et confinement C1/C2 | JOB_LIST atomique, suspension, descendants, mort du parent, active=0 et racine signalée avant déverrouillage |
| Secret F2/C3 | DPAPI synthétique, DuplicateHandle ciblé non héritable, concurrence avec processus tiers, erreurs/interruption, fermetures et absence de fuite dans les logs |
| Arrêt F3 | Secours dans processus indépendant en lecture/écriture/TLS/suspension, collision et fausse heure de création refusées ; arrêt worker distinct de clôture durable du contrôleur |
| Persistance B1/B2/C0 | Reçus/curseur atomiques, crash avant/après commit, unicité, quotas, corruptions, backup cohérent et restauration désarmée |
| Intégration F3 | Préflights comptés, 300 s/20 lectures/6 tentatives, /help et /status exacts, authentification avant données, expiration, génération ancienne et réponse tardive |
| Incertitude | Acceptation locale puis perte de réponse : uncertain durable, aucune réémission après reprise/génération nouvelle |
| Encodage / base enfant | Chemins Unicode, CLI et création via venv puis base F2 ; preuve que tous les enfants utilisent le runtime retenu |

Les ACL natives ne sont exécutées que dans l'aire autorisée et avec des identités
approuvées. Reboot/rollback restent simulés sauf autorisation spécifique ; ne pas
revendiquer une coupure électrique ou une autorité anti-rollback indépendante.
Une CI Ubuntu/3.12 inchangée reste une non-régression distincte, pas un substitut
à ces résultats Windows/3.15.

Erreur critique, identité/hash divergents, corruption, quota, arrêt douteux ou
résultat ambigu : fermer l'admission, conserver uncertain/retired/recovery_required
et les barrières de restauration ; aucun reset, retry ou repli vers le bot de
publication. Si terminaison/récolte non prouvée, garder la quarantaine, ne pas
déclarer l'arrêt sûr et ne pas nettoyer une fixture encore potentiellement utilisée.

## Nettoyage et autorisations à demander dans l'ordre

Après arrêt et récolte prouvés, vérifier le chemin absolu sous Q, UUID, SID,
volume/file IDs, inventaire fermé, absence de liens et de fichier inconnu avant
toute suppression des seuls objets de test créés. Approuver la suppression du
venv/code/roues de qualification et des fixtures listées ; conserver les rapports
expurgés nécessaires à l'audit selon la décision opérateur. Doute/collision/
échec d'accès : arrêt sans suppression destructive et signalement. Aucun parent,
runtime de base, compte/profil ou objet préexistant supprimé. Pas de garantie
d'effacement physique de mémoire, pagefile, dump ou disque.

1. Autoriser une **nouvelle mission** et le SHA, compte actuel et runtime exacts
   pour qualification uniquement ; aucun GO réseau ou production implicite.
2. Approuver Q résolu absent, inventaire, budget disque, venv/bootstrap, source
   exportée et nettoyage. Les éventuelles ACL de ces objets neufs doivent être
   expressément incluses ; ne pas réutiliser une autorisation C0 antérieure.
3. Autoriser l'acquisition bornée des dépendances et outils ; approuver le lock
   complet, URLs/hashes et, si retenue, la construction sgmllib3k avant installation.
   Aucune modification des pins directs sans décision supplémentaire.
4. Autoriser les tests natifs synthétiques sur ces fixtures, et séparément les
   mutations ACL négatives/sondes tiers si nécessaires. Les tests ordinaires
   n'autorisent pas la création d'un compte ni un reboot Windows.
5. Après rapport, décider du runtime et d'une éventuelle qualification sous le
   futur compte/stockage F4-B1. Credential Manager, WAN et Telegram restent des
   étapes distinctes, sans permission déduite du venv ou de sa réussite.

**Aucune de ces opérations futures n'a été exécutée en F4-B2A.** Aucun lancement
de F4-B2B, installation, modification d'ACL, compte, Credential Manager, système,
publication, state.json, workflow, FIFO, flux, cadence ou journal confirmé 1947.
Une seule PR F4-B2A, sans fusion ni activation ; arrêt après livraison.
