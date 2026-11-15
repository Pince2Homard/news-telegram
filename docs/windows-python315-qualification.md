# Qualification intégrée Windows / Python 3.15 — issue 43

## Décision

**Compatibilité fonctionnelle privée démontrée sur les fixtures Windows 3.15.2 :
446 tests, succès, 2 exclusions. Qualification opérationnelle du poste : NO-GO.**
La confidentialité durable du conteneur Q n'est pas établie : une ACE de lecture
pour une identité d'outil réapparaît après les contrôles. Deux caches Python
créés hors Q pendant le premier essai restent conservés pour décision opérateur.
Ces conditions ne sont pas transformées en avertissements permettant l'activation.
**Telegram réel reste NO-GO.**

Mission unique de l'[issue 43](https://github.com/Pince2Homard/news-telegram/issues/43),
opérations bornées par l'[autorisation 6890985168](https://github.com/Pince2Homard/news-telegram/issues/43#issuecomment-6890985168)
renvoyant à l'inventaire du commentaire 6890874092. Aucune nouvelle sous-phase,
autorité externe, installation système ou PR documentaire préparatoire.

Prérequis : #42 fusionnée au commit
`b2a93ec547b62753300cdd996c6ef2cb22e76836` ;
[CI post-fusion 43687632275](https://github.com/Pince2Homard/news-telegram/actions/runs/43687632275)
réussie avant mutation. Base récupérée :
`07064d86f6b669091b59620bbf755b9ad6e25107` ; branche
`codex/issue-43-windows-python315-qualification`.
Le seul écart entre le checkout précédent et ce main concernait state.json :
la branche et son index ont été déplacés sans réécrire les octets de ce fichier.
Sa divergence locale est conservée, exclue des commits et du diff livré.

## Environnement réellement utilisé

Un seul conteneur neuf Q sous le profil courant, au nom UUID précisément
autorisé, sans compte créé. Les chemins absolus et SID restent locaux.
Deux venv **3.15 uniquement**, tests et build, sans system-site-packages,
changement de PATH/launcher ou paquet emprunté à 3.12. Aucun venv de publication
utilisé pour cette mission.

| Élément | Observation |
| --- | --- |
| Runtime déjà présent | CPython 3.15.2 x64, GIL actif |
| Bibliothèques chargées | OpenSSL 3.5.9 ; SQLite 3.53.4 |
| Bootstrap | Roue ensurepip pip 26.2.2 existante, empreinte vérifiée, sans écriture bytecode dans la base |
| Acquisition | Exactement les 34 artefacts approuvés, 20 548 593 octets ; tailles, SHA-256, tags, METADATA racine et RECORD vérifiés |
| Build sgmllib3k | Source 1.0.0 approuvée, inspectée ; pip 26.2.2, setuptools 84.2.1, wheel 0.48.0, packaging 26.3 |
| Venv de tests | Exactement 32 distributions/version conformes au lock ; pip check réussi |
| Réseau de tests | Fixtures 127.0.0.1 uniquement ; contrats d'isolation des workers conservés |

Les [entrées et empreintes](qualification-python315/inputs.json),
[lock de tests](qualification-python315/test.lock),
[lock de build](qualification-python315/build.lock) et
[résultats structurés](qualification-python315/results.json) sont des preuves
de qualification, pas de nouveaux manifestes de production ou un installateur.
Aucune découverte automatique de ces fichiers par le watcher.

La roue locale sgmllib3k a pour SHA-256
`0b413654749cde901915a1cb18d1b6fe2a385723b830c14e4ca60ce16d978d8b`.
Son unique module correspond octet pour octet à la source approuvée ; les autres
entrées sont ses métadonnées dist-info. Build Python pur, réseau/sous-processus
refusés par un audit hook et sondes de refus explicites, aucun compilateur ni
backend téléchargé. SOURCE_DATE_EPOCH=1800000000, sans changement de l'horloge OS.
Une seule construction : aucune reproductibilité bit à bit indépendante affirmée.
Les hashes reçus du même index ne sont pas une preuve indépendante de provenance.

## Changements limités et justifiés par les essais

1. **Workers privés et descendants : -I -B explicites.** Le premier essai a montré
   que PYTHONDONTWRITEBYTECODE transmis dans l'environnement ne suffit pas avec
   -I. Les lancements C1/C2, F2 et du descendant fixe ajoutent -B ; les deux
   lancements directs de tests suivent ce contrat. Aucune modification du Job
   Object, des handles, de l'environnement secret, du fencing ou des délais.
2. **Banc C0 sous le profil approuvé.** Les trois tests natifs utilisent
   QualificationArea(parent=Q/fixtures), avec racine et référence d'autorisation
   explicitement fournies. Marqueur UUID, identité NTFS, propriétaire et chaîne
   ACL sont vérifiés avant création. Absence d'accord ou parent étranger : refus
   avant inspection/mutation ACL. Aucun usage du créateur historique sous C:.
   Les deux mutations négatives restent limitées aux noms autorisés ; leurs
   DACL sont sauvegardées puis restaurées exactement, avec contrôle d'identité
   avant restauration et nettoyage. Ce marqueur de test n'est pas une autorité
   indépendante contre un administrateur, un clone ou un rollback coordonné.
3. **Contrat du watcher historique préservé.** manage.diagnose exige toujours
   Python 3.12. Le test de préparation reste exécuté sous 3.12 ; sous 3.15 un
   autre test exige le refus et l'absence de fichier de tâche préparée. Aucun
   faux sys.version_info et aucune ouverture de ce garde pour faire passer la suite.

Seuls trois modules privés et quatre fichiers de tests sont modifiés, en plus
des preuves de cette qualification. Aucun code de publication, watcher.py,
manage.py, workflow, flux, FIFO, cadence ou journal confirmé 1947 modifié.

## Tests exécutés et portée des preuves

Exécution complète depuis l'export Q/src, avec le Python de Q/venv et un
environnement construit explicitement, sans credential ambiant. Les sept fichiers
modifiés testés ont été comparés octet pour octet à ceux du diff livré.

**Résultat final : 446 tests en 157,594 s, succès, 2 exclusions.**
Le processus complet a été récolté ; contrôle ciblé final : zéro processus
Python dont la ligne de commande référence Q. Ce contrôle complète les preuves
par handles/job des tests ; il ne les remplace pas par un simple PID.

| Classe native Windows réellement exécutée | Cas réussis | Preuve bornée |
| --- | ---: | --- |
| NativeRuntimePrerequisites | 3 | ABI ctypes, job vide, DPAPI synthétique et PRAGMA effectifs |
| WindowsJobTests C1 | 11 | Budget monotone, blocages TLS/lecture/écriture, descendants, mort du parent, arrêt/récolte et quarantaine |
| NativeRecoveryTests C2 | 8 | JOB_LIST atomique, générations, crashes du contrôleur, preuves OS et refus de reprise douteuse |
| NativeTrialTests C3 | 10 | Autorisation synthétique, compteurs, DPAPI/pipe, interruption et désarmement |
| WindowsTransferTests F2 | 4 | Duplication ciblée non héritable, concurrence, erreurs et transport local |
| WindowsIntegration F3 | 6 | Chaîne B1/B2 locale, intention avant tentative, uncertain, secours indépendant et mort du contrôleur |
| NativeWindowsQualificationTests C0 | 3 | Propriétaire/DACL/parents, journal SQLite, backup/restauration désarmée et deux refus ACL avec restauration |

Soit **45 cas dans les classes natives**. Certaines injections de panne de ces
classes restent des simulations explicites ; le classement ne transforme pas
une panne injectée en coupure électrique ou reboot physique.
Les autres tests couvrent publication et modèles B1–C5, dont les limites de
l'autorité expérimentale C5 ; celle-ci n'est pas réintroduite dans l'architecture.

Les crashes SQLite par os._exit exercent les commits/rollbacks réels. Anciennes
générations, commandes expirées, 300 s/20 lectures préflights compris/6 tentatives,
authentification avant données et absence de retry uncertain sont vérifiés avec
identités/horloges synthétiques et scénarios locaux. Pas de session WAN de 300 s,
réseau Telegram, credential réel, autre compte ou coupure d'alimentation testé.

Les deux exclusions sont le cas de modes POSIX et la préparation historique du
watcher réservée à Python 3.12. Le refus réel de cette préparation sous 3.15 est
testé, sans promouvoir le watcher/publication à cette nouvelle version.

Le garde réseau du contrôleur a compté 74 binds de boucle locale, zéro connexion
directe et une opération refusée. Les connexions des workers ont leur propre
garde destination/port et tests négatifs. Ce n'est pas une capture de paquets
globale ou une qualification DNS/TLS/WAN.

Ruff 0.14.0, compileall sur poster/cadence_simulator/newsbot/tools/tests et pip
check réussissent sous 3.15. pip-audit 2.10.1 : **32 paquets installés audités,
aucune vulnérabilité connue signalée**. Audit public PyPI séparé des tests,
sans netrc/proxy/credentials ambiants, redirection ou cache hors Q ; aucune
installation/résolution/correction par l'audit.

Windows 3.12 : baseline historique de **444 tests, 4 exclusions**, SHA
`6ec7b44ca61c318f16290dfbfd1e7ed03a276113`. **Pas de nouvelle suite Windows 3.12
sur ce diff**, l'accord porte sur deux venv 3.15 et interdit le recours au venv
de publication. Les CI Ubuntu/3.12 du SHA publié sont consignées dans la PR ;
elles ne remplacent pas cette preuve native manquante.

## Incidents conservés dans le bilan

Le premier essai a parcouru 442 tests : un échec sur le garde 3.12 du watcher,
une erreur de classe C0 refusant les ACL et une exclusion POSIX. Les problèmes
du banc/résumé ont été corrigés ; aucun résultat de ce premier passage n'est
présenté comme une réussite complète.

### Confidentialité de Q non stable dans le contexte d'outillage

Q a été créé et validé avec la DACL privée approuvée. Une ACE explicite héritée
vers ses enfants, droits 1179817 (lecture/exécution), pour une identité dont le
nom correspond à Codex/sandbox est ensuite apparue. L'origine exacte n'est pas
établie ; elle n'est pas attribuée aux tests sans preuve.

Un seul rétablissement effectif de la DACL du Q appartenant à cette mission,
sans changement de parent, a permis les trois tests C0 natifs : contrôle positif,
deux mutations négatives exactement bornées, refus et restauration. **Le contrôle
après la suite et les outils qualité constate de nouveau un accès tiers et refuse
Q.** Aucune boucle de réécriture privilégiée ni exception au validateur pour
tolérer cette identité. Les succès ponctuels ne prouvent pas une confidentialité
continue. Q n'est pas approuvé comme stockage privé opérationnel.

### Deux fichiers de cache créés hors du périmètre Q

Les workers -I du premier essai ont produit les caches d'encodage nécessaires
au TLS local. La protection par variable d'environnement était insuffisante ;
-B a été ajouté avant le second passage. Relevé de l'installation de base :
**2471 fichiers préexistants inchangés, aucun supprimé ; deux seuls ajouts**.
Le second passage n'a ajouté/modifié aucun autre fichier de cette installation.

| Fichier relatif au runtime 3.15 enregistré | Octets | SHA-256 constaté |
| --- | ---: | --- |
| Lib/__pycache__/stringprep.cpython-315.pyc | 54420 | 5a9574f98f91dafca0dae11b4f9f64480e97106db4a855d6914acc4bdc9facb0 |
| Lib/encodings/__pycache__/idna.cpython-315.pyc | 15119 | f74a24345cfe40039c6128c0d0c3144afff047f261ac1e288235f94c89de9053 |

**Ils n'ont pas été effacés : l'accord de nettoyage ne vise que Q.**
La conformité à une installation de base intégralement inchangée n'est donc pas
revendiquée. Supprimer uniquement ces deux ajouts, puis prouver leur absence de
réapparition sur un test TLS -I -B nécessite une décision opérateur ciblée.
Aucun .py, exécutable ou DLL préexistant ne sera supprimé/modifié à cette occasion.

## Matrice de décision et clôture

| Condition | État et conséquence |
| --- | --- |
| Composants privés sous Windows 3.15.2 | GO fonctionnel borné aux fixtures ; suite complète et 45 cas de classes natives réussis |
| ACL natives C0 | Trois scénarios réussis lorsque Q était protégé ; pas de preuve de stabilité ensuite |
| Stockage Q/confidentialité continue | **NO-GO** : accès tiers réapparu, aucun assouplissement du garde |
| Base Python strictement inchangée | **Non satisfaite** tant que les deux caches restent présents ; nettoyage hors Q non autorisé |
| Non-régression Windows 3.12 actuelle | Non exécutée ; baseline historique et CI portable seulement |
| Compte dédié / ESU / redémarrage | Conclusions F4-B1 conservées, aucune opération ou preuve supplémentaire ici |
| Credential Manager / DNS-TLS-WAN / exclusivité distante | Non qualifiés, **NO-GO** ; aucun déploiement ou essai réel déduit de ces tests |
| Rollback coordonné / hôte hostile / handles perdus au reboot | Limites F1 conservées ; arrêt fermé, aucune autorité technique supplémentaire prétendue |
| Exploitation Telegram | **NO-GO absolu**, aucune activation ni fusion automatique |

Q et les deux caches sont conservés pour l'arbitrage décrit dans l'issue. Aucun
worker lié à Q n'est encore observé ; les aires C0 ont effectué leur propre
nettoyage vérifié. Le conteneur global ne sera supprimé que si identité, inventaire,
liens et conditions d'arrêt sont suffisamment prouvés. Doute = conservation et
signalement, jamais suppression d'une ressource préexistante.

Recommandation : retenir les résultats fonctionnels 3.15 et le correctif -B,
maintenir le NO-GO de stockage/exploitation et faire arbitrer les deux caches
hors Q. La stabilité des DACL doit être qualifiée dans un contexte opérateur
qui ne réintroduit pas cet accès tiers, sans créer automatiquement un compte,
modifier les ACL des parents ou entrer en compétition avec l'outillage.
Une seule PR de résultats/correctifs ; aucune nouvelle sous-phase.
