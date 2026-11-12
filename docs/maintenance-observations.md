# Observations de référence

Relevé du 12 novembre 2026 ; toutes les heures ci-dessous sont en UTC.
Lecture seule de Git, des PR et des journaux Actions ; aucun déclenchement de
workflow, envoi Telegram, arrêt de production ou changement d'état pour ce relevé.

## Versions et travaux existants

- Référence : [`1dd233d764b0f1cf5997944fd3d8f4d97fb1be6d`](https://github.com/Pince2Homard/news-telegram/commit/1dd233d764b0f1cf5997944fd3d8f4d97fb1be6d),
  commit d'état du 12 novembre à 22:12:03. Le dernier changement fonctionnel du bot
  est [`d9c28d1`](https://github.com/Pince2Homard/news-telegram/commit/d9c28d1),
  fusion de la [PR de fraîcheur et déduplication](https://github.com/Pince2Homard/news-telegram/pull/9)
  du 11 novembre à 15:18. Cette phase est donc déjà en production.
- Le checkout initial était propre sur `codex/windows-github-watcher`, commit
  `bfcc04c`. La [PR du watcher Windows](https://github.com/Pince2Homard/news-telegram/pull/10)
  reste ouverte et indépendante ; ses travaux sont à préserver.
- Les références locales initiales de `main` étaient anciennes. Les mesures
  ci-dessous utilisent la référence distante actualisée, pas cet ancien checkout.

## Sources et portée des mesures

L'historique de `state.json` a fourni 140 versions dans la fenêtre demandée
du 5 au 12 novembre. Les résumés persistés sont limités à douze par état : ils
ont été réunis depuis ces versions et dédoublonnés par `run_started_epoch`.
Cela donne **33 exécutions mesurées**, du 11 novembre 11:26:25 au 12 novembre
22:04:03, dont 29 avec la politique de fraîcheur active, à partir du 11 novembre
16:17:15. Il ne s'agit donc pas d'une semaine de métriques représentative.

L'API Actions a aussi fourni 151 exécutions du workflow de production entre
le 7 novembre 06:30:22 et le 12 novembre 22:03:53 : 147 planifiées et quatre
manuelles. Cet historique couvre plusieurs versions aux règles différentes.
Une absence d'incident dans ces observations ne démontre pas son impossibilité.

## Volumes et état

| Indicateur | Observation |
| --- | --- |
| Flux configurés à la référence | 63 |
| Entrées examinées, 33 résumés | 109 631 |
| Nouveaux candidats découverts | 830 |
| Admissibles et ajoutés à la file | 782 |
| Publications confirmées | 420 |
| Rejets de fraîcheur | 48 |
| Sous-fenêtre avec fraîcheur active | 740 admissibles ; 378 publications ; 48 rejets |
| Reports de collecte `rss_deferred` | 0 |
| Ajouts et promotions du tampon d'admission | 0 |
| Mises en quarantaine de date, réexamens, admissions et expirations | 0 |
| Compteur nommé `articles_skipped_by_seen_id` | 95 027 |
| Sauts par curseur de flux | 13 774 |
| Plus grande file finale dans les 33 résumés | 274 |
| Plus grande ancienneté de tête après publication, `queued_at`/`admitted_at` | 33 503,309 s, environ 9 h 18 |

Les 140 instantanés d'état, incluant les versions antérieures aux métriques,
montrent une file principale d'au plus 533 articles et aucune file à 1 000.
Ce maximum d'instantanés ne mesure pas les pics intermédiaires pendant les
exécutions. Les compteurs d'admission et de quarantaine n'existaient pas encore
sur toute cette période. Dans les états qui les contiennent, tampon et
quarantaine de date sont vides ; aucune saturation de ces structures n'est
observée. Cela ne justifie pas de supprimer leur politique de saturation.

À `1dd233d`, la file contient **234 articles**, le tampon et la quarantaine de
date sont vides, l'historique contient 5 000 identifiants et 4 092 empreintes URL.
La liste `erreurs` contient **128 rejets Telegram HTTP 400**. Cette liste est
distincte de `date_quarantine` ; ses entrées ne doivent pas être supprimées pour
nettoyer l'état.

Le compteur nommé `articles_skipped_by_seen_id` agrège dans le code de référence
les doublons par identifiant **et par empreinte URL**. Les mesures existantes
ne permettent pas de répartir précisément les 95 027 entre ces deux causes.
Les rejets de fraîcheur ne représentent pas nécessairement 48 articles uniques
sur toutes les sources et toutes les exécutions.

## Cadence, budgets et incidents

Les écarts entre les 147 déclenchements planifiés disponibles ont une médiane de
3 345 s (55 min 45), un minimum de 2 125 s et un maximum de 6 149 s
(1 h 42 min 29). Ce sont les dates de création des runs, pas une garantie
de démarrage effectif ni de publication. La fenêtre traverse plusieurs
configurations ; elle ne permet pas d'attribuer toute cette irrégularité au
cron actuel.

La [dernière exécution observée](https://github.com/Pince2Homard/news-telegram/actions/runs/43316339278)
a terminé avec succès à 22:12:07, sauvegarde distante comprise. Son journal
indique une cadence effectivement configurée à **30 secondes**, 16 publications,
aucune erreur Telegram et 234 articles conservés faute de budget avant la
prochaine cadence. Le dernier succès Telegram enregistré dans l'état est à
22:12:04. L'heure enregistrée peut être arrondie prudemment au-delà de la date
Telegram. La recommandation simulée de 15 secondes n'a pas changé la cadence
effective.

Les 151 runs se répartissent en 146 succès, quatre échecs et un run encore
affiché `queued` lors du relevé. Les quatre échecs sont anciens :

- [7 novembre 12:16](https://github.com/Pince2Homard/news-telegram/actions/runs/42371769066)
  et [8 novembre 19:28](https://github.com/Pince2Homard/news-telegram/actions/runs/42515195719) :
  publication terminée puis `git push` de l'état rejeté (`fetch first`).
- [7 novembre 16:09](https://github.com/Pince2Homard/news-telegram/actions/runs/42389819803) :
  connexion interrompue (`ConnectionResetError`) pendant le programme.
- [8 novembre 09:17](https://github.com/Pince2Homard/news-telegram/actions/runs/42466556343) :
  délai de lecture dépassé vers Telegram.

Ils précèdent plusieurs durcissements actuels. Ces changements et les succès
suivants ne prouvent pas que le risque de perte de sauvegarde ou de résultat
réseau ambigu est supprimé. Le [run affiché en attente](https://github.com/Pince2Homard/news-telegram/actions/runs/43096221567)
date du 11 novembre 20:28 ; des exécutions ultérieures ont réussi. Il n'a pas été
annulé pour ce diagnostic. Les résumés persistés ne donnent pas les motifs précis
de tous les reports de publication ni les occurrences de 429 : la dernière
exécution a été examinée en détail, pas tous les journaux de la fenêtre.

## Compatibilité GDATA

Les 63 flux actifs de `feeds.txt` n'utilisent pas GDATA. Les références actives
dans le dépôt se limitent au résolveur, à ses deux tests et à sa mention de
compatibilité dans le README. Les flux YouTube configurés utilisent les adresses
RSS par identifiant de chaîne. L'état conserve néanmoins une ancienne entrée
GDATA dans `feeds` et une entrée du cache `yt` : ces traces historiques doivent
rester lisibles et être conservées même si le résolveur est retiré.

## Décisions possibles et décisions différées

Les extractions de responsabilités, le retrait du résolveur GDATA inutilisé,
l'exécution explicite du simulateur et la couverture CI peuvent avancer sans
changer la politique d'admission. La fenêtre n'est pas suffisante pour supprimer
une protection de dates, réduire une capacité ou conclure que le tampon sera
inutile dans tous les cas. Les propositions fonctionnelles sont séparées dans
[functional-proposals.md](functional-proposals.md).

La production est restée sur GitHub Actions et la destination Telegram existante.
Aucun essai réel n'a été provoqué pour ce diagnostic ; la dernière observation
confirme une diffusion et une sauvegarde habituelles réussies. Ce constat est
borné à l'heure et à la version ci-dessus.
