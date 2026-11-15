# F4-B1 — runtime Windows et décisions de maintenance

## Verdict et périmètre

**F4-B1 PRÊTE POUR DÉCISIONS OPÉRATEUR. Exploitation Telegram réelle : NO-GO.**

Retenir comme candidat **CPython 3.15.2 Windows x64 standard avec GIL**, dont un
exemplaire existe déjà sur le poste et a pu subir des sondes natives stdlib.
Il expose OpenSSL 3.5.9 et SQLite 3.53.4. Préparer un environnement de supervision
distinct sous un compte dédié ; ne pas modifier le Python de publication.
Ce choix n'est pas une déclaration de compatibilité complète F1/F2/F3 sous 3.15.

Le [plan compte/stockage et autorisations](supervision-phase823F4B1-storage.md)
décrit les opérations futures. Elles ne sont pas exécutées. Aucun runtime ou
installateur téléchargé, installé, remplacé ou compilé ; seuls documentation
et fichiers de métadonnées de build ont été consultés en ligne. Pas de Credential
Manager, réseau distant privé, Windows Update, activation ESU ou migration OS.
F4-B2 n'est pas démarré.

Prérequis vérifiés avant modification : #40 fusionnée au SHA
`9f24e5f3baf9f00df1f9195ae18c09f7f6d69c64` et
[CI post-fusion 43677210421](https://github.com/Pince2Homard/news-telegram/actions/runs/43677210421)
terminée avec succès sur ce SHA. Dernier origin/main récupéré à
`79251e52f0e5de78c8a7319b0bb01d827d440ec0`. Checkout propre et aucune branche
ou fichier F4-B1 antérieur ; branche `codex/phase-f4b1-runtime-storage`.
Rapports F1/F2/F3 et deux documents F4-A relus, sans les réécrire rétroactivement.

## Windows 10 : faits supplémentaires en lecture seule

Lecture ciblée des valeurs de version et de marqueurs du registre, puis des
seules métadonnées Name/LicenseStatus ESU via CIM. Aucun champ de clé de produit,
credential, identifiant de machine ou chemin personnel publié. Les identifiants
des trois années ESU interrogées sont les identifiants de produit publics
documentés par Microsoft, pas une clé d'activation locale.

| Contrôle au 15 novembre 2026 | Résultat | Interprétation |
| --- | --- | --- |
| Version | Windows 10 Pro 22H2, 19045.7846 | Build de registre observée ; pas une preuve de couverture ESU |
| CBS RebootPending | Clé absente | Aucun signal par ce marqueur uniquement |
| CBS RebootInProgress | Clé absente | Idem, aucune preuve universelle d'absence de redémarrage requis |
| WindowsUpdate Auto Update RebootRequired | Clé absente | Aucun signal par ce marqueur ; aucune recherche de mises à jour déclenchée |
| Session Manager PendingFileRenameOperations | Valeur présente, 23 paires estimées | Opérations différées à classer ; noms de fichiers non publiés, valeur non modifiée |
| ESU commerciaux années 1/2/3 | Chaque produit exact trouvé, LicenseStatus=0 | État « Unlicensed » pour ces produits au moment de la lecture |
| ESU grand public | Non déterminé par ces sondes | Ne pas conclure à son absence depuis le statut des produits commerciaux |

Microsoft documente les produits ESU commerciaux et leur contrôle de statut ;
la classe WMI définit 0 comme Unlicensed et 1 comme Licensed. Aucun appel à une
méthode d'activation, slmgr, écriture de licence ou outil d'enrôlement n'est fait.
[Produits ESU](https://learn.microsoft.com/en-us/windows/whats-new/enable-extended-security-updates),
[LicenseStatus](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/sppwmi/softwarelicensingproduct).

La build 19045.7846 correspond au relevé Microsoft du 10 novembre ; sa table
affiche aussi 19045.7850 du 13 novembre. Applicabilité, installation effective
de correctifs supplémentaires et état global de redémarrage ne sont pas prouvés
par nos sondes. Aucun scan Windows Update, modification de service ou effacement
des indicateurs n'est utilisé pour obtenir un résultat favorable.
[Historique officiel](https://learn.microsoft.com/en-us/windows/release-health/release-information).

### Peut-on conserver Windows 10 ?

Oui comme **option conditionnelle**, avec couverture ESU réellement applicable
à ce poste et à son usage, correctifs requis vérifiés, incidents de redémarrage
résolus par l'opérateur, runtime maintenu et protections locales qualifiées.
Le support ordinaire est terminé ; la seule présence d'une mise à jour récente
ne suffit pas. Le programme grand public décrit actuellement une couverture
jusqu'au 12 octobre 2027 et couvre les utilisateurs d'un poste enrôlé, mais
ses critères d'éligibilité et l'enrôlement de cette machine restent à attester.
Le compte de supervision n'a pas à devenir administrateur pour exploiter une
couverture valide du poste. Ne pas modifier licences ou compte Microsoft dans
cette mission.
[Programme Microsoft grand public](https://www.microsoft.com/en-us/windows/extended-security-updates).

Le marqueur de renommages différés n'est pas une preuve qu'une mise à jour
Windows exige un reboot ; il peut concerner d'autres logiciels. Un opérateur
doit en classer la cause dans un canal local approprié, sans publier les chemins.
Ne pas effacer la valeur, redémarrer automatiquement ou présenter les trois
clés absentes comme un GO. Maintenance OS et redémarrage restent NO-GO réel.

### Windows 11 : option à étudier séparément

Une migration n'est pas un correctif automatique à appliquer à ce dépôt.
L'éligibilité matérielle n'a pas été établie : CPU supporté, TPM 2.0, UEFI/Secure
Boot, RAM/disque, pilotes et périphériques doivent être étudiés, ainsi que les
processus de publication, leurs chemins, tâches existantes éventuelles et le
plan de retour arrière. Aucun PC Health Check installé, matériel reconfiguré,
partition modifiée ou upgrade déclenché.
[Prérequis Microsoft](https://www.microsoft.com/en-us/windows/windows-11-specifications).
La même suite native et une qualification du stockage/compte seraient nécessaires
après migration ; Windows 11 ne prouve pas à lui seul la sûreté de l'application.

## Inventaire des runtimes : ne pas confondre observation et installation

Le contexte registre du sandbox ne montrait initialement aucune version ; une
lecture ciblée hors sandbox a trouvé les tags utilisateur 3.12 et 3.15. Un tag
présent dans les vues 32/64 ne signifie pas deux installations distinctes.
Aucune recherche dans des répertoires personnels arbitraires, ni invocation de
py/python à sélection automatique : certains gestionnaires peuvent installer
un runtime manquant. Aucun exécutable découvert n'est utilisé pour lancer le bot.

Le binaire enregistré 3.15 présente une version PE 3.15.2 et un éditeur déclaré
Python Software Foundation. Une sonde absolue **-I -S**, avec environnement limité
à SystemRoot/PYTHONDONTWRITEBYTECODE/PYTHONUTF8, a confirmé :

| Élément | Environnement de validation existant | Autre runtime déjà présent |
| --- | --- | --- |
| CPython | 3.12.10, x64 | 3.15.2, x64 |
| TLS effectivement chargé | OpenSSL 3.0.16 | OpenSSL 3.5.9 du 29 septembre 2026 |
| SQLite effectivement chargé | 3.49.1 | 3.53.4 |
| GIL | Chemin CPython standard utilisé | Actif ; Py_GIL_DISABLED=0 |
| Dépendances du dépôt | Déjà disponibles pour les validations 3.12 | Aucune des cinq dépendances directes de développement/production détectée dans son site-packages |

La lecture PE ne vérifie pas la signature ou la provenance de l'installateur.
Le runtime existant permet un diagnostic sans installation, mais son emplacement
et ses ACL ne deviennent pas un emplacement partagé approuvé pour le futur compte.
Un nouveau venv doit posséder ses propres dépendances ; aucun site-packages de
publication n'est ajouté à sys.path pour faire artificiellement passer la suite.

## Comparaison des options maintenues

Versions au jour de l'audit, à réévaluer avant une autorisation d'acquisition.
Les versions « build déclaré » viennent de PCbuild/python.props du tag officiel,
pas d'un binaire téléchargé/exécuté. Pour 3.15.2 seulement, la sonde ci-dessus
confirme en plus TLS/SQLite réellement chargés dans l'exemplaire local.

| Option | Maintenance / distribution Windows | TLS et SQLite | Compatibilité et exploitation | Choix |
| --- | --- | --- | --- | --- |
| Garder 3.12.10 observé | Ancien patch ; aucune preuve de correctifs fournisseur | 3.0.16 / 3.49.1 observés ; OpenSSL 3.0 hors support public | Baseline historique testée, ne résout pas la maintenance TLS | Non retenu pour le privé réel ; publication inchangée |
| 3.12.16 corrigé | Sécurité seule, source uniquement ; dernier installateur classique 3.12.10 | Build déclaré 3.0.16 avec suffixe de paquet .2 / 3.49.1 ; maintenance TLS ne se déduit pas du suffixe | Petit écart Python mais build Windows/TLS maintenu à produire ou faire attester ; charge disproportionnée ici | Pas de compilation ou DLL remplacée à la main |
| 3.13.18 | Sécurité seule, source uniquement à cette version | Build déclaré OpenSSL 3.5.9 / SQLite 3.50.4 | Écart Python réduit, mais chaîne de build/distribution supplémentaire ; 3.13.17 binaire ne remplace pas le correctif 3.13.18 | Alternative si fournisseur maintenu spécifiquement qualifié |
| 3.14.10 x64 avec GIL | Maintenance active, installateur Windows publié | Build déclaré 3.5.9 / 3.50.4 | Candidat raisonnable ; aucun exemplaire exécuté ici, changement mineur à qualifier | Alternative documentée, pas installée |
| **3.15.2 x64 avec GIL** | Maintenance active, installateur publié et exemplaire local observé | **3.5.9 / 3.53.4 observés** | Sondes stdlib natives réussies ; suite complète et dépendances manquent sous 3.15 | **Candidat retenu pour décision opérateur** |

Sources primaires de distribution : [3.12.16](https://www.python.org/downloads/release/python-31216/),
[3.13.18](https://www.python.org/downloads/release/python-31318/),
[3.14.10](https://www.python.org/downloads/release/python-31410/),
[3.15.2](https://www.python.org/downloads/release/python-3152/),
[cycles CPython](https://devguide.python.org/versions/).
Métadonnées Windows :
[3.12.16](https://raw.githubusercontent.com/python/cpython/v3.12.16/PCbuild/python.props),
[3.13.18](https://raw.githubusercontent.com/python/cpython/v3.13.18/PCbuild/python.props),
[3.14.10](https://raw.githubusercontent.com/python/cpython/v3.14.10/PCbuild/python.props),
[3.15.2](https://raw.githubusercontent.com/python/cpython/v3.15.2/PCbuild/python.props).

OpenSSL 3.5 est LTS avec support annoncé jusqu'au 8 avril 2030 ; 3.0 ne reçoit
plus de correctifs publics. Cela qualifie le choix de branche TLS, pas l'absence
de vulnérabilité d'un binaire ni sa validation de certificats/réseau.
[Politique OpenSSL](https://openssl-library.org/policies/releasestrat/index.html).
Notre préférence pour 3.15.2 est un arbitrage opérationnel : exemplaire existant
sondable sans installation et pile TLS maintenue observée. La nouveauté de la
branche et le changement SQLite rendent obligatoire la suite complète future.
Si elle échoue, rester désarmé et examiner 3.14.10 ; pas de repli automatique.

La documentation CPython annonce Windows 10 et suivants pour 3.15 ; ce support
de plateforme n'annule pas les conditions ESU de Microsoft.
[Windows et environnements isolés](https://docs.python.org/3.15/using/windows.html).
L'installateur standard est préféré au paquet embeddable ou à une nouvelle
distribution/outillage de gestion à maintenir. GIL conservé pour limiter le
périmètre de qualification, jamais pour remplacer les verrous applicatifs.

## Compatibilité avec les composants existants

README et CI déclarent actuellement Python 3.12, sans contrat de support testé
sur 3.15. Aucun workflow, dépendance ou composant fonctionnel n'est modifié.
Les seules lignes de code nouvelles sont trois tests stdlib justifiés par
l'absence de dépendances dans le runtime candidat.

| Garantie | Revue et preuve disponible | Reste à tester sous le runtime/compte cible |
| --- | --- | --- |
| ABI ctypes | Sous 3.12 et 3.15 : pointeurs 8 octets, Startup 104, StartupEx 112, ExtendedLimit 144, ProcessInfo 24 | Imports complets, CreateProcess étendu et attribut JOB_LIST dans la chaîne C2 réelle |
| Job / handles | Sous 3.15 : job vide, limites kill-on-close/8 processus relues et duplication locale non héritable | Worker suspendu, descendants, collisions, transfert exclusif F2 et erreurs de création ; le handle dupliqué de la sonde reste dans le même processus |
| DPAPI | Fonctions et vault synthétiques du dépôt exécutés nativement sous 3.15, effacement/revocation vérifiés | Chemin complet pipe/worker et changement d'identité Windows ; aucun Credential Manager testé |
| SQLite | Sous 3.15 : DELETE/EXTRA, FK, MEMORY, trusted_schema OFF, busy_timeout 100 ; BEGIN IMMEDIATE, rollback et réouverture cohérente | Schémas B1/B2/C0, unicités, quotas, sauvegarde/restauration, corruptions et tous les crashes sous 3.15 |
| Fencing et contrôleur unique | Code C2/F3 inchangé, preuves natives rejouées sous 3.12 | Même récolte avant déverrouillage sous 3.15 ; pas de fallback vers la création C1 historique |
| Quotas / uncertain / arrêt F3 | Transactions, limites 300/20/6, absence de retry et secours conservés | CLI sous le compte cible, interruptions TLS/écriture/lecture/suspension et mort contrôleur sous 3.15 |
| Publication séparée | Aucun fichier de publication modifié ; aucun environnement ou paquet partagé pour la sonde | Venv privé neuf, lancement par chemin absolu, base du worker réellement épinglée |
| Encodage | Les formats privés utilisent des lectures/écritures UTF-8 explicites ; 3.15 active UTF-8 par défaut | Chemins Unicode, CLI et sous-processus à rejouer avec les dépendances cibles |

Les trois tests de tests/test_private_runtime_qualification.py extraient une
liste AST fermée de définitions locales : structures/_api de private_job,
StartupEx et fonctions/vault DPAPI de private_credential. Ils évitent de remplacer
requests par un faux module pour simuler un import complet. Aucun worker n'est
lancé par ces sondes. Le test SQLite est un modèle de transaction du moteur,
pas le magasin B1 complet. Cette portée limitée est volontaire et reproductible
en lançant ce seul fichier avec le Python cible -I -S -B, sans cache bytecode.

Le layout ctypes MSVC par défaut sous Windows est documenté ; les variations
récentes de ctypes justifient ces mesures plutôt qu'une présomption d'ABI.
[Documentation ctypes](https://docs.python.org/3.15/library/ctypes.html).

### Dépendances et qualification à compléter

Métadonnées installées du venv 3.12 : requests 2.34.2 (Requires-Python >=3.10),
feedparser 6.0.11 (>=3.6), Ruff 0.14.0 (>=3.7), pip-audit 2.9.0 (>=3.9),
coverage 7.10.7 (>=3.9). Les deux premières correspondent à requirements.txt.
Le manifeste de développement demande **pip-audit 2.10.1 et coverage 7.17.2** :
le venv local n'est pas synchronisé exactement sur ces deux versions.
pip check ne relève pas de dépendance cassée, sans prouver cette synchronisation.

Les audits locaux, exécutés par pip-audit 2.9.0 déjà présent, analysent les deux
manifestes déclarés ; la CI installe leurs versions épinglées. Coverage n'est pas
utilisé par la commande unittest/quality de cette livraison. Aucun paquet n'a
été mis à jour dans le venv existant. Requires-Python et classifiers ne prouvent
ni une roue compatible 3.15 ni un comportement correct : résolution/installation
dans le nouveau venv et suite complète devront être explicitement autorisées.

## Validation et matrice actualisée

Trois sondes Windows natives sous le **3.15.2 existant : succès**,
avec -I -S -B et environnement fermé. Aucun site-packages du 3.12 emprunté.
Sous 3.12, la suite complète finale inclut ces trois sondes et tous les anciens
tests : **444 tests en 151,528 s, succès, 4 exclusions**. Les CI du SHA livré
sont consignées dans la PR. Ruff,
compileall et pip check réussissent ; audits production puis production et
développement : aucune vulnérabilité connue dans les manifestes analysés.

Les trois tests C0 exigeant une nouvelle DACL native restent exclus : aucune
autorisation historique réutilisée. Le cas POSIX reste exclu sous Windows.
Les sondes 3.15 n'incluent ni réseau même local, ni arrêt d'un worker, ni autre
compte. La suite 3.12 conserve les qualifications natives F2/F3 et les modèles
clairement identifiés, sans prouver une compatibilité 3.15 par substitution.

| Décision / condition | État F4-B1 | Suite nécessaire |
| --- | --- | --- |
| Diagnostic et options opérateur | GO documentaire : verdict prêt pour décisions | Audit humain de cette PR |
| ESU commercial | Statuts observés 0, aucune activation prouvée | Déterminer couverture grand public ou autre couverture applicable, sans déduire une absence globale d'ESU |
| Windows Update / reboot | NO-GO réel ; indicateur de renommages à clarifier, état WU non exhaustif | Vérification/remédiation opérateur séparément autorisée, jamais suppression de marqueur |
| Branche Python/TLS candidate | GO choix : 3.15.2 / OpenSSL 3.5.9 observés | Provenance/signature/SBOM, version de patch et environnement cible à figer |
| Compatibilité complète du candidat | NO-GO intégration réelle ; trois prérequis natifs passent seulement | Dépendances isolées et suite complète/native sous 3.15, compte et base du worker retenus |
| Compte et stockage cible | NO-GO réel ; plan précis disponible | Autorisations du document stockage, puis preuves ACL natives et accès tiers |
| Budget d'espace global | Proposition, non garantie par les seuls quotas par répertoire | Vérifier/enforcer l'agrégation cible sans élargir les limites B1 |
| Credential Manager / WAN | Non commencés, NO-GO | Missions et autorisations spécifiques ultérieures |
| Activation Telegram et F4-B2 | Interdites | Aucune autorisation déduite de cette PR ou de son verdict |

Les incertitudes restantes ne sont pas contournées. Le verdict signifie que
les choix et autorisations sont assez précis pour être arbitrés par l'opérateur,
pas que le poste ou un essai Telegram sont qualifiés. Aucune fusion automatique.
