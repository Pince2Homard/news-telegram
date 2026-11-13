# Architecture et règles

`poster.py` orchestre la configuration, le chargement, la collecte, la publication
et les métriques finales. Les règles et les transports sont répartis dans
`newsbot`, sans initialisation réseau à l'import.

| Module | Responsabilité et effets |
| --- | --- |
| `settings.py` | Constantes, validation de la cadence et de la fenêtre de fraîcheur |
| `collection.py` | Lecture des flux, téléchargement/parsing, budget de collecte, rotation et points de sauvegarde |
| `dates.py` | Interprétation des publications, fraîcheur, motifs et échéances de quarantaine |
| `deduplication.py` | Identifiants, canonisation/empreintes URL, index des historiques et des articles actifs |
| `admission.py` | Décisions sur des entrées déjà parsées ; amorçage, doublons, dates, capacités et mutations des files/index/curseurs |
| `publication.py` | Transport Telegram, classification des résultats, cadence, budget et progression de la file |
| `persistence.py` | Chargement/validation de l'état et sauvegarde locale atomique |
| `metrics.py` | Mesures d'attente, résumés bornés et journaux sans secrets |

## Parcours d'un article

La collecte commence au `feed_offset` persistant, parcourt les flux configurés
avec rotation, puis traite les entrées dans l'ordre inverse de leur présentation
par le flux. Elle conserve un curseur `latest_uid` par source et son état de santé
dans `feed_health`. Le premier démarrage initialise silencieusement les articles
présents ; une source ajoutée ensuite initialise ses archives et traite sa
dernière entrée normalement.

Les règles d'admission examinent la quarantaine existante, les historiques et
index actifs, les liens publiables, l'amorçage, puis les dates et capacités.
Un doublon peut mettre à jour un historique ou un curseur ; la déduplication
n'est donc pas seulement un filtre sans effet. Un candidat non conservé à cause
d'une saturation ne doit pas être marqué « vu » ni dépassé par le curseur.

| Structure persistante | Capacité | Transitions |
| --- | --- | --- |
| `file` | 1 000 | Article frais → attente ; succès Telegram → retrait ; rejet définitif propre à l'article → `erreurs` |
| `admission_buffer` | 1 000 | Article admis → FIFO ; places disponibles → transfert derrière la file existante |
| `date_quarantine` | 500 | Date douteuse → réexamen ; correction/arrivée de la date → admission ; échéance dépassée → expiration |
| `erreurs` | 500 | Conserve les derniers rejets Telegram propres aux articles |
| `vus`, `url_seen` | 5 000 chacun | Mémoire bornée, complétée par les articles actifs et curseurs |
| `metrics_history` | 12 | Derniers résumés d'exécution sans contenu d'article |

Un tampon non vide garde la priorité sur les nouvelles admissions. Les articles
admis ne repassent pas le filtre de fraîcheur pendant leur attente. Leur
`first_seen_at` et `admitted_at` sont distincts du `queued_at` fixé au transfert
vers la file. Les anciennes entrées sans ces timestamps restent lisibles.

Si une date de quarantaine devient fraîche alors que file et tampon sont pleins,
l'article reste durablement en quarantaine avec `reason=admission_deferred`.
Il a acquis son admission et ne doit plus expirer par vieillissement. La capacité
totale n'est donc pas résumée par « 2 000 articles » : ces admissions partagent
les 500 places de quarantaine avec les dates encore douteuses.

## Dates et déduplication

La publication RSS/Atom est normalisée en UTC. `updated` ne remplace pas une date
de publication absente. La fenêtre par défaut est de 24 h ; une date à plus de
cinq minutes dans le futur ou une date absente, invalide ou de précision jour
entraîne une quarantaine. Les dates inconnues disposent de 24 h depuis la première
observation. Les dates futures plausibles jusqu'à sept jours ont une échéance
liée à leur date annoncée ; les plus lointaines ont une grâce de 24 h.
Les corrections présentes dans le flux sont examinées avant l'admission fondée
sur une date future mémorisée. La collecte seule ne fournit pas une publication
de remplacement.

L'identifiant provient de `id`, ou du lien si `id` manque. L'empreinte URL est un
SHA-256 tronqué à 128 bits : schéma/hôte et port par défaut normalisés, fragment
retiré, chemin et requête conservés. Les GUID historiques ne sont pas interprétés
arbitrairement comme URL. La priorité et les mutations actuelles sont conservées ;
leur simplification fonctionnelle fait l'objet de propositions séparées.

## Publication et points de sauvegarde

La collecte dispose de 180 s, avec points de sauvegarde tous les cinq flux et en
fin de collecte. Le bot dispose de 720 s au total et d'au plus 50 publications
réussies par passage. La publication réserve le budget maximal d'une opération
Telegram avant de commencer ou d'attendre la prochaine cadence. Une attente
inline supérieure à 120 s est reportée.

`process_queue` sauvegarde avant le premier envoi, après les mutations durables
de la file et en fin de traitement. Le succès met à jour la cadence persistée
et l'identifiant Telegram disponible. Un rejet propre à l'article l'isole et
la file continue ; une erreur globale d'accès arrête le passage avec file
préservée. Une réponse réseau ambiguë conserve la tête de file sans renvoi
immédiat. Les délais/réessais Telegram restent bornés, notamment pour les 429.
Les limites de livraison et la sauvegarde distante sont décrites dans
[operations.md](operations.md).

## Tester sans réseau ni état global

`admit_feed_entries`, `prepare_pending_articles` et `release_due_future_articles`
reçoivent l'état, les index, les compteurs, la date du passage et une horloge
d'admission. Ils ne téléchargent ni ne sauvegardent. Ils modifient explicitement
ces objets et renvoient les résultats utiles à l'orchestration.

`collect_articles` accepte `http`, `parser`, `clock`, `queue_clock` et `checkpoint`.
`process_queue` accepte `sender`, `sleep`, `clock`, `monotonic` et `save`.
Le callback `save(state, seen, state_path)` permet de tester les échecs de
persistance sans écrire le fichier de production. Ces injections permettent de
vérifier files pleines, horloges limites, cadence/budgets, interruptions et
résultats Telegram avec les mêmes entrées qu'à la référence.

## Métriques et simulateur explicite

Le bot conserve les métriques de collecte/publication et la ligne JSON historique
`PHASE51_METRICS`. Le compteur nommé `articles_skipped_by_seen_id` agrège aussi
les doublons URL. L'âge d'attente utilise `queued_at`/`admitted_at`, jamais la date
RSS ; aucune date n'est inventée pour compléter l'historique.

`cadence_simulator.py` est exécuté explicitement et ne fait plus partie du
parcours du bot. Il compare 30/25/20/15/10 s, réserve les budgets d'envoi et tient
compte des durées d'opération et écarts de démarrage observés. Son hystérésis se
recalcule à partir de l'historique lu ; il ne persiste pas une recommandation à
chaque exécution de production. Ses projections, y compris les délais de
résorption, restent conditionnelles. La commande ne modifie ni l'état ni
`TELEGRAM_INTERVAL_SECONDS`.
