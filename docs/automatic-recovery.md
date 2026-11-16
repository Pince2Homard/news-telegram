# Reprise automatique simplifiée — issue #7

Statut : branche de développement ; ni fusion, ni activation.
Décision : [rapport validé](https://github.com/Pince2Homard/news-telegram/issues/7#issuecomment-6906129531).

## Contrat

- HTTP seul ne confirme pas : HTTP 200, `ok: true`, Message avec ID entier
  positif, date entière plausible et chat correspondant. Une destination
  numérique est comparée exactement ; un `@username` exige le même username
  public retourné et un ID numérique. Aucun appel de découverte. Les
  redirections sont désactivées ; aucun payload/token n'est journalisé.
- Réponse incohérente/perdue/timeout : résultat incertain, un seul appel
  potentiellement accepté par passage, arrêt du passage. Délais minimum :
  900 secondes puis 1800 secondes. Ils sont persistés avec l'article et
  survivent au redémarrage si le checkpoint a bien été poussé.
- Après trois incertitudes enregistrées, quarantaine `erreurs`, motif
  `retry_exhausted_possible_delivery`, contenu et compteur conservés.
  Les autres articles reprennent automatiquement au prochain passage normal.
- Rejet 429 valide : attente intégrale si elle tient dans la réserve existante,
  sinon report sans augmenter le compteur d'incertitudes. Si l'enveloppe est
  un rejet valide mais le délai est absent/invalide, report conservateur de
  900 secondes, classification `rejected`, un seul appel et aucun compteur
  d'incertitude consommé. Tout délai entier positif fourni reste intégralement
  respecté. 401/403, destination
  inattendue et défauts globaux préservent toute la file. Seules des erreurs
  d'article reconnues (texte trop long/vide, entités invalides) sont isolées.
- DRY_RUN prioritaire, ALLOW obligatoire, groupe de concurrence, FIFO normale,
  déduplication, checkpoints de collecte et intervalle configuré conservés.
  Le plafond de 50 opérations borne aussi les articles rejetés.

## Incidents Actions / Git

Le helper `tools/persistence_health.py` a trois modes, sans fonction d'envoi :
`gate`, `probe`, `sync`. Le précédent passage pertinent est sélectionné parmi
les passages terminés, par date de mise à jour, avec sa tentative. Rerun,
passage interrompu/échoué et absence de preuve de persistance entraînent un
passage de santé sans publication ; une réussite permet la reprise suivante.
La recherche reste bornée à 100 passages.

Une API Actions indisponible ou inexploitable produit désormais `degraded`.
Dans ce cas, le même passage prépare une sonde puis la synchronise et vérifie
son contenu distant AVANT toute publication. Le step publish ne s'exécute que
si le step checkpoint réussit. Échec de la sonde, du push ou de la vérification :
aucun credential Telegram injecté dans une étape exécutée, aucune publication.
La preuve doit être fraîche : un ancien marqueur ne suffit pas. Cette route
permet de publier à chaque passage malgré une panne API prolongée lorsque Git
fonctionne. La sonde et le checkpoint n'importent aucun sender. Le résultat final
de publication est ensuite synchronisé normalement, même après erreur Python.
Les garde-fous pour rerun ou autre run in_progress restent en mode `recovery`,
pas en mode dégradé. Aucun manifeste ni permission supplémentaire.


La synchronisation ne modifie que state.json, sur main. Trois tentatives et un
budget de 60 secondes, sous-commandes limitées à 15 secondes. Pas de force-push.
Un rebase de l'unique commit d'état n'est tenté que si le blob distant d'état
est identique à la base ; l'empreinte du candidat et le diff sont revérifiés.
Un état concurrent différent n'est jamais fusionné automatiquement : passage
en échec, puis nouveau clone et récupération au cron suivant. Une réponse de
push perdue est vérifiée par fetch du blob canonique. Aucun manifeste ni commit
pré-envoi en régime normal.

## Validation et limites

Tests Python 3.12 sur Windows, transports simulés ; qualification Git native sur
un dépôt bare temporaire local. Les tests couvrent réponse valide/invalide,
timeout, reports, quarantaine, redémarrage, sauvegarde après envoi, conflits et
acquittement perdu, récupération après panne et DRY_RUN. Ruff, compileall et
audit des dépendances sont exécutés avant livraison. Les résultats exacts et CI
figurent dans la PR et le compte rendu de l'issue #7.

Quelques doublons, omissions et perturbations d'ordre sont acceptés. Une perte
du runner avant push peut perdre un compteur ou une confirmation ; trois
incertitudes connues ne signifie pas trois appels physiques garantis. Une
récupération de santé ne reconstruit pas les confirmations perdues. Une panne
intermittente seulement après les sondes peut encore causer des rejeux.
Les appels connus gardent leur cadence ; l'heure réelle d'une acceptation
serveur tardive après timeout n'est pas maîtrisée. Pas d'exactly-once.
La rétention d'erreurs est bornée ; son élagage est compté et journalisé.

Coûts : copies/validation JSON à chaque checkpoint, jusqu'à 6 secondes d'attente
supplémentaire en cas d'écriture transitoire, deux lectures API Actions usuelles,
un checkpoint final normal. Un incident connu peut ajouter un passage sans
émission et un commit de santé. En mode dégradé API, un checkpoint supplémentaire
précède la publication : jusqu'à deux commits dans ce passage. Le budget du
processus est réduit de 720 à 660 secondes pour réserver les 60 secondes de
vérification préalable ; le plafond Actions reste 14 minutes. Les timeouts Requests ne sont pas une échéance réseau
absolue ; le budget du run et la limite Actions de 14 minutes restent en place.
Aucune qualification WAN/Telegram réelle n'est revendiquée.

## Retour arrière

Aucune bascule n'est autorisée par cette PR. Pour un futur rollback : pause
opérateur des nouveaux départs, vérification des invocations déjà actives,
conservation du dernier état canonique et des preuves. Revenir sur les seules
sources/workflow par un commit dédié ; ne pas restaurer un ancien state.json.
L'ancien code peut ignorer `delivery_retry` et réessayer plus tôt : ne pas le
réactiver aveuglément sur une file comportant des reprises différées. Préférer
un maintien désarmé jusqu'à revue de la copie de reprise. Aucun reset automatique.

Avant production : validation humaine du diff, CI verte, droits Actions de
lecture disponibles, chemin normal unique de production et plan de rollback
approuvés. Ni secrets ni état de production n'ont été modifiés pendant le travail.
