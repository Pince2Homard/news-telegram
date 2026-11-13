# Bilan du chantier

Référence : `1dd233d`. Chantier clôturé après intégration et validation en
production. La PR Windows existante reste indépendante et intacte.

## Résultat comparé

| Critère | Référence | Résultat intégré |
| --- | --- | --- |
| Responsabilités concentrées | Réseau, décisions d'articles, Telegram, état et métriques dans poster.py | poster.py orchestre ; collecte, admission, dates, doublons, publication et persistance ont leurs modules |
| Décision d'admission isolée | Exercée via collect_articles et un transport simulé | Entrées parsées, état, index, compteurs et horloges explicites ; tests directs sans transport |
| Règle représentative : fraîcheur | Parse/classification et transitions dans le même grand fichier | Interprétation/classification dans dates.py ; conséquences d'admission dans admission.py |
| Transitions persistantes | Tampon → file ; quarantaine → admission/expiration ; file → succès/erreurs | Inchangées ; aucune transition nouvelle ou suppression dissimulée |
| Compatibilité active | Résolution GDATA et scraping malgré absence dans feeds.txt | Flux directs ; anciens caches/historiques conservés |
| Simulation de cadence | Calcul à chaque passage, sans pouvoir sur la cadence | Outil explicite en lecture seule ; configuration réelle préservée |
| CI | PR main couvertes, pushes limités à quatre anciennes branches | PR main sans filtre ; pushes main/codex/hardening couverts sauf état seul |

Le découpage conserve des fonctions d'admission et de publication substantielles :
leurs règles n'ont pas été effacées. Le gain vérifié est de pouvoir les lire et
les exercer indépendamment du téléchargement et de l'orchestration. Les horloges
et transports déjà injectables à la référence restent contrôlables. La validation
porte sur les décisions et l'état, pas sur un objectif de nombre de lignes.

## Vérifications

- Python 3.12.10, feedparser 6.0.11, requests 2.32.4 ; pip check réussi.
- Référence : 97 tests ; séparation seule : 104 ; résultat complet : **110 réussis**.
- `python tools/compare_reference.py` : **19 scénarios identiques**, code ancien
  chargé depuis Git, flux/états copiés ou synthétiques et horloges communes.
  Comparaison des articles, de leur ordre, des reports, des métriques, de l'état
  final et des points de sauvegarde. Inclut saturation, dates corrigées,
  vieillissement d'articles admis, interruption/reprise, budgets et erreurs Telegram.
- Tests supplémentaires : échecs d'écriture atomique et après confirmation,
  règles d'admission sans réseau, parcours main avec envois fictifs à cadence
  configurée, priorité DRY_RUN, CLI répétable sans réseau ni modification d'état.
- Syntaxe et git diff --check réussis. Aucun changement de state.json, feeds.txt,
  requirements.txt ou des workflows de publication/ancien test manuel.
- [CI de la séparation réussie](https://github.com/Pince2Homard/news-telegram/actions/runs/43319573600).
  [CI du résultat complet réussie](https://github.com/Pince2Homard/news-telegram/actions/runs/43320658836).

## Livraison et décisions restantes

Les trois PR ont été intégrées séquentiellement, sans squash ni conflit :

| PR | Commit de fusion |
| --- | --- |
| [#11](https://github.com/Pince2Homard/news-telegram/pull/11) | `49c84c58af3b3a88ca6c4f801ca69b0bf1f90af1` |
| [#12](https://github.com/Pince2Homard/news-telegram/pull/12) | `71ff7f8f8a949a843ea10d2759008edc3237a703` |
| [#13](https://github.com/Pince2Homard/news-telegram/pull/13) | `da0dddc691d9b8ef1186a5255cd94df6bd135b6c` |

Les CI ont réussi après reciblage des PR suivantes, puis sur le
[main intégré](https://github.com/Pince2Homard/news-telegram/actions/runs/43324633134).
Aucune adaptation du code n'a nécessité de répéter les comparaisons de référence.

Files, capacités, déduplication, dates et quarantaine sont conservées. Les
[propositions fonctionnelles](functional-proposals.md) isolent les décisions
nécessitant encore mesures, validation et éventuelle migration sur copie. Les
33 résumés historiques couvrent moins de deux jours : leur silence sur une
protection ne démontre pas son inutilité. Le Raspberry Pi reste un chantier futur.

La validation réelle a utilisé une unique
[exécution normale de production](https://github.com/Pince2Homard/news-telegram/actions/runs/43324648814),
lancée sur `da0dddc691d9b8ef1186a5255cd94df6bd135b6c`, avec la concurrence
habituelle et les articles déjà en attente du canal d'origine. Les mises à jour
intermédiaires ne concernaient que l'état ; le code intégré est resté identique.

Ce passage s'est terminé le **12 novembre 2026 à 23:08:01 UTC**
(13 novembre à 00:08:01 à Paris) :

- **16 publications confirmées**, sans message artificiel.
- Cadence configurée de **30 s**, intervalles entre confirmations du passage de **31 s**.
- File observée de **218 articles au départ à 202 après publication** : les
  16 premiers ont été retirés et les 202 suivants conservés exactement dans leur ordre.
- Sauvegarde distante réussie : `baddc879e1fca68ec4e1ae327cc45bd2bf545013`.
- Aucun échec RSS, Telegram ou de persistance relevé pendant cette exécution.

Ces volumes décrivent ce passage daté, pas une taille permanente ou actuelle de
la file. La production poursuit son fonctionnement habituel sur GitHub Actions ;
des passages planifiés ultérieurs ont également réussi. Aucun arrêt volontaire,
nouveau stockage, dispositif de surveillance ou tâche planifiée n'a été ajouté.

Restent différées la fusion éventuelle des files, les autres simplifications
fonctionnelles et la migration d'hébergement. Le Raspberry Pi reste indisponible.
La gouvernance communautaire, les étoiles et les cadeaux Premium relèvent d'un
chantier distinct ; aucune réalisation n'est engagée ici.

La livraison peut toujours être répétée après un succès Telegram suivi d'une
sauvegarde locale ou distante échouée. Le retour arrière des lots actuels conserve
le dernier état sain ; il n'exige aucune migration de format. Voir les
[notes de reprise](state-migrations.md) et la [procédure d'essai](operations.md).
