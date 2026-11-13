# Expérimentation : connexions RSS réutilisées

Branche de préparation : `codex/rss-session-experiment`. Aucun déploiement ni
essai Telegram. Référence examinée :
`ca80c30750a1e3f82d6225c37e6cc23128ea04ec` (code de collecte identique à #23).

## Périmètre exact

- `newsbot/collection.py` : une `requests.Session()` pour la boucle séquentielle
  d'un passage, fermée par son gestionnaire de contexte à la sortie de la boucle,
  y compris sur interruption d'admission ou de checkpoint. Les traitements
  finaux sans HTTP suivent ce bloc. `http=None` sélectionne ce transport ; un
  `http` injecté reste utilisé directement, sans création ni fermeture de session
  appartenant à l'appelant.
- Chaque réponse obtenue est consommée entièrement (`stream=False` par défaut),
  puis fermée dans un `finally` avant parsing. Les doubles historiques sans
  méthode `close` restent acceptés. Une réponse HTTP en erreur est aussi fermée.
- Les cookies de la session possédée sont effacés entre les flux, y compris
  après erreur : les anciens appels `requests.get()` ne les partageaient pas.
  Les cookies internes aux redirections d'une même requête restent possibles.
- Les timeouts `(5, 20)`, l'User-Agent, les adaptateurs Requests par défaut
  (zéro réessai), les exceptions et leur traitement restent conservés.
- `RSS phases` ajoute des durées réseau, parsing et admission aux journaux,
  uniquement avec le libellé hôte/empreinte existant. L'horloge de mesure est
  distincte de l'horloge de budget injectée. Aucune nouvelle métrique persistée.
- `tests/test_collection_session.py` protège ressources, erreurs, cookies,
  timeouts, ordre, déduplication, rotation, checkpoints et séparation Telegram.
- `tools/benchmark_rss_session.py` fournit une comparaison locale explicite ;
  il n'est appelé ni par le bot ni par les workflows.

L'ordre des opérations d'admission, de curseur et de sauvegarde est conservé.
Les 63 sources, les budgets, les capacités, la fraîcheur, la FIFO, la cadence
Telegram configurée à 15 s et les workflows restent inchangés. Aucun cache,
parallélisme de collecte, dépendance ou réessai supplémentaire. Aucun changement
à `state.json`, aux secrets ou aux branches Windows/Dependabot.

## Mesures isolées du 13 novembre 2026

Windows, Python 3.12.10, Requests 2.34.2, feedparser 6.0.11. Serveur HTTP/1.1 en
boucle locale uniquement ; 53 flux synthétiques alternant RSS et Atom, ordre
rotatif, doublons entre flux, cookies et sauvegardes dans un répertoire temporaire.
Les deux collecteurs utilisent les mêmes contenus et horloges fonctionnelles
figées. Trois répétitions, ordre ancien/nouveau alterné ; aucune source réelle
ni API Telegram contactée. Le serveur de test accepte des connexions dans ses
threads, mais le client effectue toujours ses requêtes une par une.

| Profil local | Médiane ancien | Médiane session | Réduction mesurée |
| --- | ---: | ---: | ---: |
| Délai artificiel de 25 ms à chaque nouvelle connexion | 2,080 s | 0,641 s | 69,18 % |
| Sans délai artificiel | 0,702 s | 0,544 s | 22,52 % |

Dans chaque comparaison : 53 requêtes ; **53 connexions → 1** ; 107 articles
admis ; 11 checkpoints. État final, ordre des identifiants, métriques
fonctionnelles, trace des requêtes et octets de chaque sauvegarde identiques.
Aucun cookie reçu d'un flux transmis au suivant.

Sur les trois passages avec délai, les nouvelles mesures cumulées indiquent
0,122–0,142 s de réseau, 0,120–0,125 s de parsing et 0,012–0,014 s d'admission.
Le temps total comprend aussi la préparation, les journaux et les sauvegardes.
« Réseau » agrège requête, éventuelles redirections, corps et fermeture : il ne
distingue pas DNS, TCP, TLS ou temps serveur. En cas d'échec avant parsing,
parsing/admission sont indisponibles, jamais présentés comme travail effectué.

Reproduction depuis la racine, dépendances habituelles installées :

```sh
python tools/benchmark_rss_session.py --reference ca80c30750a1e3f82d6225c37e6cc23128ea04ec
python tools/benchmark_rss_session.py --reference ca80c30750a1e3f82d6225c37e6cc23128ea04ec --connection-delay 0
```

Le commit de référence doit être disponible localement ; seul du code Git de
confiance doit être choisi. Les temps absolus varient selon la machine et le
système de fichiers. Les gains ci-dessus ne sont **pas** une prévision YouTube.
Ce serveur n'émule ni TLS, ni le CDN, ni les limites et fermetures distantes.

## Vérifications et limites

- Suite complète : **125 tests réussis**, dont 9 nouveaux tests ciblés.
- Ruff et `compileall` : réussis ; `pip-audit` production/développement : aucune
  vulnérabilité connue signalée avec les versions épinglées.
- Comparaison existante avec `1dd233d764b0` : **19 scénarios identiques**, notamment
  saturation, budget, dates/quarantaine, interruptions, ambiguïté Telegram et 429.
  Ces scénarios injectent leur transport ; le benchmark exerce le nouveau
  transport par défaut avec de vraies connexions locales.

Le gain réel dépend de la part TCP/TLS évitable, de la réutilisation autorisée
par le serveur et des délais de réponse. Un serveur peut fermer une connexion
persistante ; le traitement actuel des erreurs RSS reste en vigueur. Une collecte
plus rapide peut parcourir davantage de flux avant le même budget, sans changer
leur ordre ni les règles d'admission. Elle ne garantit ni la résorption de la
file ni la disparition des erreurs Telegram ; elle ne réduit pas les pauses
de planification GitHub.

Après autorisation explicite seulement : intégration isolée, puis observation
de plusieurs passages **planifiés**, sans nouvel envoi manuel ni surveillance
supplémentaire. Comparer durées réseau/parsing/admission, flux effectivement
consultés, admissions, erreurs RSS, FIFO et sauvegardes ; vérifier séparément
les diagnostics Telegram. Si nécessaire, revenir sur le seul commit de code
de cette expérimentation, en conservant le dernier état de production. Aucune
migration d'état n'est requise. Les autres optimisations et les PR Dependabot
restent différées.

La [documentation Requests](https://requests.readthedocs.io/en/latest/user/advanced/)
décrit la réutilisation des connexions, la consommation des réponses, les cookies
et la fermeture des sessions par contexte.
