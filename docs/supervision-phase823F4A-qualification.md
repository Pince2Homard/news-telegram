# F4-A — qualification préalable Windows, audit hors ligne

## Décision au 15 novembre 2026

**Audit F4-A livré ; mise en service réelle NO-GO.** Les tests locaux passent,
mais ne qualifient ni la maintenance du runtime observé, ni un compte/stockage
cible, ni Credential Manager, ni le réseau distant. Les points manquants de la
matrice ci-dessous sont des conditions bloquantes, pas des avertissements.

Une seule PR documentaire, sans modification des composants fonctionnels ni
nouvelle architecture. Le [runbook proposé](supervision-phase823F4A-runbook.md)
reste désactivé et soumis à des autorisations ultérieures. F4-B n'est pas engagé.

Prérequis vérifiés : #39 fusionnée au commit
`452e1ba8bcf757193488e39947a20d14fba3938c` et
[CI post-fusion 43661719885](https://github.com/Pince2Homard/news-telegram/actions/runs/43661719885)
terminée avec succès sur ce SHA. Dernier origin/main récupéré au commit
`2f86898aca9d5e18209bbf68fe1b2419b8d11485`. Checkout initial propre, aucune
branche ou fichier F4-A local à reprendre ; branche dédiée
`codex/phase-f4a-windows-qualification`. Rapports F1/F2/F3 relus.

## Méthode et limites de l'inspection

Lecture seule du registre de version Windows, identité effective courante,
trois services nommés, volume système et horloge via Win32/Python. Inspection
Get-Item/Get-Acl des seuls répertoires connus workspace, Temp, profil courant,
LocalAppData, ProgramData et de leurs parents ; aucun parcours de leur contenu.
Les snapshots ACL restent en mémoire et ne sont publiés qu'en résultats agrégés,
sans nom d'utilisateur, SID personnel, nom de machine ou chemin de profil.

La requête CIM de version OS était refusée dans le contexte d'inspection ;
registre et API Win32 ont fourni les constats ci-dessous. L'inspection ACL a
nécessité l'exécution en lecture seule hors des restrictions du sandbox outil.
Aucune élévation UAC, modification de droit, création de compte ou de dossier
système, consultation d'entrée Credential Manager ou installation n'a été faite.
La lecture des ACL sous le contexte d'audit ne prouve pas les droits d'un futur
compte distinct. La santé complète de Windows et le statut ESU ne sont pas déduits
du succès de quelques appels API.

## Constats natifs du poste

| Contrôle | Résultat effectivement observé | Portée |
| --- | --- | --- |
| Windows | Windows 10 Pro, 22H2, build de registre 19045.7846 | Version observée, pas une attestation de maintenance ou d'absence de reboot en attente |
| Python | CPython 3.12.10, processus 64 bits | Environnement de validation ; installation cible maintenue à qualifier |
| TLS | OpenSSL 3.0.16 du 11 février 2025, TLS 1.2 et 1.3 disponibles | Disponibilité technique, pas qualification TLS/WAN |
| SQLite | 3.49.1 ; DELETE/EXTRA et autres PRAGMA contrôlés par la suite B1–C3 | Transactions réelles sur fixtures ; aucune preuve de coupure électrique |
| Volume système | NTFS, support des ACL persistantes, environ 11 Gio disponibles | Mesure arrondie ponctuelle ; aucune réservation d'espace ni quota disque garanti |
| Compte effectif | Authentifié, non SYSTEM, rôle Administrateur non actif | Ne prouve ni compte dédié ni absence d'appartenance Administrateurs via UAC filtré |
| Services nommés | CryptSvc actif/automatique ; KeyIso et VaultSvc actifs/manuels | État observé seulement ; aucun démarrage ou changement de configuration |
| Horloge monotone | Monotone et non ajustable selon Python | Deadlines natives rejouées ; reboot physique et synchronisation civile non qualifiés |
| Autorisation C0 historique | Non activée dans l'environnement des tests | Les trois essais qui modifieraient une nouvelle DACL native restent exclus |

### Maintenance OS et runtime : blocage indépendant des tests

Le support ordinaire Windows 10 22H2 est terminé depuis le 14 octobre 2025.
La table Microsoft consultée le 15 novembre 2026 associe 19045.7846 à la mise
à jour du 10 novembre et affiche aussi 19045.7850 du 13 novembre. L'inscription
ESU, l'applicabilité de cette dernière mise à jour et l'état de redémarrage
restent à confirmer par l'opérateur ; une build récente ne les prouve pas.
Le statut d'activation et d'inscription ESU n'a pas été qualifié ; aucune
licence ou inscription n'a été modifiée.
[Source Microsoft](https://learn.microsoft.com/en-us/windows/release-health/release-information).

OpenSSL indique la fin du support public de la branche 3.0 au 7 septembre 2026.
La présence d'un fournisseur assurant des correctifs étendus pour ce runtime
n'a pas été démontrée : **NO-GO pour son futur emploi avec un secret réel et
du WAN tant qu'une distribution maintenue n'est pas qualifiée**.
[Source OpenSSL](https://openssl-library.org/post/2026-09-16-eol30/).

CPython publie des corrections de sécurité postérieures à 3.12.10, notamment
3.12.16. La branche 3.12 est en correctifs de sécurité uniquement ; 3.12.10 était
sa dernière version avec installateurs binaires classiques. Le choix d'un build
Windows maintenu, sa provenance et les correctifs effectivement embarqués restent
à établir. Une mise à niveau ne doit pas modifier implicitement le Python de
publication ; aucun remplacement ni compilation n'est réalisé dans F4-A.
[Source CPython](https://www.python.org/downloads/release/python-31216/).

Ces constats ne constituent pas un diagnostic d'exploitation d'une CVE précise.
pip-audit porte sur les dépendances Python déclarées, pas sur la maintenance du
système, du runtime ou de ses bibliothèques embarquées. Son succès ne lève pas
ce blocage.

## ACL NTFS et stockage privé

Inspection native par credential_acl.inspect puis validate existants, sans
exception ou assouplissement. Le validateur exige propriétaire de confiance,
DACL non nulle, absence de reparse, aucun accès tiers sur la feuille, et aucun
droit tiers dangereux de remplacement/mutation sur la chaîne des parents.
QualificationArea exige en plus une DACL de répertoire protégée de l'héritage.

| Répertoire examiné, contenu non consulté | DACL protégée | ACE effectives Allow hors confiance sur la feuille | Parents avec mutation hors confiance | Validateur strict |
| --- | --- | --- | --- | --- |
| Workspace courant | Non | 5 | 2 | Refus |
| Temp courant | Non | 3 | 0 | Refus |
| LocalAppData courant | Non | 1 | 0 | Refus |
| ProgramData | Oui | 2 | 0 | Refus |
| Profil courant | Oui | 0 | 0 | Passe pour ce répertoire et sa chaîne uniquement |

Les propriétaires des cinq feuilles sont dans la liste de confiance ; aucune
DACL nulle ou réanalyse n'a été observée dans ces chaînes. Une ACE Allow hors
confiance suffit au refus de feuille selon le validateur conservateur, sans
revendiquer une analyse exhaustive des accès effectifs de Windows.
Les résultats sont ceux de cet audit ; ils ne reprennent pas les constats
historiques C0 comme s'ils décrivaient encore toutes les ACL actuelles.

Le profil qui passe ne devient **pas** un magasin applicatif approuvé. Aucun
futur enfant, fichier, propriétaire d'un autre compte, journal SQLite ou backup
n'y a été créé/vérifié. Temp reste réservé aux données synthétiques des tests,
et le workspace n'est pas proposé pour le stockage privé réel.

Recommandation minimale à faire approuver séparément : compte Windows dédié
au contrôleur, non élevé, et un nouveau sous-répertoire de son profil, sous
réserve d'une validation de toute sa chaîne. Chemin et compte exacts doivent
être approuvés localement avant création ; le profil audité aujourd'hui ne
qualifie pas celui d'un futur compte. Aucune création directement sous C: n'est
proposée pour F4-A, et l'autorisation C0 consommée n'est pas réutilisable.

Avant réel, qualifier ensemble base, marqueur, verrous, -journal DELETE,
configuration, ticket d'arrêt, résultats du worker, logs, sauvegardes et
checkpoints. Refuser -wal/-shm inattendus, liens, hardlinks et parent dangereux,
sans corriger ni effacer pour contourner le contrôle. Prévoir quotas existants,
espace disponible, rotation bornée et sauvegarde SQLite cohérente ; restauration
toujours désarmée, sans perte de tombstones ou réémission. Les droits de lecture,
remplacement et suppression par un autre compte doivent être testés sur une
fixture expressément autorisée. Chiffrement disque, dumps et pagefile ne sont
pas qualifiés dans cet audit.

## Qualification future de Credential Manager — plan non exécuté

Le fournisseur actuel reste PinnedSyntheticProvider/SyntheticVault, explicitement
épinglé et désactivé par défaut ; DPAPI en mémoire, format synthétique fermé,
aucun CredRead ou fallback de production. Le service VaultSvc actif ne prouve
pas le comportement d'une entrée, ni l'autorisation d'un essai.

Proposition à soumettre à autorisation distincte : cible unique de fixture
`NewsTelegram.Private.Qualification.<UUID>`, type CRED_TYPE_GENERIC, identité
bot/génération/cible épinglées et CRED_PERSIST_SESSION à qualifier. Cette portée
est liée à la session d'ouverture de Windows, pas à la durée de vie du contrôleur ;
elle disparaît à la déconnexion et n'isole pas les autres processus de cette
même session. Ni LOCAL_MACHINE ni ENTERPRISE ne sont choisis implicitement.
[Contrat CREDENTIALW](https://learn.microsoft.com/en-us/windows/win32/api/wincred/ns-wincred-credentialw).

CredRead utilise le jeu de credentials de la session utilisateur, pas une
identité d'exécutable. **Un autre processus de cette même identité ne peut pas
être considéré comme exclu par le seul nom de cible.** Compte dédié, poste de
confiance et limites F1 restent nécessaires ; l'enrôlement et l'autorisation
d'essai doivent être distincts du credential.
[Contrat CredReadW](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credreadw).

CredWrite peut remplacer une entrée de même nom/type : il ne fournit pas ici
une création exclusive atomique démontrée. UUID et contrôle préalable ne sont
pas une preuve contre une écriture concurrente. Le protocole futur doit préciser
la cible autorisée, le contrôle d'absence et ses droits de lecture, l'exclusivité
des écrivains et le traitement d'une collision avant tout appel. Une vérification
CredRead ne peut pas être présentée comme une inspection sans accès à l'entrée.
Collision ou preuve douteuse : arrêter, aucun écrasement/suppression d'existant.
[Contrat CredWriteW](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credwritew).

Plan de tests à approuver : secret synthétique uniquement ; bonne/mauvaise cible,
absence, taille/type/persistance, identité de bot incorrecte, autre compte Windows,
même session contre autre session, fermeture/reboot simulé, révocation locale,
transfert borné, créations concurrentes et absence de divulgation. Nettoyage
seulement de l'entrée identifiée comme appartenant à l'essai ; contrôle de
propriété et contenu de test avant suppression, arrêt en cas de doute.
Pas de cmdkey /list, énumération globale ou consultation d'entrée existante
dans F4-A. Une suppression locale ne révoque pas un credential chez son fournisseur.

## Passage futur du réseau local au distant : écarts minimaux

Ne pas relâcher LocalEndpoint, l'audit réseau F2 ou les types exacts F3 pour faire
passer le réel. Garder ces fixtures reproductibles et le même contrôleur, sans
second lecteur, collecteur, framework réseau ou service. Les changements suivants
sont une analyse, pas du code activable livré par F4-A.

| Écart | Travail futur minimal et preuve requise |
| --- | --- |
| Configuration distante | Contrat privé explicite épinglant HTTPS et un hôte/port approuvé ; candidat api.telegram.org:443, cité seulement comme texte sans résolution F4-A ; aucun proxy, URL libre, userinfo, redirect ou retry |
| DNS et destinations | Résolution dans le worker borné ; politique IPv4/IPv6/peer à définir, test de rebinding et adresses non autorisées ; ne pas confondre validation initiale d'URL et confinement de toutes les connexions |
| TLS | SNI et hostname attendus, CERT_REQUIRED, racines de confiance approuvées, tests de CA inconnue, faux nom, expiration et handshake bloqué ; qualification WAN séparée |
| Secret dans la requête | Le worker F2 consomme/efface le faux secret avant HTTP et emploie /fixture ; il ne peut pas émettre une vraie route authentifiée tel quel. Revoir le cycle du secret dans le seul worker, sans argv/env/config/exception/log contenant la valeur |
| HTTP et résultat | Compatibilité distante Content-Length/chunked/compression non observée ; conserver refus fermé et tailles/budgets, qualifier avant toute extension du parseur |
| Contrats transactionnels | Interface future fermée revue explicitement ; pas de suppression des contrôles exacts de type de F3 ou de deliver. Intention/quota avant reprise, validation avant commit B2, uncertain sans réémission |
| Contrôleur et opérateur | Entrée réelle manuelle et finally/arrêt à qualifier sous le compte cible ; le CLI livré reste --fixture-stop, pas une interface réelle déjà approuvée |

Le budget global doit couvrir DNS, connexion, TLS, écriture et lecture dans le
job C2 ; des timeouts socket isolés ne suffisent pas. Après terminaison, prouver
job vide et racine signalée avant libération, sans promettre un délai matériel
absolu ou l'arrêt de tous les services réseau Windows. Perte de preuve = blocage.

Le futur contexte TLS doit aussi éviter une journalisation de clés activée par
l'environnement : create_default_context peut utiliser SSLKEYLOGFILE. L'environnement
du worker F2 reste sur liste blanche ; conserver cette protection et vérifier
explicitement l'absence de keylog dans la qualification future.
[Documentation Python ssl](https://docs.python.org/3.12/library/ssl.html).
Aucun serveur Telegram ou DNS distant n'a été interrogé pour cet audit.

## Revue B1/B2 et arrêt F3

Le diff des sources et tests par rapport à origin/main est nul. Aucun schéma,
mode de transaction ou protection n'est modifié par F4-A. La suite complète
rejoue les tests F2/F3, dont duplication ciblée des handles, six qualifications
natives F3, 13 frontières de crash SQLite et mort réelle du contrôleur après
acceptation locale. Les garanties restent celles du périmètre synthétique.

Points de revue confirmés : reçus et curseur atomiques ; prochain offset issu
uniquement du magasin ; authentification avant snapshot ; deux commandes exactes ;
intention B2 avant transport ; quota C3 avant reprise ; résultat attempts/outbox
atomique ; état ambigu sans seconde tentative ; récolte avant déverrouillage ;
ticket/PID seuls insuffisants ; retired/recovery_required/RESTORE_BARRIER conservés.

Le plafond de six réponses borne les opérations réseau admises par C3. Une
septième intention B2 conservatrice peut être enregistrée uncertain si le quota
refuse sa création de worker : ce n'est ni une septième émission ni un quota
remboursable. Le runbook ne doit pas compter les seules confirmations.

Le secours F3 retourne controller_closure=false même s'il a prouvé le worker
arrêté : le contrôleur doit encore récolter ses propres handles et committer
son désarmement. Entre workers, un événement signalé interdit la prochaine
reprise, mais l'absence de job ne permet pas d'inventer une preuve d'arrêt global.
Après reboot/handles perdus, aucun retour automatique à sealed ou active.

## Matrice GO / NO-GO

GO ci-dessous signifie seulement que la condition indiquée est établie dans
son périmètre ; il ne compose jamais un GO réel si une autre ligne bloque.

| Condition | Type de preuve | État | Autorisation / preuve restante |
| --- | --- | --- | --- |
| Base F3 fusionnée et CI verte | GitHub vérifié | GO audit | Aucun droit de fusion ou d'activation accordé par ce constat |
| WinAPI job/duplication/événement/DPAPI | Tests Windows natifs de fixtures | GO local | Requalifier le runtime et le compte réellement retenus |
| Transactions, déduplication et arrêt F3 | SQLite réel, WinAPI et modèles identifiés | GO local | Pas de garantie WAN ou de coupure électrique |
| Maintenance Windows/ESU et reboot en attente | Build native et documentation ; preuves partielles | NO-GO réel | Vérification opérateur ciblée et remédiation autorisée séparément si nécessaire |
| Runtime Python/OpenSSL maintenu | Versions natives et cycles officiels | NO-GO réel | Build maintenu/correctifs et provenance prouvés, puis mêmes tests, sans toucher implicitement la publication |
| Compte standard dédié | Non établi | NO-GO réel | Compte exact et mode de lancement approuvés, droits/accès tiers qualifiés |
| Magasin et annexes privés | Profil ACL positif seulement ; autres feuilles refusées | NO-GO réel | Nouvelle fixture/cible précise autorisée, ACL natives, annexes, backup et restauration |
| Credential Manager | Analyse seulement ; aucun accès natif au magasin | NO-GO réel | Autorisation de cible synthétique et protocole absence/collision, puis tests et nettoyage |
| DNS/TLS/WAN et secret réel | Non testés, transport incapable de réel | NO-GO réel | Développement et qualification explicitement autorisés, destinations précises, aucun raccourci depuis le POC |
| Commande opérateur sous compte cible | CLI de fixture native prouvée | NO-GO réel | Chemin/entrée réelle, accès et secours opérateur à éprouver |
| Identités bot/propriétaire/chat et exclusivité distante | Synthétiques seulement | NO-GO réel | Enrôlement manuel local et préflights sous autorisation distincte ; un verrou local ne prouve pas un lecteur unique distant |
| Essai 300 s / 20 lectures / 6 tentatives | Compteurs natifs/modèles, proposition de runbook | NO-GO réel | GO humain final lié au SHA, poste, génération, IDs locaux, méthodes et révocation |

## Validation F4-A et limites

Python 3.12 : **441 tests en 130,540 s, succès, 4 exclusions**. Les 16 tests F3,
dont six natifs Windows, et tous les tests F2 sont conservés et passent. Trois
tests C0 nécessitant une nouvelle DACL native ne sont pas autorisés ; le quatrième
exclu porte sur les modes POSIX. Aucun test de lecture seule ne les remplace par
une qualification de magasin. Pas de nouveau test redondant pour cette PR sans
changement de code ; les contrôles natifs ci-dessus complètent la suite existante.

Ruff et compileall réussissent. pip-audit production puis production+développement :
aucune vulnérabilité connue. Les CI du SHA livré et la différence entre Ubuntu
et Windows sont consignées dans la PR. Les audits consultent les services de
développement nécessaires, distincts du banc privé ; aucune API Telegram utilisée.

Ordre proposé pour une décision ultérieure : auditer humainement F4-A ; choisir
le runtime maintenu et le compte ; autoriser précisément les seules fixtures
stockage/credential nécessaires ; décider d'une mission suivante limitée ;
qualifier ensuite réseau et exploitation ; enfin demander un GO d'essai séparé.
F4-B n'est pas déduit de la publication ou fusion de ce rapport.

Pas d'ACL/entrée Credential Manager modifiée, de secret réel, d'installation
changée, de nouveau répertoire sous C:, de Telegram, de bot de publication,
watcher, state.json, workflow, flux, FIFO, cadence ou journal confirmé 1947 touché.
Aucun service/tâche lancé, déploiement, démarrage automatique, fusion ou activation.
