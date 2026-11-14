# Phase 8.2.3-C1 — worker privé hors ligne et qualification Windows

Prérequis vérifiés avant modification : #31 fusionnée au SHA
`0d628a4c7bc37fc87fe52673cb5bc875ecdf9553`, CI post-fusion
[43611784687](https://github.com/Pince2Homard/news-telegram/actions/runs/43611784687)
terminée avec succès, dernier origin/main récupéré. Branche dédiée C1 ; fichiers
locaux préservés. Aucun transport Telegram actif ni accès à un credential.

## Architecture et périmètre réellement exécuté

Quatre modules indépendants du transport ponctuel/publication existant :

| Module | Responsabilité |
| --- | --- |
| private_contracts | Décodage borné et contrats purs de futures réponses privées |
| private_job | Job Object Windows, création suspendue, échéance, récolte et verrou de fixture |
| private_local_worker | Worker fixe isolé : HTTP de fixture et TLS sur 127.0.0.1 uniquement |
| private_local_lab | Serveur de test local, certificat synthétique éphémère et pannes locales |

Le banc utilise POST `/fixture`, avec une charge synthétique sans token ou ID
personnel. Il n'a aucune route Bot API. `getUpdates` et `sendMessage` désignent
uniquement les futurs contrats purs de validation ; aucune de ces méthodes
n'est appelée. Le routeur, le watcher, les magasins B1/B2/C0 et leurs contrôles
ne sont pas modifiés ou raccordés à un nouveau collecteur/orchestrateur.

## Isolation du banc local

Le serveur ne prend aucun hostname/URL en paramètre : bind AF_INET/TCP sur
`127.0.0.1:0`, port obtenu de ce listener, conservé ouvert pendant l'essai.
Windows utilise SO_EXCLUSIVEADDRUSE. Les adresses réellement acceptées sont
vérifiées et restent toutes `127.0.0.1`.

Le worker ne prend pas de destination distante : seul le port du banc est
fourni, dans un fichier de fixture de taille bornée et à clés fermées. Le
connect est toujours vers l'IPv4 numérique `127.0.0.1`. Un hook d'audit installé
avant toute socket refuse résolution DNS réelle, UDP/IPv6, bind et connect vers
une adresse ou un port différents. Les tests évaluent les refus sur événements
synthétiques, sans essayer d'ouvrir une connexion Internet. Un champ host/URL
supplémentaire est refusé avant toute socket. Pas de proxy, retry ou redirection.

Cette isolation est celle d'un programme de test fixe et contrôlé, pas un
pare-feu Windows ou un sandbox capable de contenir du code natif malveillant.
Le worker n'importe aucun plugin ni callable de transport arbitraire. Les
requêtes ne contiennent aucune identité réelle. Le Job Object contrôle les
processus, pas leur routage IP. La qualification Internet réelle reste interdite.
Les seuls accès Internet des outils de livraison sont GitHub, documentation
technique et audit des dépendances, distincts du banc réseau et jamais Telegram.

## TLS de test sans installation ni secret réel

Windows génère une clé RSA synthétique avec RSACng en mémoire ; IsEphemeral
doit être vrai. CertificateRequest crée un certificat de test auto-signé avec
SAN IP 127.0.0.1. Il n'est importé dans aucun magasin système. La clé est
exportée seulement pour les fichiers éphémères du serveur de fixture ; aucun
matériau de clé n'est versionné ou imprimé. L'encodage PKCS#1 est un assemblage
DER ; génération RSA et signature sont réalisées par .NET, pas par une nouvelle
implémentation cryptographique du projet. Sous Linux, les certificats de
fixtures viennent d'OpenSSL local, sans accès à une CA distante.

Le client emploie un SSLContext privé, CERT_REQUIRED, check_hostname=true,
TLS >=1.2 et uniquement le certificat de fixture en confiance. Aucune recherche
de CA réseau, aucun changement de certificat système ou d'ACL. Un certificat
de fixture différent est réellement refusé. Les fichiers sont créés dans des
TemporaryDirectory de tests, sans nouveau dossier directement sous C: et sans
réutilisation de l'autorisation C0 déjà consommée. Ils sont nettoyés après fin
du worker et du thread serveur.

## Qualification du Job Object Windows

Les layouts ctypes sont vérifiés sur Windows 64 bits : ExtendedLimit 144 octets
et STARTUPINFO 104 octets. Limites effectives relues : KILL_ON_JOB_CLOSE activé,
pas de BREAKAWAY/SILENT_BREAKAWAY, plafond de huit processus. Les descendants
ordinaires restent dans le job. Référence :
[Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).

Ordre testé :

1. Prendre le verrou OS de fixture, démarrer l'échéance monotone unique.
2. Créer le job anonyme non hérité, appliquer ses limites.
3. CreateProcess avec CREATE_SUSPENDED, CREATE_NO_WINDOW, bInheritHandles=false.
4. Affecter au job avant ResumeThread. Échec d'affectation : terminer et attendre
   le processus encore suspendu ; aucun fallback hors job.
5. Environnement du worker reconstruit sur liste blanche, sans copie de celui
   du parent : SystemRoot et options Python fixes. Exécution Python `-I`, pas
   de profil, PYTHONPATH ou secret hérité. Un marqueur synthétique présent dans
   l'environnement parent est effectivement absent dans le worker.
6. Attendre sous la même échéance, puis terminer tout le job, y compris en cas
   de sortie normale du worker qui aurait laissé un descendant.
7. QueryInformationJobObject doit indiquer ActiveProcesses=0 et le handle du
   processus racine doit être signalé. Alors seulement fermer les handles et
   libérer le verrou. La trace doit ordonner reaped_zero avant lock_released.

Références : [flags de création](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags),
[limites de job](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-setinformationjobobject),
[compteurs de processus](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information).

Un arrêt impossible à prouver lève UnsafeStop, conserve les handles et le verrou
dans une quarantaine en mémoire ; aucun safe_stop=true, relancement ou libération
implicite dans ce chemin. Le test injecte un échec de TerminateJobObject,
vérifie que le verrou reste occupé, puis rétablit l'API de test et récolte les
processus avant nettoyage. Ce mécanisme n'est pas un processus de secours réel.
Un résultat déjà présent refuse le lancement ; un même contrôleur ne peut pas
être lancé deux fois. Ces refus empêchent la réutilisation d'une ancienne preuve.

## Budget monotone et réponses tardives

L'échéance part avant la création du job/processus : démarrage, DNS simulé,
connexion, handshake TLS, écriture, lecture et retour se partagent un seul
budget. Aucun renouvellement par phase. Les essais utilisent 0.5 à 3 secondes,
sans modifier les budgets proposés pour un futur essai réel.

Les timeouts de socket locaux ne remplacent pas cette échéance. Le parent attend
le processus jusqu'à la limite, termine le job et attend sa vidange. La récolte
possède une marge bornée distincte : au plus 2 s pour ActiveProcesses=0 puis
1 s pour le processus racine. Toute absence de preuve bloque la libération.
Les latences de planification/kernel ne sont pas des garanties temps réel.

Une réponse n'est exploitable que si le processus a terminé à temps **et** si
l'arrêt vérifié reste dans l'échéance ; sinon son fichier de résultat n'est
pas accepté. Un serveur local tente une réponse après le délai : worker déjà
terminé, aucun résultat tardif n'est repris. Arrêter un processus n'annule ni
des octets déjà transmis ni une acceptation distante : conserver uncertain.

### Blocages réellement testés

- DNS : blocage artificiel, aucune résolution réseau réelle (limite assumée).
- TCP : connexion locale réelle ; une perte réseau WAN n'est pas reproduite.
- TLS : handshake réel avec certificat synthétique, serveur local qui bloque.
- Écriture : TLS réel, serveur qui ne lit pas, buffers réduits pour provoquer
  une écriture réellement suspendue ; la phase write reste active à l'interruption.
- Lecture : requête locale reçue puis en-têtes retenus ; terminaison à échéance.
- Accumulation : délai avant connexion et réponse retardée, sans remise à zéro.
- Corps tronqué, fermeture, 302 sans suivi, JSON malformé et 429 synthétique :
  aucun succès inventé ou retry automatique.

## Mort du parent, descendants et limites de l'ordre des verrous

Un test lance un contrôleur, un worker et un descendant. L'observateur ouvre des
handles de **processus**, pas de job (un second handle de job empêcherait le
dernier close de déclencher kill-on-close). Le contrôleur meurt par os._exit,
sans appeler stop. Les handles du launcher, du worker et du descendant effectif
doivent tous être signalés avant que l'observateur considère l'arrêt vérifié.
La fermeture du dernier handle non hérité a donc effectivement tué l'arbre.
Le cas sortie normale du parent worker avec descendant encore vivant est aussi
testé : la fin du processus racine ne suffit pas, le job doit devenir vide.

**La mort brutale du propriétaire d'un verrou n'est pas un arrêt ordonné.**
Windows peut libérer ce verrou pendant la terminaison asynchrone du job. C1
ne prétend pas imposer un ordre atomique entre ces deux effets kernel. Aucun
résultat safe_stop n'est rendu par le contrôleur mort. L'observateur attend la
fin des handles connus avant de rouvrir l'état ou de poursuivre un test.

Avant un transport réel, une reprise doit réconcilier une preuve indépendante
de l'ancienne génération/job/processus, sans se satisfaire d'un verrou acquis
ou d'un PID seul réutilisable. L'exclusivité entre machines exige aussi un
fencing de l'ancien lecteur. Ce protocole de reprise opérationnelle n'est pas
implémenté comme activation dans C1 ; il reste une condition avant essai réel.

## Intention durable et contrats purs

Un test d'intégration B2 enregistre une intention in_flight, puis lance un
worker de réseau **local** qui bloque après réception de la requête. Le verrou
SQLite reste détenu pendant l'interruption. Le job est vidé/récolté avant que
l'appelant enregistre l'échec et ferme le magasin. À réouverture : uncertain,
aucune seconde tentative. Le simulateur B2 n'a pas été élargi pour accepter un
transport réel : ce test utilise son point d'injection de panne après intention.

`decode_envelope` refuse taille >65536 octets, UTF-8/JSON invalide, clés dupliquées,
NaN/Infinity et booléens mal typés. Les descriptions distantes ne sont pas
réémises. Les contrats ne journalisent ni corps, identité ou chemin.

`validate_updates` exige une liste bornée et des update_id entiers ordonnés,
sans doublon. Message privé direct et IDs configurés, bots/transferts/business
et intermédiaires refusés ; is_from_offline/via_business_bot sont également
rejetés. Un ID fiable refusé devient un reçu minimal sans contenu tiers. Un
lot structurellement douteux ne produit aucune permission d'avancer un curseur.
La classification de fraîcheur et l'enregistrement durable restent B1 ; ce
validateur ne fait aucun acquittement.

`validate_message` exige HTTP/API cohérents, message_id positif, destination
privée numérique exacte et texte rendu correspondant au HTML figé. Un résultat
incohérent reste non confirmable. Un rejet 4xx explicite cohérent est rendu sous
codes fermés ; retry_after est borné et ne constitue pas une autorisation de
retry. Rien ici ne prouve l'origine d'un JSON arbitraire ou un exactly-once réel.

## Résultats et distinctions de validation

22 nouveaux tests C1 : tous passent sur Windows, dont 11 tests natifs Job Object.
La suite complète compte 338 tests, OK avec 4 exclus sous Windows (tests C0
natifs non réautorisés/non répétés et cas POSIX). Aucun dossier C0 n'est recréé.
Mesure finale : 59.139 s, Windows 10 build 19045 AMD64, Python 3.12.10,
OpenSSL 3.0.16. Les résultats CI du SHA livré sont joints à la PR ; cette CI
Linux ne remplace pas les preuves natives ci-dessus.

Commandes de validation (environnement Python de développement existant) :

```text
python -m unittest discover -s tests -q
python -m ruff check .
python -m compileall -q poster.py cadence_simulator.py newsbot tools tests
python -m pip_audit -r requirements.txt -r requirements-dev.txt --progress-spinner off
```

Tests, Ruff et compileall : succès. pip-audit : aucune vulnérabilité connue sur
les dépendances production et développement. Les CI sont vérifiées séparément
sur le SHA publié, avec résultats joints à la PR.

| Niveau | Preuve apportée | Hors de cette preuve |
| --- | --- | --- |
| Windows natif | Job, affectation suspendue, limites, mort du parent, descendants, blocages TLS/écriture/lecture, récolte et ordre avant déverrouillage ordonné | Ordre atomique au crash du contrôleur, panne du kernel, pile WAN réelle |
| Boucle locale réelle | 127.0.0.1 seulement, certificat vérifié/refus d'autre CA, HTTP tronqué/erreur/retard, aucune redirection suivie | Telegram, Internet, proxies d'entreprise, droits du bot |
| Simulation/pur | DNS bloqué, contrats JSON, permissions/identités synthétiques, intention B2 et uncertain | Credential et enrôlement réels, observation de la Bot API |
| CI Linux | Contrats et banc local portable avec TLS ; tests antérieurs | Job Object et qualification Windows (explicitement ignorés) |

Les certificats/clefs sont générés en mémoire ou dans les seules fixtures ;
aucune installation système, dépendance ajoutée, ACL modifiée, service ou tâche.
Les fixtures de processus n'exécutent pas d'assistant autonome. Les erreurs de
banc ne révèlent ni corps, token ou configuration réelle.

## Conditions avant un premier essai Telegram (non autorisé par C1)

1. Revue de C1 et autorisation distincte, hôte/SHA/génération uniques ; aucun
   ancien lecteur ou webhook concurrent. Le verrou local ne prouve pas cela.
2. Enrôlement numérique manuel local : compte Telegram de confiance, défi privé
   borné, validation explicite from.id/chat.id et ID numérique du bot. Pas de
   confiance dans le pseudonyme, premier /start ou bot tiers ; pas d'IDs GitHub.
3. Qualification de l'emplacement réel, ACL, sauvegarde et restauration désarmée,
   preuve indépendante d'identité/génération contre clones et retours en arrière.
4. Concevoir/revoir un adaptateur réel séparé, destination/hôte fixes, secret
   hors arguments/environnement/logs et canaux d'entrée bornés. Les fichiers de
   commande C1 sont pour des fixtures non secrètes ; aucun passage de token réel
   n'est préparé par une option de ce worker.
5. Tester DNS réel, TLS réel de l'hôte autorisé, interruptions de la pile complète,
   mort du contrôleur et récupération des ressources avec le futur canal secret.
   Les propositions 20 s/25 s et marge de récolte doivent être approuvées.
6. Plan révocable : session manuelle au plus 5 minutes, 20 lectures/6 réponses,
   quotas persistants, stock ancien non exécuté, arrêt sur doute et aucune
   reprise automatique incertaine. Ce sont des propositions, pas une permission.
7. Aucun basculement Pi avant arrêt/fencing prouvé de l'ancien lecteur et
   qualification distincte du nouvel hôte. Aucune redondance n'est déployée ici.

Les transports de publication, le watcher, state.json, workflows, sources, FIFO,
cadence et journal confirmé 1947 sont inchangés. Aucun C réel, fusion ou phase
suivante n'est engagé. La livraison s'arrête à la PR et au rapport de qualification.
