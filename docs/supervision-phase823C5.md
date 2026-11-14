# C5 — autorité externe minimale, faisabilité et POC isolé

## Verdict : C5 À REVOIR

**Mise en service réelle : NO-GO.** La seule candidate retenue pour l'analyse
est **B2, autorité et point de sortie privés uniques**. B1 ne ferme pas le
fencing si un ancien PC possède encore un token utilisable directement.

Le banc B2 démontre des invariants de registre et des décisions de refus,
mais son test négatif rétablit une ancienne base d'autorité et reproduit un
effet synthétique déjà confirmé. L'anti-rollback de l'autorité elle-même reste
non résolu. Il manque aussi une preuve du contrôle effectif du credential et
de la sortie réseau. Il serait donc prématuré de retenir une architecture
opérationnelle satisfaisant toutes les exigences. Ce verdict ne sanctionne pas
l'absence volontaire de Telegram dans un POC ; il constate une limite de
sécurité effectivement reproduite, sans la transformer en avertissement.

## Préconditions et périmètre

Avant modification : #35 fusionnée au SHA exact
`efc135ba14cd013b7d6ae392abaccc177b069516` ;
[CI post-fusion exigée 43636445337](https://github.com/Pince2Homard/news-telegram/actions/runs/43636445337)
terminée avec succès. Dernier origin/main récupéré au SHA
`7d91fde0e0209c9c7440dc7a599a1a9a303c7797` ; checkout propre, branche
`codex/phase-8-2-3-c5-authority-poc`. La fin tronquée de la directive a été
complétée par l'utilisateur avant livraison.

Rapports [C1](supervision-phase823C1.md), [C2](supervision-phase823C2.md),
[C3](supervision-phase823C3.md), [C4](supervision-phase823C4.md) et modules privés
relus. Aucun module existant modifié. Un module de POC, ses tests et ce rapport
sont ajoutés ; aucun service, réseau, compte externe ou nouvelle orchestration.

## Trois garanties séparées

| Garantie | Propriété visée | Ce qui ne la démontre pas |
| --- | --- | --- |
| Anti-rollback | Un état ancien ne récupère pas permissions, quotas ou révocations consommés | Atomicité SQLite, DPAPI, signature d'un vieux checkpoint, double journal restauré ensemble |
| Exclusivité logique | Une seule consommation/exécution de la même intention dans l'état d'autorité courant | Un host_id copiable ne prouve pas qu'il n'existe qu'une machine physique |
| Fencing | L'ancien détenteur ne peut plus utiliser la capacité d'agir | Expiration d'un bail, refus d'un ticket, verrou Windows libéré ou génération locale modifiée |

Une requête déjà acceptée peut rester incertaine après n'importe quel fencing.
L'objectif est de ne pas la rejouer, pas de promettre un exactly-once distant.

## Comparaison B1 / B2 et décision d'architecture

| Dimension | B1 — permissions externes | B2 — autorité + capacité privée exclusive |
| --- | --- | --- |
| Secret | Le PC peut conserver le token ; l'autorité ne contrôle alors pas son usage direct | Le token ne quitte jamais le point de sortie ; le PC n'obtient aucun ticket d'envoi direct |
| Garantie démontrable | Consommation atomique, checkpoints et quotas tant que l'autorité ne recule pas | Mêmes invariants + contrôle central des opérations, sous preuve de séparation du secret/egress |
| Clone / ancienne génération | L'autorité peut refuser mais le clone peut ignorer ce refus et appeler directement | Un clone ne dispose plus du token ; l'exécuteur refuse les anciennes générations. Une identité cliente clonée nécessite encore authentification/politique et déduplication côté autorité |
| Réception | Un détenteur du token peut aussi concurrencer le lecteur | Toute capacité Bot API privée, lectures comprises, doit être centralisée |
| Crash du PC | Registre extérieur intact utile, mais ancien worker potentiellement encore capable d'émettre | Résultat durable consultable sans réexécution ; toute frontière inconnue bloque la suite |
| Restauration de l'autorité | Peut réhabiliter des permissions et quotas | Même problème ; une base SQLite unique n'est pas une ancre non restaurable |
| Disponibilité | Dépend de l'autorité avant chaque opération, sauf contournement dangereux | Autorité ou sortie indisponible : aucune opération. Pas de bypass direct autorisé |
| Exposition du secret | PC et copies/sauvegardes de ses capacités | Nouveau composant de confiance détenant le secret ; compromission de ce composant est critique |
| Exploitation | API de permissions + administration de son état, en plus du PC | Autorité et exécuteur dans un même programme et une même base, mais vrai périmètre secret/réseau à administrer |
| Compatibilité | Plus proche de C3, sans satisfaire le fencing | Change le rôle du canal C3 : il ne devra plus apporter un token Telegram au PC ; adaptation séparément autorisée nécessaire |

**Décision : écarter B1 comme solution de fencing ; garder B2 comme unique
candidate à réexaminer.** Aucun ajout de proxy générique, broker, consensus,
framework HTTP, SaaS imposé ou redondance. Une B2 future pourrait être un seul
programme de confiance, un seul exécuteur privé et une base locale à ce composant
indépendant du PC. Le POC n'est pas le déploiement de ce programme.

Une machine externe/administration indépendante est cependant un coût réel pour
un bot personnel. Si on ne peut pas établir son cycle de récupération sans
réhabiliter un ancien secret/état, aucune option étudiée ne satisfait actuellement
le modèle de menace avec une simplicité démontrée. Rester hors ligne plutôt que
d'ajouter un second témoin local présenté comme racine de confiance.

## POC : ce qui est implémenté

`private_authority.FixtureAuthority` est une machine de fixtures Python 3.12,
standard library et ScriptedTransport fermé existant. Aucun callable de transport
arbitraire, token, URL, socket, serveur ou client externe. `fault` ne sert qu'aux
injections contrôlées du banc. Les communications de tests multiprocessus sont
des appels au même fichier SQLite de fixture et des barrières de fichiers :
**pas une authentification réseau, pas une séparation de privilèges**.

Les TemporaryDirectory ont deux zones distinctes : `pc` et `authority`.
Leur indépendance physique, de sauvegarde et d'administration est simulée,
pas démontrée par leur présence sur le même disque de test.

Deux tables : métadonnées de l'essai et opérations. Génération externe entière
croissante liée à un hôte logique, révision CAS, checkpoint PC, échéance, quotas,
révocation et une seule opération pending. Cette génération externe n'est ni
l'UUID de déploiement B2 ni le nonce de session C2 : leur liaison future doit
être explicite, jamais une égalité implicite de types différents.

Le checkpoint est une valeur comparée à la valeur attendue, **pas une attestation
du contenu du magasin PC**. Un clone peut copier le checkpoint courant exposé
par status. Les quotas et l'unicité métier appartiennent donc à l'autorité,
sans se fier uniquement à la déclaration du client.

Deux copies présentant le même host_id ne sont pas détectées comme deux hôtes
physiques. Le banc sérialise leurs opérations, sans authentifier une machine ni
prouver qu'un message_id soumis correspond à une commande privée réellement reçue.
Le futur point de sortie devra lier chaque réponse à une admission authentifiée.
Il devra aussi conserver l'unicité bot/chat/message au-delà d'un changement
d'essai : ouvrir une nouvelle base/identité ne doit pas effacer cet historique.
Le POC est volontairement limité à un essai, sans procédure de réinitialisation.

DELETE, synchronous=EXTRA, trusted_schema=OFF, temp_store=MEMORY, busy_timeout
borné ; BEGIN IMMEDIATE pour les transitions, contraintes uniques sur op_id
et `(action, message_id)`. Les quotas 20 lectures / 6 réponses bornent les lignes.
Un nouvel op_id pour la même intention est refusé. L'empreinte du contenu de
requête est conservée avec l'ID et le résultat ; une répétition ne modifie ni
l'opération consommée ni son résultat et ne produit aucun nouvel effet. Elle
peut néanmoins faire avancer l'horloge ou suspendre l'essai avant son refus.
Le namespace de message est ici un unique bot/chat synthétique dans un essai ;
le futur protocole devra aussi lier durablement le namespace métier aux identités.

SQLite sérialise les écritures et peut refuser un écrivain concurrent ; cela
ne prouve pas le fencing réseau. Voir [transactions SQLite](https://www.sqlite.org/lang_transaction.html).
La durabilité suppose un stockage correct ; aucun essai de coupure électrique
réelle n'est revendiqué ([atomic commit](https://www.sqlite.org/atomiccommit.html)).

### Frontières durables et résultats

1. Vérifier essai actif, hôte/génération, révision/checkpoint, unicité, aucun pending
   et quota ; consommer quota et opération atomiquement. Incrémenter la révision.
2. Relire le temps de l'autorité et la révocation avant dispatch. L'horloge est
   injectée dans le banc ; avec `clock`, elle est relue après les pauses, pas
   prise dans un champ de requête fourni par un PC distant. Le mode sans clock
   utilise le temps logique passé par le test : aucune horloge réseau réelle
   ou durée de service n'est qualifiée.
3. Commit `dispatching` AVANT l'effet fixe. Après cette frontière un crash doit
   rester incertain, même si l'effet n'a peut-être pas eu lieu.
4. Seul le modèle d'autorité appelle ScriptedTransport. Aucun ticket utilisable
   pour envoyer n'est remis au PC. Persister ensuite confirmed ou uncertain.
5. Consultation séparée inspect, sans appel du transport. Avant une permission
   suivante, acknowledge compare la révision et un nouveau checkpoint après
   commit local ; uncertain n'est pas acquittable par cette voie. Les données
   de checkpoint restent déclaratives dans ce POC, sans preuve de commit distante.

| Classe | Sens exact |
| --- | --- |
| permission_refused | Échec explicite avant nouvelle consommation ; aucun nouvel effet accordé |
| consumed_without_operation | Consommation durable, frontière d'effet non franchie à l'observation |
| confirmed | Effet synthétique cohérent et résultat de l'autorité commit ; pas encore une confirmation locale B2 |
| uncertain | Frontière franchie ou acceptation possible sans résultat durable fiable ; aucune réémission |

Le résultat inclut aussi l'état brut et `terminal`. Un consumed encore pending
et actif n'est **pas** la preuve que l'appel initial, simplement suspendu,
n'exécutera jamais. Un acquittement d'abandon concurrent vide pending dans une
transaction ; le dispatch qui reprend doit alors refuser l'effet. Une révocation
avant frontière interdit aussi cet effet. `dispatching` est incertain mais non
terminal tant qu'une exécution en cours peut finir ; confirmé/incertain final
est durable. Le résultat de fixture contient un code et une empreinte, pas un
message_id Telegram authentifié.

Autorité injoignable/stockage défaillant n'est jamais un succès. L'absence de
réponse après une tentative ne doit pas être traduite en « permission refusée » :
consulter l'état si possible, sinon garder le résultat inconnu et ne pas renvoyer.
Les erreurs de stockage exposent unavailable_unknown ; la réouverture ne crée
jamais une nouvelle base pour remplacer un fichier absent ou corrompu.

## Tests de résilience : lecture obligatoire des preuves négatives

| Scénario | Résultat/limite attendus |
| --- | --- |
| Deux contrôleurs, même opération, barrière simultanée | Une consommation/confirmation, autre refus ; une seule ligne et un seul quota |
| Autre machine logique, même génération | Refus host_id ; deux clones ayant le même host_id ne deviennent pas deux identités physiques détectables |
| Ancienne génération / vieux checkpoint PC | Refus ; quotas externes non remis à zéro par restauration du PC |
| ID ou intention métier rejouée | Refus, même avec un op_id neuf pour le même message |
| Consommation commit puis réponse d'autorisation perdue | Quota consommé, aucun effet encore ; inspection/abandon, jamais nouvelle exécution du même ID |
| Crash avant consommation commit | Pas de consommation, pas d'effet dans ce scénario contrôlé |
| Crash après consommation commit | Opération consumed, jamais reprise automatiquement |
| Crash après frontière ou après effet, avant commit résultat | uncertain ; aucun appel supplémentaire du transport |
| Révocation avant frontière | Consommation sans effet, révocation persistante |
| Révocation après acceptation possible | uncertain, jamais « refus donc aucun envoi » |
| Ancien worker vivant après échéance | Refus à son prochain appel ; pause avant frontière puis expiration relue empêche l'effet du modèle |
| B1, ancien détenteur qui contourne l'autorité | Le modèle direct peut encore accepter l'effet : contre-exemple au fencing par permission |
| Autorité elle-même restaurée | **Deux effets synthétiques acceptés pour la même opération** après retour à l'image ancienne : anti-rollback non démontré |

Le dernier test réussit comme **détecteur d'une limite**, pas comme preuve de
sécurité. Il affiche explicitement la limite. Le registre externe de ce POC
ne possède pas de racine non restaurable. Une restauration peut également
réhabiliter un essai révoqué, effacer une révocation et restaurer des quotas
anciens ; ni EXTRAsync ni une chaîne
d'empreintes locale supplémentaire ne corrigerait ce problème fondamental.

Les processus concurrents et les crashes os._exit sont réellement exécutés sur
l'hôte de qualification, avec vrais commits SQLite et contrôles après reprise.
Leurs « machines », effets et credentials/capacités sont simulés. Pas de nouvelle
preuve Windows native de Job Object, DPAPI, ACL ou reboot physique dans C5 ;
les tests C1/C2/C3 hérités restent distincts. Aucun service réel n'a été arrêté.

## Insertion future dans les composants existants

| Composant | Ordre / adaptation à revoir séparément |
| --- | --- |
| private_recovery | begin puis touch_store avant ouverture du magasin ; retired après crash dirty reste bloquant, même avec autorité accessible |
| private_fenced_job | Confinement du client local utile ; ne prouve pas le confinement du futur exécuteur externe, qui aura ses propres budgets et arrêts à qualifier |
| private_trial | Conserver la garde locale, mais les quotas externes sont la limite autoritative, consommés sans remboursement ; restaurer C3 ne les remet pas à zéro |
| private_credential | En B2 le token privé ne doit plus être transféré au PC. Le canal C3 pourrait porter une identité cliente distincte seulement après nouvelle conception/revue ; rien n'est remplacé ici |
| private_integration / B1 | Après lecture permise/exécutée extérieurement, authentifier/classer puis commit reçus et curseur B1 avant checkpoint externe et avant acquittement distant de l'offset |
| B2 outbox | Préparer/figer, commit intention in_flight AVANT demande à l'autorité ; consommer/exécuter ensuite. Valider le résultat puis commit attempts et outbox ensemble |
| C0 | Sauvegarde cohérente et restauration désarmée ; aucune permission externe ne lève RESTORE_BARRIER |

L'ordre B2 intention → consommation externe privilégie une intention incertaine
ou une consommation sans effet lors d'une panne, au lieu d'un effet sans trace.
Après résultat confirmé par l'autorité mais réponse perdue au PC, le test
d'intégration conserve l'outbox locale uncertain et une seule intention.
Le simulateur local B2 n'est pas appelé pour inventer un succès. Consultation
externe n'est ni réexécution ni commit automatique local. Le pont réel reste à
concevoir séparément : DeliveryStore.deliver n'accepte toujours que la classe
exacte ScriptedTransport, sans élargissement C5.

Une ancienne génération est contrôlée côté autorité avant consommation et
dispatch, pas seulement côté PC restaurable. L'arrêt d'urgence doit révoquer
l'admission externe, arrêter/récolter l'exécuteur et décharger sa capacité ;
ce protocole réel n'est pas implémenté. Une requête déjà passée la frontière
peut encore être acceptée : conserver uncertain et interdire toute nouvelle
génération tant que l'ancien détenteur n'est pas effectivement fenced.

## Coût d'exploitation et proportionnalité

Le POC ajoute un module sans dépendance, des tests et ce rapport. Il ne donne
aucune estimation de prix d'hébergeur non consulté. Pour une B2 réelle minimale,
prévoir au moins **un runtime de confiance indépendant du PC, une base durable
et un point d'entrée authentifié**, plus stockage du secret et capacité de sortie
exclusive. Pas de cluster ni haute disponibilité nécessaires : indisponibilité
= arrêt. Le gain de simplicité vient de fusionner autorité et exécuteur,
pas de supprimer authentification, récupération ou maintenance.

Charge réelle : provisionnement sous nouvelle autorisation, durcissement et
mises à jour, certificats/identités clientes, rotation du token privé, contrôle
d'accès aux sauvegardes, surveillance/quota disque, journaux expurgés et exercice
d'incident. Chaque restauration exige un runbook empêchant toute ancienne copie
de fonctionner. Ces tâches peuvent dépasser le bénéfice d'un bot personnel.
Le choix d'un hébergement, d'un administrateur et d'une racine anti-rollback
reste ouvert ; coût et effort ne peuvent pas être chiffrés honnêtement avant ce
choix. Aucun compte ni serveur n'est créé pour lever cette incertitude.

## Garanties manquantes et prochaine intervention recommandée

La prochaine intervention devrait être **un arbitrage ciblé**, sous nouvelle
décision : choisir une stratégie de cycle de vie de l'autorité qui résiste à
sa propre restauration et à ses clones, avec contrôle vérifiable du secret et
de l'egress. Définir qui peut restaurer, comment une ancienne instance perd sa
capacité, et quel état indépendant établit la révocation. Un simple booléen
« ne pas restaurer » dans la base restaurable n'est pas acceptable.

Si aucune stratégie proportionnée n'est retenue, arrêter l'évolution vers le
réel ou demander explicitement un nouvel arbitrage du modèle de menace ; ne pas
ajouter automatiquement un consensus, une seconde autorité ou une nouvelle phase.
Même une solution de cycle de vie future laisserait à qualifier authentification
client/host, quotas/temps de service, secret/ACL, DNS/TLS/WAN, confirmation B2,
reprise, arrêt opérateur et toutes les conditions de C4.

**C5 À REVOIR ; NO-GO réel maintenu.** Aucun résultat de test ou fusion de cette
PR ne lève les interdictions d'activation.

## Validation et périmètre

Résultats de la suite Python 3.12 complète, tests C5, Ruff, compileall,
pip-audit production/développement et CI du SHA exact joints à la PR de livraison.
Validation locale : Windows 10 build 19045, Python 3.12.10, SQLite 3.49.1 ;
**411 tests en 90.630 s, OK avec 4 exclus** (trois qualifications C0 natives
non réautorisées et un cas POSIX). Les **25 tests C5** passent : 24 tests du
registre/effets et un test d'intégration B1/B2/C2/C3. Ruff et compileall passent ;
pip-audit ne trouve aucune vulnérabilité connue en production/développement.
La concurrence et les crashes C5 sont exécutés sur de vrais processus Windows,
sans prétendre qualifier une nouvelle API Windows, une ACL ou un fencing réseau.
Les limites affichées par les tests négatifs restent bloquantes malgré le statut OK.

```text
python -m unittest discover -s tests -q
python -m ruff check .
python -m compileall -q poster.py cadence_simulator.py newsbot tools tests
python -m pip_audit -r requirements.txt -r requirements-dev.txt --progress-spinner off
```

La suite existante de publication est conservée. Pas de changement de
production, state.json, watcher, workflows, FIFO, sources, cadence ou journal
confirmé 1947. Aucun Telegram, token/ID privé réel, ACL, Credential Manager,
nouveau dossier sous C:\, cloud, serveur, Pi, service, tâche, boot automatique,
déploiement ou fusion. Seuls des répertoires de fixtures temporaires sont utilisés.
Une seule PR C5 ; arrêt du travail après publication et rapport.
