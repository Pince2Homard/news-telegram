# Phase 8.2.2 — consultation privée du registre, hors ligne

Le contrat `route_message(message, config, snapshot, now)` de la Phase 8.2.1 est
conservé. Le routeur existant ajoute trois vues pures ; aucun transport ou
collecteur n'est ajouté. L'authentification est exécutée avant toute consultation
de l'instantané. Les contrôles privés, refus silencieux uniformes et modes de
simulation de la Phase 8.2.1 sont inchangés.

## Commandes

Les commandes `/start`, `/help`, `/status` sont conservées. L'aide indique les
six commandes. Les nouveaux filtres optionnels sont exclusivement consultatifs :

| Commande | Résultat et filtre |
| --- | --- |
| `/pr [numéro]` | PR connues : numéro, titre disponible, état observé, date, lien canonique |
| `/ci [numéro ou SHA]` | Résumé CI connu par SHA ; le numéro sélectionne le SHA connu de la PR |
| `/rapports [UUID]` | Rapports structurés Codex connus, éventuellement sélectionnés par UUID |

Numéros canoniques de 1 à 999999, SHA de 40 caractères hexadécimaux minuscules et
UUID canoniques seulement. Un seul espace précède l'unique filtre. Aucun autre
argument, suffixe `@bot`, normalisation, commande de modification ou d'exécution
n'est accepté. Une référence inconnue produit un message fixe sans inventer
d'élément. `/start`, `/help` et `/status` refusent toujours tout argument.

## Structures réutilisées

Les données proviennent exclusivement de l'état en mémoire du watcher :

- `pr_snapshots[numéro]` : `head_sha`, `closed` ; `threads["pr:numéro"].updated_at`
  fournit la date d'actualisation. Les titres et le détail de fusion ne sont pas
  persistés par le watcher actuel : le routeur affiche explicitement leur absence.
  Il accepte aussi les champs facultatifs `title`, `state`, `merged`, `updated_at`,
  `html_url` s'ils figurent dans l'instantané, sans changer le producteur.
- `ci_snapshots[SHA]` : `result` et `checked_at`, déjà calculés par le watcher via
  `coordination.ci_summary`. Les cinq valeurs existantes sont distinguées :
  success, failure, pending, incomplete, unknown. L'association aux PR dépend
  uniquement de leur `head_sha` connu. Aucun contrôle manquant n'est créé.
- `coordination_registry[UUID]` : `event.coordination`, `event.prefix`,
  `event.authorized_author`, `event.protocol_error`, `event.created_at`,
  `event.url`, et les indicateurs `conflict`, `source_missing`, `source_changed`.
  Seul un événement CODEX-REPORT d'auteur autorisé, sans erreur de protocole et
  avec enveloppe de type report revalidée par `coordination.parse_envelope`,
  peut être présenté comme rapport. La clé du registre doit correspondre à
  l'UUID de l'enveloppe. Les autres types structurés ne sont pas des rapports.

Les validateurs UUID, SHA, enveloppe et URL de `coordination` sont réutilisés.
Les corps de commentaires, prompts, empreintes, chemins, auteurs, reçus bruts
et secrets ne sont jamais affichés. L'état du rapport désigne son enveloppe et
ne constitue pas une nouvelle vérification de publication distante. Les
contradictions de source sont signalées. Aucun arbitrage n'est effectué.

## Fraîcheur, intégrité et limites

Chaque vue indique le dernier relevé `last_poll_at`, son âge et le seuil existant
de 3600 secondes incluses, sans toucher aux cadences. Au-delà, l'état actuel est
explicitement indisponible et les informations affichées restent historiques.
Les dates propres aux PR, CI et rapports sont également présentées avec leur
âge. Une CI ancienne ne devient pas récente grâce à un nouveau relevé global.
Pour un rapport, la date est sa création, pas une confirmation de publication.
Une date d'élément absente reste inconnue. Une date future ou postérieure au
relevé global invalide l'élément. Aucun succès CI n'implique une aptitude à
fusionner ; aucun état observé n'est présenté comme une vérification actuelle.

La validation porte sur les champs consultés, pas sur tout le schéma du watcher.
Une représentation CI concurrente (`check_runs`, `statuses`, `conclusion`, `state`)
est refusée plutôt qu'interprétée en parallèle du résumé existant. Les états
PR facultatifs ne doivent pas contredire `closed`. Un élément malformé reçoit
un diagnostic fixe, sans réémission de ses données brutes.

Les vues acceptent au maximum 1000 PR historiques, 1001 threads, 100 résumés CI
et 1000 entrées de registre. Les limites PR/threads permettent la conservation
des PR fermées au-delà des 100 PR ouvertes du watcher. Un dépassement rend la
vue indisponible plutôt que d'effectuer une lecture non bornée.

Au maximum trois éléments, dans l'ordre stable des clés, sont affichés, sous la
limite commune de 2048 caractères HTML échappés. Chaque élément est ajouté
entièrement après vérification du budget et une note signale les omissions.
Les titres, lorsqu'ils existent, sont limités à 80 caractères avant échappement
et suffixés d'une ellipse. Les titres vides, trop grands ou contenant des
caractères de contrôle/non imprimables ne sont pas affichés. Aucune entité HTML
n'est coupée. Les filtres permettent de consulter un élément omis.

Les liens sont des URL HTTPS de l'hôte exact github.com et du dépôt fixe
Pince2Homard/news-telegram, d'au plus 240 caractères. Une liste fermée autorise
uniquement la PR demandée, les checks du commit concerné, un run Actions ou une
ancre canonique de commentaire/revue. Le validateur du watcher est complété
par ces routes strictes : pas de requête, port, userinfo, encodage, traversée,
redirection ou dépôt alternatif. Une URL fournie invalide n'est pas réémise.
En l'absence d'URL PR/CI, le lien canonique est dérivé du numéro/SHA connu.
La validation est syntaxique et locale ; aucune existence distante n'est
vérifiée et aucune redirection n'est suivie.

## Validation et protections

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m compileall -q poster.py cadence_simulator.py newsbot tools tests
.\.venv\Scripts\python.exe -m pip_audit -r requirements.txt -r requirements-dev.txt --progress-spinner off
```

Les tests existants restent présents ; les attentes de l'aide et des imports
purs ont été étendues. `test_private_registry.py` ajoute les vues, filtres,
références inconnues, intégrité, fraîcheur, URL hostiles et bornage HTML, ainsi
qu'une simulation du producteur watcher. Les identités privées sont synthétiques.
Des gardes interdisent au routeur fichiers, réseau, environnement, horloge et
processus et vérifient la non-mutation des entrées. Un instantané sentinelle
vérifie qu'une identité refusée ne provoque aucune consultation des données.

Aucun token ni identifiant privé réel lu ; aucun appel Telegram ou GitHub depuis
le routeur. Aucun fichier de production, journal du message 1947, state.json,
workflow, source, FIFO, cadence, installation Windows ou manifeste n'est modifié.
Aucun getUpdates, webhook, polling, service, tâche ou assistant n'est activé.
Retour arrière : retirer les changements de code/tests/documentation de la PR,
sans restaurer de données de production.

Limites : authenticité d'un JSON simulé non garantie, fiabilité dépendante du
producteur et de sa datation, titres absents dans les instantanés actuels,
vues partielles et URL non vérifiées à distance. Cette phase n'autorise ni fusion
ni interaction Telegram réelle. Pour 8.2.3, sous autorisation distincte : définir
un transport privé authentifié, déduplication, limitations de débit et livraison
maîtrisée, sans modifier ces vues pures. Les Phases 8.3 à 8.5 restent hors périmètre.
