# news-telegram

Bot de collecte de flux RSS/Atom et de publication Telegram.

## Branches et sécurité

- `main` est la branche de production.
- Les branches `hardening/*` servent aux corrections et validations avant fusion.
- Les tests automatisés n'appellent jamais l'API Telegram.
- Le workflow de qualité vérifie les modifications et les pull requests vers `main`.
- Le workflow de production vérifie la file toutes les 5 minutes sur `main`, avec un décalage de deux minutes après l'heure.

## Configuration GitHub

Secrets requis dans le dépôt :
- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT`

Le workflow de production active explicitement `ALLOW_TELEGRAM_SEND=1` et `DRY_RUN=0`.
Le programme reste en simulation par défaut si cette autorisation explicite manque.

La cadence entre publications confirmées se configure avec la variable de dépôt
GitHub Actions `TELEGRAM_INTERVAL_SECONDS` (entier positif, défaut `30` secondes). Cette
variable n'est pas un secret et peut être ajustée depuis les paramètres du dépôt
sans modifier le code. Le programme persiste l'heure Unix UTC du dernier envoi
confirmé (`last_successful_send_at`) et l'identifiant Telegram disponible dans
`state.json`, puis calcule la prochaine heure autorisée à chaque exécution.

## Exécution locale sans publication

```bash
python -m pip install -r requirements.txt
python poster.py
```

Pour une simulation explicite :

```bash
DRY_RUN=1 python poster.py
```

Ne jamais activer l'autorisation d'envoi en local sans avoir vérifié le canal cible.

## Tests

```bash
python -m unittest discover -s tests -v
```

Les tests utilisent des transports simulés et ne publient aucun message Telegram.

## Collecte et file d'attente

- Les flux RSS/Atom sont téléchargés avec un délai de connexion de 5 s et de lecture de 20 s.
- La collecte dispose d'un budget de 3 minutes et reprend au prochain flux après chaque point de sauvegarde. L'exécution complète du bot est plafonnée à 12 minutes, sous la limite de 14 minutes du workflow.
- Le curseur global et le dernier article traité par flux sont écrits dans `state.json` par lots de cinq flux et à la fin de la collecte ; le curseur par flux protège aussi des doublons lorsque l'historique global de 5 000 IDs est tronqué. Le workflow de production ne crée qu'un commit d'état en fin d'exécution, comme auparavant.
- Les publications utilisent une seule cadence, indépendante de la taille de la file. Si un nouvel envoi n'est pas autorisé avant la fin du budget restant, l'article est conservé et l'exécution se termine sans attente inutile. Si la file contient d'autres articles et que l'attente tient dans le budget, le workflow peut attendre jusqu'à 120 secondes ; les attentes plus longues sont reportées au passage suivant.
- Les journaux de tentative Telegram indiquent l'heure UTC, le résultat HTTP, la durée et l'identifiant de message retourné par Telegram lorsqu'il est disponible. Ils n'impriment ni le token, ni le chat, ni l'URL de requête.
- La file d'attente est plafonnée à 1 000 articles. Quand elle est pleine, les nouveaux articles sont différés et ne sont pas marqués comme vus : ils seront retentés au passage suivant.
- La fenêtre de fraîcheur RSS est configurable avec `ARTICLE_MAX_AGE_HOURS` (entier positif, défaut 24). Elle utilise la date de publication RSS/Atom normalisée en UTC; le champ de mise à jour ne remplace pas une publication absente. Les dates à plus de 5 minutes dans le futur et les dates absentes, invalides ou de précision jour sont mises en quarantaine bornée à 500 éléments. Les dates inconnues sont réexaminées pendant 24 h depuis leur première découverte; les dates futures jusqu'à 7 jours sont conservées jusqu'à leur admission possible, tandis que les corrections de dates plus lointaines ont une grâce de 24 h. Ces articles ne sont jamais publiés en s'appuyant seulement sur leur heure de collecte.
- Le code lit `ARTICLE_MAX_AGE_HOURS` dans l'environnement du processus. Le workflow de production actuel ne transmet pas encore de variable de dépôt pour ce réglage : il conserve donc le défaut de 24 h. Rendre la valeur modifiable depuis les paramètres GitHub demanderait un changement séparé du workflow, hors du périmètre de cette PR.
- Un article frais dont la file principale est pleine peut entrer dans `admission_buffer`, FIFO persistant de 1 000 éléments sans expiration. À chaque collecte, les plus anciens éléments admis sont transférés après les éléments déjà en file, avant toute nouvelle admission. `queued_at` est fixé au transfert; `first_seen_at` et `admitted_at` restent distincts. Une fois admis, l'article ne repasse pas le filtre 24 h et n'est jamais purgé de la file.
- Si la file et le tampon d'admission sont pleins, ou si la quarantaine est saturée, l'article n'est pas marqué comme vu et le curseur ne dépasse pas le candidat non conservé. Il sera relu tant que la source RSS le conserve; sa perte reste possible si la source le retire avant qu'une place soit disponible.
- Au premier démarrage, les articles déjà présents dans les flux sont initialisés silencieusement. Lorsqu'un flux est ajouté à un bot déjà initialisé, ses articles historiques sont ignorés et son article le plus récent est traité normalement ; s'il ne peut pas entrer dans une file pleine, il reste retentable.
- Les erreurs Telegram définitives sont conservées dans la liste `erreurs`, limitée à 500 entrées.
- Les flux YouTube par identifiant de chaîne utilisent directement le flux RSS public YouTube. L'ancien format GDATA reste pris en charge par compatibilité.
- Les journaux de collecte donnent une durée et un résumé par flux. Les erreurs HTTP précisent le statut, une catégorie générale et le délai `Retry-After` numérique s'il existe ; les erreurs récurrentes sont suivies dans l'état. Les identifiants de journal n'exposent pas les paramètres d'URL.
- Chaque exécution émet une ligne JSON compacte `PHASE51_METRICS` avec les entrées examinées, découvertes, admissibles, ajoutées à la file et admises dans le tampon, les rejets de fraîcheur, les dates inconnues/futures, les doublons d'identifiant ou d'URL, la distribution d'âge, les tailles des trois structures, les publications, reports, durées et intervalles. L'index de déduplication URL contient au plus 5 000 empreintes SHA-256 tronquées à 128 bits; `vus` et les curseurs exacts existants sont conservés. Les métriques n'incluent ni URL, ni titre, ni secret. L'âge d'attente utilise `queued_at` ou `admitted_at`, jamais la date RSS.
- `state.json` conserve au plus 12 petits résumés d'exécution, sans titre, URL, identifiant Telegram ni secret. Les nouveaux articles reçoivent un timestamp `queued_at`; aucune donnée de mise en file n'est inventée pour les articles déjà présents.
- Le module `cadence_simulator.py` compare 30, 25, 20, 15 et 10 secondes. Sa capacité réserve le budget d'envoi maximal, tient compte de l'attente initiale propre à chaque intervalle et ajoute la durée d'un appel d'envoi avant chaque délai de cadence, comme `process_queue`. Sans historique, la période de workflow supposée est le cron de 300 s; sinon elle est la médiane des derniers écarts de démarrage, avec leur étendue visible. Si un lot modélisé dépasse cette période, le cycle est allongé. Toute durée de résorption est conditionnelle et explicitement non fiable; elle est absente si le budget ne permet pas d'envoyer ou si les arrivées égalent ou dépassent le débit. GitHub Actions peut retarder ou regrouper ses événements.
- Les états v2 existants reçoivent des listes vides par défaut pour le tampon, la quarantaine et l'index d'URL; la file, son ordre, ses `queued_at`, les curseurs, `vus` et les métriques historiques restent préservés. Aucune donnée n'est supprimée pendant la migration. L'historique antérieur ne contient pas toujours l'URL associée à un ID GUID; la déduplication URL rétroactive de ces articles n'est donc pas garantie.
- Une hystérésis attend deux observations avant de changer la recommandation simulée, puis avance d'un seul palier de 5 secondes par validation. En l'absence d'historique suffisant, la simulation garde 30 secondes.
- La recommandation de cadence reste uniquement informative en phase 5.1. Elle n'est jamais transmise à la file Telegram. La cadence effective demeure `TELEGRAM_INTERVAL_SECONDS` (30 secondes par défaut, ou la valeur explicite configurée). Le simulateur n'appelle ni réseau ni fonction d'envoi.

### Limites de la planification

Le cron est décalé à `2/5 * * * *` pour éviter le début exact de l'heure et laisser la file être contrôlée plus souvent. GitHub Actions impose un minimum de cinq minutes entre les déclenchements planifiés et peut retarder ou abandonner des événements en période de charge. La cadence persistée garantit seulement qu'un succès ne sera pas suivi d'un autre avant l'intervalle configuré ; elle ne garantit pas une publication exactement toutes les deux minutes. Le groupe de concurrence `poster-production` sérialise les exécutions et conserve une file d'attente, limitée à 100 exécutions en attente.

## Garantie de livraison Telegram

La file assure une livraison « au moins une fois ». Si Telegram accepte un message puis que le processus s'interrompt avant la sauvegarde qui le retire de la file, l'article sera renvoyé après reprise et un doublon est possible. Une erreur réseau ambiguë n'est pas retentée dans le même passage, mais l'article reste en file pour la prochaine exécution. L'architecture actuelle ne garantit pas une livraison exactement une fois.

Si l'envoi est confirmé mais que l'écriture locale de `state.json` échoue, le programme s'arrête et journalise un risque de doublon. Le workflow tente ensuite de versionner l'état local ; si la sauvegarde distante échoue aussi, l'état de `main` peut rester en retard. L'identifiant de message Telegram, lorsqu'il est disponible dans la réponse, aide à réconcilier ce cas mais ne permet pas au bot de retrouver automatiquement tous les messages du canal.

## État persistant

Pour le moment, `state.json` reste versionné dans Git afin de préserver la file et l'historique de déduplication. Son déplacement vers un stockage externe nécessite une migration dédiée et un secret supplémentaire ; ne pas supprimer le fichier Git avant validation de cette migration.
