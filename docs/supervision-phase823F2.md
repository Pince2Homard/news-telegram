# F2 — credential privé synthétique et adaptateur local désactivé

## Verdict et périmètre

**F2 VALIDÉE POUR POURSUIVRE HORS LIGNE**, dans le périmètre synthétique testé
ci-dessous. **Mise en service réelle : NO-GO.** Aucune autorisation F3,
qualification de stockage persistant Windows ou connexion Telegram n'en découle.

Prérequis vérifiés avant modification : PR #37 fusionnée au commit exact
`aa8c21d06c4c0281dc17c81f44c0bf09f70592e0` et
[CI post-fusion 43648227493](https://github.com/Pince2Homard/news-telegram/actions/runs/43648227493)
terminée avec succès. Dernier origin/main récupéré, checkout initial propre,
branche `codex/phase-f2-private-storage-transport`. Les conclusions F1 et C0–C5
restent applicables ; aucune autorité externe expérimentale ajoutée.

## Architecture retenue et composants

Une seule chaîne de fixtures réemploie C1/C2/C3 : fournisseur explicitement
épinglé → autorisation et quotas C3 → worker suspendu créé atomiquement dans
le Job Object C2 → transfert borné → HTTP/TLS local → récolte OS → validation.
Aucune boucle de collecte, découverte de transport ou entrée automatique.

| Fichier | Modification |
| --- | --- |
| tools/windows_watcher/private_credential.py | Fournisseur synthétique épinglé, DPAPI en mémoire, duplication ciblée du pipe |
| tools/windows_watcher/private_bot_adapter.py | Contrats des quatre méthodes, configuration locale fermée, exécution explicite et résultat expurgé |
| tools/windows_watcher/private_fenced_job.py | Sélection explicite de l'exécutable et lecture stricte de la configuration de fixture |
| tools/windows_watcher/private_local_worker.py | Consommation du handle dupliqué, requêtes bornées, lecture HTTP stricte et timeout d'E/S |
| tools/windows_watcher/private_local_lab.py | Réponses HTTP synthétiques et taille annoncée contrôlables |
| tests/test_private_transport.py | 14 tests F2, dont quatre tests natifs Windows et de nombreux sous-scénarios |
| tests/test_private_trial.py | Assertions C3 adaptées au lancement sans héritage de handles |
| docs/supervision-phase823F2.md | Décision, preuves, exclusions et portes restantes |

Pas de dépendance ajoutée. Aucun changement de private_outbox.deliver,
private_store, schémas, sauvegardes, routeur, watchdog de production ou workflows.

## Fournisseur et stockage des secrets

PrivateFixtureTarget lie une cible UUID unique, un bot numérique et une
génération UUID ; le bot et la génération doivent correspondre à l'enrôlement
manuel C3. PinnedSyntheticProvider est désactivé par défaut et ne recherche
aucune cible. Absence, révocation ou discordance : refus avant tentative.
Il n'existe aucun repli vers le credential de publication.

Le stockage effectivement implémenté et qualifié est **DPAPI utilisateur en
mémoire**, réutilisant SyntheticVault. Son format fermé ne peut contenir qu'un
secret synthétique ; un token Telegram est refusé. Aucun secret, même chiffré,
n'est écrit sur disque par ce fournisseur. Il est fermé après une opération,
y compris si son acquisition est refusée ; les buffers détenus sont effacés.
La présence du fournisseur ne vaut jamais autorisation C3.

Ce n'est **pas un fournisseur Windows persistant qualifié**. Aucune lecture ou
création Credential Manager, aucun changement ACL ou nouveau compte Windows
n'a été effectué. La future stratégie persistante proposée reste une entrée
Credential Manager dédiée, nom exact et identité épinglés, sans énumération,
recherche automatique ou fallback. Elle exigera une qualification distincte.
Les garanties C0 de propriétaire/DACL/parents, SQLite et annexes demeurent
requises pour le futur emplacement. Temp ne devient pas un emplacement de
production approuvé parce que les fixtures s'y exécutent.

Si cette qualification est demandée ultérieurement, faire autoriser séparément :

1. Compte et chemin de fixture précis, vérification préalable d'absence et des
   parents ; création de la seule fixture et DACL restrictive de ce nouveau
   contenu, jamais assouplissement des parents ou ACL existantes.
2. Nom exact d'une nouvelle entrée Credential Manager de test, confirmation
   qu'elle n'existe pas, création/lecture/suppression uniquement de ce secret
   synthétique sous l'identité approuvée. Aucun credential existant consulté.
3. Essai avec un autre compte Windows approuvé, vérification des refus,
   nettoyage après contrôle d'identité et inventaire, arrêt en cas de doute.

Ces opérations ne sont ni exécutées ni implicitement autorisées par F2.

## Correction de la fenêtre d'héritabilité

L'ancien protocole C3 rendait temporairement le pipe héritable dans le parent.
Une liste de handles dans le lancement légitime ne protège pas contre un autre
lancement simultané à héritage large. F2 supprime cette condition à la source :

1. CreatePipe sans SECURITY_ATTRIBUTES héritable ; les deux handles du parent
   restent non héritables pendant toute leur durée de vie.
2. CreateProcessW avec bInheritHandles=False, CREATE_SUSPENDED et JOB_LIST,
   puis vérification IsProcessInJob. Aucun repli création puis affectation.
3. DuplicateHandle copie uniquement le handle de lecture dans ce processus
   suspendu, avec bInheritHandle=False ; fermeture immédiate de la copie source.
4. Le numéro de handle, valable seulement dans le worker, est placé dans la
   configuration de fixture. Ce numéro n'est ni un credential ni un droit
   transférable à un autre processus. Aucune valeur secrète dans argv/env.
5. Une seule petite trame entre dans le pipe vide de 4096 octets ; fermeture du
   handle d'écriture et du vault avant ResumeThread. Le worker vérifie une
   longueur maximale de 128 octets et le format synthétique, efface et ferme
   avant toute E/S réseau. Échec : arrêt/récolte du job, aucun rejeu du secret.

Le worker utilise explicitement le même CPython de base, en mode -I et stdlib.
Le launcher du virtualenv créerait un processus supplémentaire qui ne recevrait
pas le handle dupliqué non héritable. Les autres usages C1/C2 conservent leur
exécutable historique ; aucun changement d'installation Python.

La correction repose sur les propriétés documentées de
[DuplicateHandle](https://learn.microsoft.com/en-us/windows/win32/api/handleapi/nf-handleapi-duplicatehandle)
et des [conditions d'héritage Windows](https://learn.microsoft.com/en-us/windows/win32/procthread/inheritance).
Elle dépasse la seule whitelist du worker : aucun handle de secret héritable
n'existe dans le parent. Huit processus concurrents à héritage large ont été
testés pendant cette fenêtre ; aucun accès au pipe. La copie du worker est
elle aussi non héritable par ses descendants.

Limites : un processus malveillant disposant déjà de PROCESS_DUP_HANDLE,
un administrateur ou un OS compromis ne sont pas contenus par ce protocole.
Les tests ne simulent pas une attaque d'un autre compte réel. Les handles
process/thread de supervision restent détenus jusqu'à la preuve d'arrêt C2,
puis fermés ; ils ne sont pas remplacés par une preuve fondée sur le PID seul.
L'effacement des buffers détenus n'est pas une garantie d'effacement complet
des copies Python, de la mémoire OS, des dumps ou du pagefile.

## Adaptateur et isolation réseau

LocalBotAdapter est désactivé par défaut. Son seul endpoint est un port entier
explicitement fourni sur l'adresse littérale 127.0.0.1, avec TLS booléen explicite.
URL, hostname, localhost, IPv6, autre adresse, port booléen ou configuration
ambiguë sont refusés avant création du worker. Aucun endpoint distant caché.

Les méthodes getMe/getWebhookInfo/getUpdates/sendMessage ne sont que des
contrats de fixtures. Les requêtes portent /fixture/méthode, **sans token**,
jamais une route Telegram. Les deux premières exigent un objet vide ; réception
bornée à 20 et paramètres entiers stricts ; réponse au chat épinglé uniquement,
texte borné et mode HTML explicite. getMe vérifie le bot ; getWebhookInfo exige
l'absence de webhook. Contrats C1/C2 réutilisés pour messages et updates, avec
rejets minimaux sans contenu tiers. Aucun avancement de curseur par l'adaptateur.

Le worker fixe crée une seule socket TCP IPv4 neuve. Le hook d'audit refuse
DNS, UDP, bind et toute connexion autre que l'unique couple 127.0.0.1/port.
Pas de proxy système, de pool, de connexion réutilisée ou de résolution DNS.
TLS utilise exclusivement l'autorité de test fournie et vérifie certificat
et adresse. Aucun suivi de redirection, même locale ; aucune seconde tentative.
Le serveur de test écoute uniquement sur 127.0.0.1 et ne crée pas de socket
sortante. Les tests vérifient les pairs observés et injectent directement les
événements refusés sans essayer une connexion externe.

Ces preuves concernent le code de fixture de confiance et ses appels stdlib,
pas un pare-feu OS capable de contenir du code natif hostile. Aucun paquet
destiné à Internet n'est nécessaire aux tests privés. Les audits de dépendances
et opérations GitHub restent des vérifications de développement distinctes.

Bornes : JSON de requête 8192 octets, fichier de configuration 16384,
en-têtes HTTP 8192, corps de réponse 65536. Refus des clés JSON dupliquées,
NaN, cadrage ambigu, headers dupliqués, Transfer-Encoding, longueur absente ou
excessive et UTF-8 invalide. Les résultats suivent aussi les limites C1.
Socket timeout F2 par défaut 0,5 s, configurable entre 0,05 et 3 s ; budget
global du job par défaut 3 s, réduit au temps restant C3. Il couvre lancement,
transfert, connexion, TLS, écriture et lecture ; la terminaison/récolte reste
obligatoire avant déclaration d'arrêt sûr, même si elle déborde le budget utile.

## Résultats, incertitude et compatibilité transactionnelle

- Refus de configuration, credential, quota ou création avant ResumeThread :
  refus certain avant envoi. Le quota éventuellement réservé n'est pas rendu.
- HTTP 400/401/403/409/429 avec enveloppe cohérente : rejet explicite, codes
  fixes et retry_after borné retournés sans description ; aucun retry exécuté.
- Réponse HTTP 200 cohérente et arrivée à temps, génération validée par C2 et
  job récolté : résultat de transport validé. La classification « confirmed »
  de cette interface locale **ne signifie pas un commit durable B2**.
- HTTP 500, cadrage/JSON incohérent, timeout, reset, redirection, réponse tardive
  ou perdue après tentative possible : uncertain. L'autorisation est suspendue,
  aucune réémission, même avec une nouvelle fixture et un nouveau fournisseur.

L'adaptateur ne remplace pas l'intention durable B2. Le raccord F3 devra écrire
in_flight avant toute tentative et committer résultat/outbox atomiquement ;
perte de réponse ou de commit doit conserver uncertain. Aucune promesse
d'exactly-once. private_outbox.deliver conserve son contrôle exact du type
ScriptedTransport ; aucun élargissement vers un transport arbitraire.

B1 conserve reçus/curseur atomiques et déduplication ; B2 intention/unicité ;
C0 sauvegarde cohérente et restauration désarmée ; C1/C2 budgets, arrêt et
fencing ; C3 quotas persistants, expiration et autorisation distincte. Aucun
import de l'adaptateur par le watcher ou le transport de publication.

## Validation et exclusions

Environnement natif : Windows 10 build 19045, Python 3.12.10, SQLite 3.49.1.

| Vérification | Résultat / portée |
| --- | --- |
| Suite complète Python 3.12, version finale | 425 tests, succès, 4 exclusions ; 97,685 s |
| F2 après dernière normalisation des refus d'acquisition | 14 tests, succès, aucune exclusion Windows ; 14,105 s |
| Ruff | Succès |
| compileall poster.py cadence_simulator.py newsbot tools tests | Succès |
| pip-audit requirements.txt | Aucune vulnérabilité connue |
| pip-audit requirements.txt + requirements-dev.txt | Aucune vulnérabilité connue |
| CI GitHub du SHA publié | Résultat et lien consignés dans la PR ; ne pas assimiler la CI Ubuntu à une preuve Windows |

Preuves **natives Windows** F2 : DPAPI synthétique ; chaîne fournisseur/quotas/
fencing/job/TLS local ; héritage concurrent large (8 processus) ; échecs injectés
de CreateProcessW et DuplicateHandle ; interruption avant transfert et écriture
partielle du pipe ; fermeture des handles et effacement des buffers ; deadline
globale de 0,4 s sur réponse tardive à 1 s avec zéro processus actif, absence
de résultat tardif et refus d'une deuxième opération. La suite existante rejoue
également les crashes contrôleur/descendants et gardes C0–C5.

Preuves de **boucle locale réelle** : quatre contrats positifs, erreurs HTTP
400/401/403/409/429/500, redirection refusée, troncature, JSON malformé, longueur
excessive, reset, silence en lecture, réponse tardive, TLS non approuvé et
handshake bloqué. Les descriptions sensibles de fixture ne sortent pas des
résultats expurgés. Un seul pair/une tentative, sans retry.

**Simulations explicites** : TimeoutError injectées pendant connect/sendall/
recv ; événements d'audit DNS/destination interdite injectés sans socket ;
changements d'horloge et reboot déjà simulés dans C0–C3. Ces simulations ne
qualifient ni DNS/WAN ni un vrai timeout d'écriture du futur adaptateur distant.
Les blocages d'écriture C1 et récoltes natives existantes restent couverts.

Les quatre exclusions Windows de la suite sont trois tests C0 exigeant une
nouvelle autorisation de fixture ACL native, et un test de modes POSIX. La
permission historique C0 consommée n'est pas réutilisée. Les tests de modèle
ACL continuent à tourner, mais aucune nouvelle preuve ACL positive n'est
revendiquée. Sous Linux, les tests Windows sont exclus ; les contrats, modèles
et échanges de boucle locale s'exécutent, sans qualifier WinAPI ou DPAPI.

La suite publication existante est incluse et passe. Aucun state.json,
workflow, watcher de production, source, FIFO, cadence, journal confirmé,
credential réel ou installation de production modifié par cette livraison.

## Risques restants et conditions avant F3 / réel

| Point | État et décision |
| --- | --- |
| Stockage persistant et autre compte Windows | Non qualifiés ; autorisations de fixtures ciblées et preuves nécessaires avant credential réel |
| Jonction intention/résultat B2 | Réservée à F3, crash avant/après commits à éprouver ; résultat transport seul insuffisant |
| Arrêt opérateur indépendant | Primitives conservées ; interface et secours à qualifier en F3 |
| Reboot, restauration et clonage | Handles perdus ; retired/recovery_required conservés, aucun reset ou redémarrage automatique ; pas d'anti-rollback coordonné démontré |
| DNS/TLS/WAN Telegram | Aucun essai ; futures destinations exactes et autorisation distincte obligatoires |
| Même utilisateur malveillant/admin/OS compromis | Pas de garantie d'isolation hostile ni d'effacement mémoire absolu ; limites F1 à accepter explicitement |
| Exclusivité distante | Un verrou local ne prouve pas l'absence d'un autre lecteur ; aucun Pi ou basculement ajouté |

Pour engager F3 : revue et acceptation humaines de cette PR et de ses limites,
autorisation distincte de F3 avec contrat de résultat/admission et tests des
frontières transactionnelles. Si une fixture native supplémentaire est requise,
faire approuver ses opérations exactes avant création. Ne pas convertir une
preuve manquante en avertissement permettant le réel.

Avant tout essai réel restent notamment : compte/chemin/parents/annexes et
fournisseur persistant qualifiés, enrôlement privé manuel, autorisation liée au
SHA et à l'hôte, contrôle d'exclusivité et webhook sous permission, whitelist
/help et /status, quotas globaux 300 s / 20 lectures / 6 tentatives couvrant
les préflights, arrêt opérateur éprouvé et vraie jonction B1/B2 qualifiée.
Les conditions et acceptations F1/C4 non démontrées maintiennent NO-GO.

Livraison limitée à une PR F2 ; aucune fusion, activation, connexion Telegram,
service, tâche planifiée ou phase suivante.
