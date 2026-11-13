# Diagnostic des interruptions Telegram — 13 novembre 2026

Base de préparation : `7aae1f5`, sans modification ni déploiement en production.
Heures ci-dessous en Europe/Paris (UTC+1). Les journaux ne permettent pas de
reconstituer les causes internes des exceptions : les messages bruts sont
volontairement absents pour protéger le token.

## Faits historiques

| Run | Heure de la tentative en échec | Succès avant arrêt | Collecte RSS | Tentative | Budget interne restant au début de la tentative |
| --- | --- | ---: | ---: | ---: | ---: |
| [43398970771](https://github.com/Pince2Homard/news-telegram/actions/runs/43398970771) | 10:10:07 | 0 | 21 s | 5,866 s | ~699 s |
| [43403723363](https://github.com/Pince2Homard/news-telegram/actions/runs/43403723363) | 10:46:35 | 9 | 14 s | 0,385 s | ~560 s |
| [43418989123](https://github.com/Pince2Homard/news-telegram/actions/runs/43418989123) | 12:48:37 | 27 | 23 s | 0,535 s | ~265 s |
| [43427316583](https://github.com/Pince2Homard/news-telegram/actions/runs/43427316583) | 13:46:36 | 17 | 22 s | 0,706 s | ~425 s |
| [43438034975](https://github.com/Pince2Homard/news-telegram/actions/runs/43438034975) | 14:57:29 | 3 | 18 s | 0,389 s | ~653 s |
| [43453822008](https://github.com/Pince2Homard/news-telegram/actions/runs/43453822008) | 16:40:27 | 17 | 128 s | 1,456 s | ~320 s |

Les budgets sont estimés depuis run_started_epoch et l'heure de tentative ; les
horloges de journal peuvent être arrondies. Leur marge suffit à exclure le budget
comme motif de ces arrêts. Les six erreurs sont des ConnectionError sans réponse
HTTP attachée signalée et sans 429. Cinq concernent les sept passages planifiés
depuis 10:43 ; le premier cas est un passage manuel antérieur.

Le nombre de succès, le temps écoulé avant l'erreur (~21 à 455 s) et la collecte
varient. Le ralentissement RSS à 128 s est inhabituel (médiane ~22 s sur neuf
passages examinés), mais les erreurs surviennent aussi après 14 à 23 s de collecte.
Il n'explique donc pas à lui seul la répétition. Les cinq premiers incidents
précèdent Requests 2.34.2 et les fusions #17/#16 ; le dernier leur succède.

Les workflows terminent normalement et sauvegardent leur état. Pas d'indice de
timeout Actions, annulation ou interruption du runner dans ces cas. L'arrêt est
celui prévu par le bot après résultat incertain, pas une fin de budget ou le
plafond de publications. Tous les neuf passages examinés ont sauvegardé à distance.

Sur les sept passages planifiés : 268 articles admis, 133 publications confirmées,
file de 187 à 322 (+135). Les interruptions réduisent les sorties ; les arrivées,
la collecte et l'espacement des passages contribuent aussi à cette croissance.
On ne peut attribuer tout le retard aux seules erreurs réseau.

## Causes et vérifications discriminantes

1. **Incident intermittent du transport : famille établie, cause précise inconnue.**
   Une rupture de connexion/réponse est plausible, mais une résolution DNS rapide
   en échec ou un problème de connexion/proxy reste possible.
2. **Timeout explicite ou erreur TLS directement exposée : moins étayés.** Le type
   affiché n'est ni ReadTimeout, ni ConnectTimeout, ni SSLError et les tentatives
   durent moins de 30 s. Toutefois Requests peut encapsuler une erreur de lecture
   dans ConnectionError ; le seul type externe ne suffit pas à les exclure.
3. **Régression Requests, saturation Telegram ou interruption Actions : non démontrées.**
   Antériorité des incidents, absence de 429 et sauvegardes normales vont contre
   une attribution directe, sans prouver l'absence de tout incident infrastructure.

Requests.post crée une Session par appel ; le pool n'est donc pas conservé entre
les articles. L'hypothèse d'une connexion keep-alive périmée entre publications
est peu soutenue. L'adaptateur Requests utilise par défaut Retry(0, read=False).
Le bot ne retente localement que les réponses 429 explicites. Les variables de
proxy prises en compte par Requests pourraient influer sur le chemin réseau,
mais leur contenu n'est ni connu ni collecté ici.

Références techniques : [adaptateur Requests](https://requests.readthedocs.io/en/latest/_modules/requests/adapters/),
[lecture des réponses](https://requests.readthedocs.io/en/latest/_modules/requests/models/),
[exceptions urllib3](https://urllib3.readthedocs.io/en/stable/reference/urllib3.exceptions.html).

## Instrumentation proposée, non déployée

Classification par types reconnus uniquement, en parcourant une chaîne bornée
de causes/contextes, reason et arguments qui sont eux-mêmes des exceptions.
Aucune conversion en texte des arguments ou causes, aucun traceback, URL,
en-tête, corps, titre ou identifiant d'article ajouté aux journaux.

Catégories : DNS, certificat TLS, TLS générique, échec/timeout de connexion,
rupture de connexion, lecture/décodage interrompus, timeout de lecture ou inconnu.
Une phase précise n'est affichée que lorsque le type la permet ; rupture TCP,
SSLError générique et ReadTimeout restent de phase inconnue. Même ReadTimeoutError
peut être produit autour de la validation de connexion par urllib3 : on ne prétend
donc pas distinguer négociation TLS, attente d'en-têtes et lecture du corps avec
ce seul type. Les erreurs de corps reconnues ont une phase spécifique.

La présence d'une réponse attachée est séparée de la réception HTTP : sans preuve,
celle-ci reste inconnue. Aucun diagnostic ne prouve que Telegram n'a pas accepté
le message. Les journaux conservent aussi la durée de tentative et ajoutent le
budget avant opération, sa réserve et le nombre de succès au point d'arrêt.

Risques limités mais réels : certains wrappers futurs resteront inconnus, une
chaîne trop longue sera tronquée et l'interprétation dépend des types Requests/
urllib3. urllib3 est déjà une dépendance requise par Requests, pas un nouveau
paquet ajouté. Aucun texte d'exception n'est analysé pour deviner une catégorie.
La classification ne pilote aucune décision de publication, retry ou sauvegarde.

Conserver la cadence, l'arrêt sur résultat ambigu, la FIFO et l'état. Ne pas
introduire de retry réseau, de Session persistante ou de réglage DNS/TLS à l'aveugle.
Recommandation : faire revoir cette PR diagnostique ; après autorisation distincte
de déploiement, observer les prochains passages planifiés avant de proposer un
correctif fonctionnel. Aucun workflow de production n'est changé par ce lot.
