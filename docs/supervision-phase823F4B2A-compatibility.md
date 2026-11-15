# F4-B2A — compatibilité du Python 3.15.2 existant

## Verdict et portée

**F4-B2A — PRÊTE POUR QUALIFICATION ISOLÉE AUTORISÉE.**
**Exploitation Telegram réelle : NO-GO. Suite complète Python 3.15 : non qualifiée.**

Le candidat reste CPython 3.15.2 Windows x64 avec GIL. Les sondes natives
limitées passent ; les paquets nécessaires à l'intégration sont absents.
Le [protocole d'environnement isolé](supervision-phase823F4B2A-isolation.md)
décrit la prochaine qualification, sans l'exécuter ni engager F4-B2B.
Le verdict porte sur la préparation de cette qualification, pas sur son résultat.

Prérequis vérifiés avant modification : #41 fusionnée au commit exact
`e507fa2ed2fc0b35cc471c2c86361965b80891db` ;
[CI post-fusion 43685665333](https://github.com/Pince2Homard/news-telegram/actions/runs/43685665333)
terminée avec succès sur ce commit. Fetch effectué ; origin/main à ce même SHA.
Arbre local propre, aucune mission F4-B2A existante ; branche
`codex/phase-f4b2a-python-compatibility` créée depuis origin/main.
Rapports F4-A, F4-B1 et garanties F1/F2/F3 relus. Aucun composant fonctionnel,
manifeste de dépendances ou workflow modifié ; deux documents ajoutés seulement.

## Méthode et runtime observé le 15 novembre 2026

Lecture ciblée du seul InstallPath HKCU PythonCore/3.15, puis utilisation du
chemin absolu de python.exe, sans sélectionner py/python via PATH. La lecture
du registre et les sondes natives nécessitent une sortie du sandbox de l'outil,
pas une modification des droits Windows. Aucun inventaire de profils personnels.
Le chemin personnel résolu reste local et n'est pas publié.

Exécution **-I -S -B**, environnement enfant limité à SystemRoot (et indicateurs
UTF-8/bytecode pour les trois sondes). -B est explicite : -I ignore les variables
PYTHON*. Aucun ajout de site-packages, PYTHONPATH, fichier .pth ou faux module.
Les métadonnées de son propre Lib/site-packages sont lues sans importer ses
paquets ; aucun environnement 3.15 supplémentaire n'est fourni ou identifié
dans ce périmètre ciblé. Aucune recherche exhaustive des disques n'est revendiquée.

| Élément réellement observé | Valeur |
| --- | --- |
| CPython | 3.15.2 final, tags/v3.15.2:5e58eaa, build du 25 octobre 2026, MSC v.1951 AMD64 |
| Architecture | Pointeur 64 bits ; exécution x64 |
| GIL | Py_GIL_DISABLED=0 ; sys._is_gil_enabled()=True |
| OpenSSL chargé | OpenSSL 3.5.9, 29 septembre 2026 |
| SQLite chargé | 3.53.4 |
| Environnement | Installation de base, sys.prefix == sys.base_prefix ; isolated=1, no_site=1 |
| Distributions propres disponibles | pip 26.2.2 uniquement |

Ces observations confirment F4-B1 pour cet exemplaire ; elles ne figent pas les
versions d'une installation future. La version publiée et son SBOM indiquent
CPython 3.15.2, OpenSSL 3.5.9 et SQLite 3.53.4.0, ce qui est cohérent avec la sonde.
[Publication officielle](https://www.python.org/downloads/release/python-3152/),
[SBOM Windows x64](https://www.python.org/ftp/python/3.15.2/python-3.15.2-amd64.exe.spdx.json).

## Signature, intégrité et provenance : preuves bornées

Empreintes SHA-256 calculées en lecture seule sur les huit fichiers ci-dessous.
WinVerifyTrust, action GENERIC_VERIFY_V2, FILE, UI_NONE, STATEACTION_VERIFY puis
CLOSE : résultat **0x00000000 pour chacun**, y compris fermeture de l'état.
WINTRUST_FILE_INFO=32 octets et WINTRUST_DATA=88 sur cet hôte x64.
Flags **0x1010 = CACHE_ONLY_URL_RETRIEVAL | REVOCATION_CHECK_NONE** ; aucun
Get-AuthenticodeSignature susceptible de récupérer une chaîne en ligne.
Microsoft documente le flag cache-only pour empêcher les récupérations réseau.
Il s'agit d'une propriété de l'API configurée, pas d'une capture de trafic.
[Contrat WinTrust](https://learn.microsoft.com/en-us/windows/win32/api/wintrust/ns-wintrust-wintrust_data).

Lecture séparée des certificats intégrés par X509Certificate.CreateFromSignedFile,
sans construction de chaîne réseau : sujet **Python Software Foundation** pour
les huit fichiers ; émetteurs Microsoft ID Verified CS EOC CA 04 ou AOC CA 04.
Cela dépasse la simple déclaration CompanyName du PE. La vérification s'appuie
toutefois sur la politique et les autorités déjà présentes sur ce Windows.
Elle ne prouve pas une révocation actuelle, la sécurité du poste, la provenance
historique de l'installateur, ni l'intégrité de tous les fichiers stdlib.

| Fichier relatif au runtime | SHA-256 observé |
| --- | --- |
| python.exe | d4e19e9a33268fb0ce575a24ab2d4b85972524f1933df008a6befc14dce19fbd |
| python315.dll | 9862572fd449f32a62c1eaec2c6750a9c9fa512f4c9c2ea0e66776cf9212b7c2 |
| python3.dll | e394c32bb9e2f0d813f4ed5b858ba717e54e3e6547cd36a8d8aaee66b9ca347f |
| DLLs/_ssl.pyd | a9c54ec4d115597880e0e44ff26b740328d0388a777dfb613d3aa2eb66b31e49 |
| DLLs/libssl-3.dll | 8957182a9c385bfd9f3f790921de2ddc614cf91f99edbed4e9e5446a46504e32 |
| DLLs/libcrypto-3.dll | 75495fe0e079ab00d49be740d3c710a0b94e3f74f9648cb7cc9f768fc7f52319 |
| DLLs/_sqlite3.pyd | 851e8bfa65328162ddad2dff9f7c213f8eceb2d90baf179aa472704ec3ca6295 |
| DLLs/sqlite3.dll | 73530e8f4555605d716352ee98fe0ce205a09e5eec57e7e6360d4c915efcacd8 |

Ces hashes sont un relevé, pas une liste officielle de fichiers installés.
Le SBOM consulté expose des composants mais aucune entrée files : il ne permet
pas cette comparaison fichier par fichier. Le hash de l'installateur publié
ne s'applique pas à python.exe extrait. Aucun installateur, paquet ou outil de
vérification n'a été téléchargé/installé ; seules des métadonnées publiques ont
été consultées. La chaîne de provenance complète reste un contrôle futur.

## Compatibilité documentaire des dépendances épinglées

Métadonnées PyPI des **versions exactes** consultées, sans téléchargement de
roues ni résolution/installation. Requires-Python admet 3.15 dans les cinq cas.
Un tag de roue ou un classifier ne prouve pas le fonctionnement du dépôt.

| Paquet demandé | Requires-Python / artefact annoncé pertinent | État sous 3.15 / limite |
| --- | --- | --- |
| feedparser 6.0.11 | >=3.6 ; py3-none-any | Absent ; pas de classifier 3.15, dépend de sgmllib3k ancien |
| requests 2.34.2 | >=3.10 ; py3-none-any ; classifier 3.15 | Absent ; ses dépendances restent à figer et tester |
| Ruff 0.14.0 | >=3.7 ; py3-none-win_amd64 | Absent ; binaire x64 sans ABI CPython spécifique, exécution à vérifier |
| pip-audit 2.10.1 | >=3.10 ; py3-none-any | Absent ; classifier limité à 3.14 dans ces métadonnées |
| coverage 7.17.2 | >=3.11 ; cp315-cp315-win_amd64 et roue Python pure | Absent ; classifier 3.15, extension native à tester avec GIL |

Sources : [feedparser](https://pypi.org/pypi/feedparser/6.0.11/json),
[requests](https://pypi.org/pypi/requests/2.34.2/json),
[Ruff](https://pypi.org/pypi/ruff/0.14.0/json),
[pip-audit](https://pypi.org/pypi/pip-audit/2.10.1/json),
[coverage](https://pypi.org/pypi/coverage/7.17.2/json).

Manquent également, dès le premier niveau obligatoire :

- feedparser : **sgmllib3k** ;
- requests : **charset_normalizer >=2,<4, idna >=2.5,<4, urllib3 >=1.26,<3,
  certifi >=2023.5.7** ; aucun extra SOCKS/chardet demandé ;
- pip-audit : **CacheControl[filecache] >=0.13, cyclonedx-python-lib >=5,<12,
  packaging >=23, pip-api >=0.0.28, pip-requirements-parser >=32,
  requests >=2.31, rich >=12.4, tomli >=2.2.1, tomli-w >=1.2,
  platformdirs >=4.2** ; sans ses extras dev/test/doc.

La fermeture transitive complète n'est pas résolue ici : ses versions exactes
ne sont pas épinglées par les deux manifestes du dépôt. Ne pas présenter cette
liste de premier niveau comme un lock complet. sgmllib3k 1.0.0 n'annonce qu'une
archive source ; une politique « roues uniquement » bloquera cette dépendance.
Une construction contrôlée et ses outils doivent être approuvés séparément,
sans retirer feedparser ou changer un pin pour faire passer le diagnostic.
[Métadonnées sgmllib3k](https://pypi.org/pypi/sgmllib3k/1.0.0/json).

**La partie suite complète 3.15 s'arrête à ce blocage.** Aucun test d'intégration
avec faux requests, emprunt de paquets 3.12, copie de site-packages ou installation
opportuniste. Les sondes stdlib ne nécessitent pas ces dépendances et restent
exécutables isolément.

## Essais réellement exécutés

| Niveau | Résultat et limite |
| --- | --- |
| Windows natif, 3.15.2 | Les 3 sondes F4-B1 passent en 0,066 s ; aucune dépendance tierce importée |
| Syntaxe 3.15.2 | 71 fichiers Python de poster, cadence_simulator, newsbot, tools et tests compilés en mémoire ; aucun import ni bytecode écrit |
| Suite Windows 3.12.10 existante | 444 tests, succès avec 4 exclusions ; code inchangé, sondes, anciens tests privés et publication inclus |
| Qualité 3.12 | Ruff 0.14.0, compileall sur ces mêmes cibles et pip check réussis |
| Suite complète 3.15 / paquets | Non exécutée, dépendances absentes ; **NO-GO qualification complète** |
| CI du SHA publié | Exécutions et liens consignés dans la PR ; workflow existant Ubuntu/Python 3.12, aucune preuve Windows/3.15 fournie par cette CI |

Commande des sondes : `<python315-absolu> -I -S -B
tests/test_private_runtime_qualification.py -v`, depuis le checkout approuvé.
Le fichier extrait par AST une liste fermée de définitions du dépôt : aucune
substitution de module pour imiter un import complet. Les preuves 3.15 sont :

- ctypes : Startup 104, StartupEx 112, ExtendedLimit 144, ProcessInfo 24 ;
  job **vide**, limites kill-on-close et active=8 relues ; DuplicateHandle
  non héritable **dans le même processus**, puis fermeture des handles ;
- DPAPI : fonctions/vault du dépôt, secret synthétique uniquement, aller-retour
  en mémoire, buffers effacés et refus après fermeture ; aucun Credential Manager ;
- SQLite : DELETE/EXTRA, foreign_keys ON, temp_store MEMORY, trusted_schema OFF,
  busy_timeout 100 effectifs ; BEGIN IMMEDIATE, rollback conjoint reçu/curseur,
  commit et réouverture/integrity_check. Modèle moteur, pas schéma B1 complet.

La suite 3.12 rejoue les créations atomiques C2, transferts ciblés F2,
interruptions de lecture/écriture/TLS et secours opérateur F3 sur boucle locale,
ainsi que les crashes SQLite. Elle est une baseline autorisée, **pas une solution
de repli prouvant 3.15**. Les modèles, ACL simulées et reboot simulé gardent leur
portée d'origine. Les trois tests C0 modifiant une DACL native restent exclus
faute d'autorisation nouvelle, et le cas POSIX est exclu sous Windows.
Aucun compte tiers, reboot physique, réseau distant ou credential réel testé.

### Audits sans installation

Auditeur local déjà disponible : **pip-audit 2.9.0**. Pour les manifestes :
`--disable-pip --no-deps -r requirements.txt`, puis mêmes flags avec les deux
manifestes ; aucune vulnérabilité connue signalée sur les **2 puis 5 pins directs**.
Le code de l'auditeur confirme que disable-pip évite son VirtualEnv de résolution.
Ces audits n'incluent pas une fermeture transitive résolue artificiellement.

Audit de l'inventaire installé 3.12 avec `--local`, sans correction : **échec,
6 avis sur pip 25.0.1** : PYSEC-2026-196, -1795, -1796, -2875, -2876 et -3721.
Leurs versions correctives annoncées vont de 25.3 à 26.2. Aucun upgrade lancé.
Ne pas qualifier cet environnement pour une acquisition de paquets sur ce seul
bilan. Son pip n'est ni copié ni utilisé comme installateur du futur venv 3.15.

Audit des seules métadonnées du site-packages 3.15 via `--path`, depuis l'auditeur
existant : **pip 26.2.2, aucune vulnérabilité connue signalée**. Il n'a pas fallu
installer l'auditeur dans 3.15 ni importer un paquet 3.12 dans 3.15.
Les requêtes d'audit/métadonnées publiques sont distinctes du transport privé,
resté entièrement local. Aucun audit ne démontre l'absence de vulnérabilité.

Le venv 3.12 contient pip-audit 2.9.0 et coverage 7.10.7, différents des pins
2.10.1/7.17.2 : écart conservé et déclaré, pas synchronisé. La CI existante
installe les pins sur un runner éphémère Ubuntu ; sa préparation habituelle
ne modifie aucune installation locale. Workflow inchangé.

## Risques, invariants et matrice GO / NO-GO

| Condition | Preuve / état | Conséquence |
| --- | --- | --- |
| Préparation de qualification isolée | GO documentaire ; protocole et blocages explicites | Revue humaine et autorisations suivantes ; aucun venv créé |
| Architecture x64/GIL, ABI de base, DPAPI, SQLite moteur | GO sondes natives 3.15 limitées | Ne valide pas worker, arrêt, dépendances ou transactions B1/B2 sous 3.15 |
| Intégrité des 8 PE signés | Vérification hors ligne réussie, hashes relevés | Provenance historique, révocation actuelle et installation complète non prouvées |
| Dépendances / suite complète 3.15 | NO-GO, paquets absents | Installation isolée expressément autorisée puis suite/native complète ; aucune déduction depuis 3 sondes |
| Acquisition reproductible | NO-GO tant que lock transitive, hashes et cas source sgmllib3k non approuvés | Aucun téléchargement/build/installation implicite |
| Publication existante | Tests 3.12 réussis, diff documentaire uniquement | Aucun PATH, launcher, paquet, état ou processus de publication changé |
| Maintenance de l'environnement 3.12 | Audit installé en échec sur pip ; écart de deux pins dev | Décision séparée de maintenance ; pas de réparation dans cette mission |
| Windows 10 / ESU / redémarrage | Conclusions F4-B1 conservées, pas de nouvelle sonde de licence ou mise à jour | Couverture applicable et opérations différées restent à clarifier par l'opérateur |
| Compte et stockage cible | Non créés/non qualifiés, NO-GO réel | Plan F4-B1 inchangé ; validation des parents/ACL/annexes sous autorisation distincte |
| Credentials, DNS/TLS/WAN, essai Telegram | Non qualifiés, NO-GO réel | Missions et autorisations distinctes ; 300 s / 20 lectures préflights compris / 6 tentatives, /help et /status seulement |

Sous le futur venv, vérifier **sys._base_executable** : F2 lance directement cette
base avec stdlib pour conserver le handle dupliqué non héritable ; d'autres
fixtures C1/C2 utilisent sys.executable. Un lancement venv qui réussit n'atteste
pas tous les exécutables enfants. Rejouer création atomique JOB_LIST, descendants,
concurrence, échecs de transfert et récolte avant libération des verrous.

Préserver B1 reçu/curseur atomiques, B2 intention avant tentative et résultat
durable, uncertain sans retry, barrières de restauration C0, fencing C2,
quotas/expiration C3 et secours indépendant F3. Aucun reset pour faire passer
un essai. Les limites F1 contre rollback coordonné/clonage/hôte hostile demeurent ;
aucune autorité expérimentale C5 n'est réintroduite.

Autorisations nécessaires : périmètre précis du nouvel environnement et des
artefacts acquis/construits, bootstrap/installation isolés, fixtures et nettoyage
contrôlé ; mutations ACL natives/compte tiers si retenus, puis décision de mission
suivante. Leur ordre et les chemins sont dans le protocole associé. Rien de cela
n'autorise Credential Manager, un secret réel, Telegram, une fusion ou F4-B2B.
