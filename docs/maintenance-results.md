# Bilan du chantier

Référence : `1dd233d` ; résultats préparés sur les branches de revue, sans fusion
ni déploiement sur main. La PR Windows existante reste indépendante et intacte.

## Résultat comparé

| Critère | Référence | Résultat proposé |
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

Revoir les PR empilées dans cet ordre, puis re-cibler les suivantes après décision
sur leur base : [séparation](https://github.com/Pince2Homard/news-telegram/pull/11),
[parcours de production](https://github.com/Pince2Homard/news-telegram/pull/12),
[documentation et CI](https://github.com/Pince2Homard/news-telegram/pull/13).
Chaque description précise périmètre, vérifications, risques et retour arrière.
Aucune fusion n'a été effectuée. Les commits de retrait GDATA et du simulateur
sont distincts ; documentation et filtres CI sont aussi séparés.

Files, capacités, déduplication, dates et quarantaine sont conservées. Les
[propositions fonctionnelles](functional-proposals.md) isolent les décisions
nécessitant encore mesures, validation et éventuelle migration sur copie. Les
33 résumés historiques couvrent moins de deux jours : leur silence sur une
protection ne démontre pas son inutilité. Le Raspberry Pi reste un chantier futur.

**Aucun essai Telegram réel ni interruption volontaire n'a été provoqué.**
Les tests n'ont donc produit aucune publication et n'ont nécessité aucune reprise
de service. La production reste sur GitHub Actions/main ; la dernière exécution
observée a terminé avec succès, sauvegarde distante comprise, le 12 novembre
à 22:12:07 UTC. Son état contient 234 articles en attente, sans tampon ni
quarantaine de dates. Ce constat est borné à cette observation.

La livraison peut toujours être répétée après un succès Telegram suivi d'une
sauvegarde locale ou distante échouée. Le retour arrière des lots actuels conserve
le dernier état sain ; il n'exige aucune migration de format. Voir les
[notes de reprise](state-migrations.md) et la [procédure d'essai](operations.md).
