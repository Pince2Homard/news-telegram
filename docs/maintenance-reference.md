# Référence du chantier de maintenance

Relevé le 12 novembre 2026, avant toute extraction.

- Référence de production : `1dd233d764b0f1cf5997944fd3d8f4d97fb1be6d`.
- Le checkout initial propre était `bfcc04c`, branche
  `codex/windows-github-watcher` (PR #10 ouverte). Il est préservé sur sa branche.
- La PR #9 (fraîcheur et déduplication hybride) est déjà fusionnée ; les anciens
  pointeurs locaux vers main ne constituaient donc pas une référence valide.
- Aucune instruction AGENTS.md trouvée dans le dépôt ni ses répertoires parents.
- Production prévue : Python 3.12, dépendances épinglées dans requirements.txt.
  Python local utilisé : 3.12.10 ; `pip check` réussit. Les alias Windows du PATH
  pour python/py sont inutilisables ; utiliser le véritable exécutable installé.
- Tests avant modification : **97 réussis**, sans transport Telegram réel.
  Les 83 tests du checkout Windows initial réussissaient également.

## Responsabilités et transitions initiales

`poster.py` concentre téléchargement/parsing, dates, déduplication, admission,
publication, métriques et persistance. `collect_articles` mêle notamment le réseau
et les décisions d'admission ; `process_queue` mêle cadence, erreurs et sauvegardes.
Le simulateur est appelé à chaque passage mais sa recommandation est informative.

État v2 versionné dans Git :

- `file` : FIFO de 1 000 articles publiables ; succès Telegram → retrait et
  sauvegarde ; échec définitif propre à l'article → `erreurs` (500 maximum).
- `admission_buffer` : 1 000 articles déjà admis ; transfert FIFO vers `file`
  après les articles déjà en file. Leur vieillissement ne retire pas l'admission.
- `date_quarantine` : 500 articles aux dates inconnues/futures ; réexamen →
  admission, maintien ou expiration. Le motif `admission_deferred` conserve aussi
  un article revalidé lorsque les deux structures d'admission sont pleines.
- `vus` et `url_seen` : historiques bornés (5 000 chacun), complétés par les
  articles actifs et les curseurs par flux. Un GUID n'est pas interprété comme URL.
- `feeds`, `feed_health`, `feed_offset` : amorçage, erreurs et reprise de collecte.
- `last_successful_send_at/message_id` : reprise de cadence ; `metrics_history` :
  12 résumés ; `yt` : ancien cache GDATA, à préserver même sans résolveur actif.

## Contrat à préserver

Ordre des flux avec rotation, entrées dans l'ordre inverse du flux, amorçage
silencieux, priorités actuelles des doublons, requêtes URL conservées, admissions
durables, reports sans marquage abusif comme vu, bornes et expirations actuelles.
Fenêtre par défaut de 24 h ; tolérance future de 5 min ; quarantaine inconnue
de 24 h ; traitement prudent des dates très futures (au-delà de 7 jours).

Double verrou DRY_RUN/ALLOW_TELEGRAM_SEND ; aucun état enregistré en simulation ;
écriture atomique locale ; refus de réinitialiser un JSON corrompu ; absence de
secrets dans les journaux ; cadence configurée indépendante du simulateur ;
budgets bornés ; reprise après interruption ; arrêt sur erreur globale Telegram ;
réessai local uniquement après réponse explicite 429, jamais après réseau ambigu.

La livraison peut être répétée si Telegram accepte puis que l'état local ou son
commit distant n'est pas sauvegardé. GitHub Actions et la persistance Git restent
en place. Aucun matériel ni stockage futur n'est requis pour les extractions.

Les observations historiques et leurs limites sont consignées séparément dans
`maintenance-observations.md`. Elles ne conditionnent pas le découpage du code.
