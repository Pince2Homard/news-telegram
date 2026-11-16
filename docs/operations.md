# Exploitation et essais

## Production GitHub Actions

Le workflow `poster.yml` est déclenché par le cron `2/5 * * * *` ou manuellement.
Son job accepte seulement `refs/heads/main` et son checkout utilise explicitement
`main`. Le groupe de concurrence `poster-production`, avec annulation désactivée,
sérialise les exécutions de ce workflow. Cela ne protège pas contre un programme
lancé ailleurs avec une copie indépendante de l'état.

Le workflow installe Python 3.12 et `requirements.txt`, configure explicitement
`DRY_RUN=0`, `ALLOW_TELEGRAM_SEND=1`, les secrets Telegram existants et la cadence,
puis exécute `poster.py`. Le timeout Actions est de 14 minutes, au-delà du budget
interne de 12 minutes. Le cron décrit les déclenchements souhaités ; retards,
regroupements ou événements manquants restent possibles. Une cadence de 30 s
entre succès ne garantit pas une publication toutes les 30 s.

Après le programme, même en cas d'échec, le workflow tente de versionner
`state.json`. Il ne crée un commit qu'en présence de changements et tente jusqu'à
cinq séquences `pull --rebase`/`push`. Examiner la conclusion de l'étape de
sauvegarde, pas seulement les publications du journal. Un échec distant laisse
potentiellement `main` en retard sur l'état local du runner.

Le workflow qualité couvre les PR vers `main` et les changements de code
pertinents sur `main`, y compris modules, tests, dépendances et workflows. Les
pushes sur ces branches portant uniquement sur `state.json` ne relancent pas
toute la qualité. Les PR vers `main` restent testées sans filtre de fichiers.

## Surveillance et livraison

Les journaux donnent la collecte par source, la cadence, les résultats Telegram,
les reports et la ligne JSON `PHASE51_METRICS`. Les résumés persistés sont bornés
à douze exécutions. Les journaux n'impriment pas le token, le chat ni l'URL de
requête Telegram ; les identifiants de source masquent les paramètres d'URL.
Ne pas ajouter de sortie de variables d'environnement ou de corps de réponse
brut pour diagnostiquer un incident.

La reprise conserve les articles dont le résultat réseau est ambigu. Telegram
peut avoir accepté un message avant l'interruption ou avant une sauvegarde locale
ou distante échouée : ce message peut être répété après reprise. L'atomicité
locale ne supprime pas cette limite. Les rejets définitifs propres aux articles
restent dans `erreurs` ; les erreurs globales d'accès interrompent le passage
avec sa file préservée. Consulter aussi les
[notes de reprise d'état](state-migrations.md) avant une restauration.

## Progression des vérifications

1. Exécuter les tests avec transports simulés et horloges contrôlées.
2. Comparer à la référence avec des données copiées/synthétiques lorsque cela
   apporte une vérification de règle, d'ordre ou de reprise.
3. Réserver un essai réel à un scénario ciblé, avec durée et publications bornées.
4. Vérifier l'état sauvegardé et la reprise de la diffusion habituelle.

Les essais sur le **canal Telegram d'origine** sont autorisés dans ce chantier,
y compris des publications de contrôle et interruptions temporaires bornées.
Cette autorisation n'autorise ni nouveau canal, ni changement de destination,
ni vidage de file/historique, ni suppression de messages existants.
**Aucun essai Telegram réel n'a été provoqué pendant ce chantier** ; les
exécutions habituelles observées ne sont pas des publications de test.

## Préparer un essai réel depuis une branche

Le workflow de production actuel ne publie pas le code d'une branche de travail :
un dispatch sur cette branche est rejeté par la condition `main`, et le checkout
est lui-même fixé à `main`. L'ancien workflow manuel `poster-test.yml` vise une
ancienne branche et ne constitue pas une procédure valide pour ce chantier.
Ne pas contourner ces conditions silencieusement.

Avant un futur essai, préparer une procédure ou modification de workflow ciblée,
réversible et liée à un SHA précis. Elle doit fixer :

- le scénario, la version, un maximum de publications et une limite de temps ;
- les verrous explicitement configurés, les secrets et la destination existants ;
- la suspension éventuelle des départs de production et la fin du run déjà actif ;
- une sérialisation commune avec `poster-production`, puis un chargement du
  dernier état sain de `main` ;
- la sauvegarde des mutations du test dans l'état qui sera repris en production,
  avant de réautoriser le processus habituel ;
- le retour au workflow habituel et la vérification d'une exécution ultérieure,
  sans réinitialisation ni seconde publication depuis un état indépendant.

Une copie d'état convient aux comparaisons sans envoi. Un test publiant depuis
cette copie puis abandonnant ses mutations compromettrait la continuité : il
pourrait répéter les mêmes articles au prochain passage de production. En cas
d'échec de sauvegarde ou de résultat ambigu, conserver les preuves et réconcilier
l'état avant de reprendre deux chemins de publication.

Le compte rendu d'une série d'essais doit indiquer la version et les scénarios,
le nombre de publications et la durée d'interruption provoqués, les résultats et
anomalies, le commit d'état final et la reprise effective ou non de la diffusion.
Un déploiement temporaire d'essai est distinct de la fusion définitive des PR
du chantier ; aucune autorisation générale de fusion n'est donnée.

## Hébergement futur

Le Raspberry Pi est indisponible. GitHub Actions, la persistance Git et les
sauvegardes locales atomiques restent en place. Aucun achat, provisionnement,
nouveau stockage ou développement pour ce matériel n'est engagé. Sa disponibilité
ouvrira un chantier distinct, selon les contraintes consignées dans les
[propositions différées](functional-proposals.md#hébergement-et-limites-de-livraison).


## Reprise automatique simplifiée (proposition non déployée)

Voir [le contrat et la validation](automatic-recovery.md). Le workflow proposé
consulte le précédent passage de `poster.yml` et sa tentative. Après échec,
annulation ou résultat inconnu, il effectue un passage de récupération sans
credential Telegram : état canonique valide, marqueur `persistence_check`, puis
commit/push vérifié. Un passage de récupération réussi permet la publication
au prochain passage normal. La lecture nécessite `actions: read` sur le token
éphémère Actions ; aucun PAT supplémentaire.

Un incident réseau devient une reprise différée (15 puis 30 minutes), pas un
manifeste ouvert. Après trois incertitudes enregistrées, l'article est conservé
dans `erreurs` avec `possible_delivery: true`, puis les autres articles peuvent
reprendre. Le passage incertain échoue explicitement ; un passage de santé peut
donc s'intercaler avant la reprise. Une corruption/configuration invalide reste
un refus : ne jamais supprimer l'état ou réinitialiser la file pour la contourner.

Ne pas lancer manuellement `tools/persistence_health.py probe/sync` contre la
production sans autorisation. `DRY_RUN=1` rend ces commandes sans écriture.


### Mode dégradé si seule l'API Actions est indisponible

Une erreur de lecture d'historique retourne `mode=degraded`. Le même passage
prépare une sonde et exige son push ET sa vérification distante avant d'exécuter
publish. Si cette vérification échoue, publish reste skipped. Si elle réussit,
la publication bornée reprend immédiatement, sans attendre le retour de l'API.
La sonde n'utilise pas de credential Telegram ; celui-ci reste limité au step
publish. Budget de publication réduit d'une minute dans ce seul mode afin de
réserver la vérification Git supplémentaire, limite du job inchangée.
Un 429 valide sans retry_after est reporté de 15 minutes comme rejet certain,
sans incertitude ni quarantaine ; le délai entier positif fourni prime toujours.
