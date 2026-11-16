# Qualification du budget de publication — issue #7

## Décision

**Réduction de réserve non qualifiée : comportement de production conservé.**
Cette livraison applique la clause de repli de la mission. Elle ne constitue pas
une optimisation activable : ni les 210 secondes, ni les réessais actuels sur
429 courts ne sont modifiés. Aucun autre transport n'est introduit.

Base examinée : `869f6d925e975e21ff15923b1a0d5ff3e6738e2a`.
Référence : [rapport ciblé](https://github.com/Pince2Homard/news-telegram/issues/7#issuecomment-6914951679).

## Pourquoi une tentative unique ne suffit pas

Dans `newsbot/settings.py`, la réserve est calculée comme
`3 × 30 + 2 × 60 = 210 s`. Les réessais internes de
`newsbot.publication.send_message` sont réservés aux rejets applicatifs 429
valides ayant un délai court. Une tentative unique supprimerait ces deux
attentes, mais ne bornerait pas le seul POST restant.

Requests 2.34.2 est appelé avec `timeout=30`, sans échéance monotone interruptible
englobant DNS, connexion, TLS, écriture, lecture et décodage. Sa documentation
précise que le timeout de lecture est un délai d'inactivité, pas une limite
totale ; la résolution DNS ne respecte pas le timeout de socket :
[Requests — timeouts](https://requests.readthedocs.io/en/latest/user/advanced/#timeouts).
Les adresses multiples peuvent également multiplier les temps de connexion.
La sauvegarde atomique et ses réessais n'ont pas non plus de borne physique
prouvée pour une opération disque bloquée.

Ainsi **30, 60 ou 90 secondes ne sont pas des réserves globales démontrées**.
Les 210 secondes actuelles ne sont elles-mêmes qu'une provision prudente,
pas une garantie absolue de terminaison. Un thread abandonné pourrait encore
publier : constater une échéance après son retour ne résout pas le problème.
Le timeout Actions de 14 minutes peut tuer un processus après acceptation
Telegram et avant confirmation ou synchronisation distante de l'état.

Ajouter ici un nouveau worker, un protocole d'arrêt et une nouvelle barrière
de persistance dépasserait une réduction ciblée justifiée par la seule
suppression des retries. Aucun ancien composant Windows supprimé n'est réintroduit.

## Expérience hors ligne à tentative unique

`tests/test_publication_budget_qualification.py` limite à **1** la constante
importée par `send_message`, exclusivement dans un contexte de mock réversible.
La politique candidate passe les vérifications suivantes :

- un seul appel HTTP ; aucun sommeil sur 429 ; `retry_after` positif respecté,
  délai absent/invalide remplacé par les 900 secondes existantes ;
- JSON incomplet/tronqué, rejet incohérent et exception réseau restent uncertain,
  sans deuxième tentative dans le passage ;
- délai et compteur de reprise persistés dans une fixture, rechargés puis
  refus d'envoi avant `not_before`, avec ordre FIFO conservé ;
- épuisement du budget et sauvegarde lente initiale empêchant l'envoi ;
- arrêt au premier échec de sauvegarde après confirmation, avec preuve que
  l'ancienne version durable contient encore l'article (risque de doublon explicite).

Deux contre-exemples expliquent le refus d'activation : le vrai helper de
connexion urllib3 laisse sa résolution DNS simulée consommer 360 secondes
malgré `timeout=30` ; un POST simulant des lectures espacées de 29 secondes
peut rendre un succès après 348 secondes. Le second est un modèle du contrat
documenté, **pas une mesure réseau native**. Les deux évitent toute socket :
socket et DNS sont interdits par défaut dans cette classe de tests ; seul le
résolveur synthétique du contre-exemple est injecté temporairement.

La suite existante couvre aussi les sauvegardes atomiques échouées, les
protections DRY_RUN/ALLOW_TELEGRAM_SEND, la déduplication, les confirmations
Telegram invalides, les reprises et la cadence. Le protocole existant autorise
une reprise différée d'un uncertain : **aucune promesse d'absence de doublon
après redémarrage ni d'exactly-once**. Cette livraison ne change pas cette politique.

## Rejeu chiffré du passage 43852035955

[Run observé](https://github.com/Pince2Homard/news-telegram/actions/runs/43852035955) :
186 articles avant publication, collecte 16,209 s, budget applicatif 720 s,
31 confirmations, 19,494 s de requêtes cumulées, 155 articles restants.

Le test rejoue la vraie boucle `process_queue` avec 186 articles synthétiques,
budget publication `720 - 16,209`, latence constante `19,494 / 31`, cadence
15 secondes et date Telegram entière. Il retrouve des espacements de 16 s.
Sauvegardes instantanées et latence moyenne : il s'agit d'un modèle reproductible,
pas d'une prédiction des queues de latence ou du prochain passage.

| Politique simulée | Réserve | Confirmés | Restants | Gain |
|---|---:|---:|---:|---:|
| Boucle actuelle | 210 s | 31 | 155 | référence |
| Une tentative, réserve conservée | 210 s | 31 | 155 | 0 |
| Sensibilité uniquement, non qualifiée | 90 s | 39 | 147 | +8 / +25,8 % |

Le gain **livré en production est nul**. Le scénario 90 s n'est ni une nouvelle
configuration ni une recommandation de déploiement. Aucun temps de résolution
DNS ou de sauvegarde maximal ne peut être déduit des moyennes de ce run.

## Conditions avant une réduction effective

Une proposition ultérieure devrait démontrer une borne totale interruptible du
transport, l'absence de publication tardive après arrêt, la classification
uncertain après tout envoi possible et un temps suffisant de checkpoint et de
synchronisation distante. Elle devra conserver FIFO, déduplication, rejet 429
durable, cadence minimale 15 s, double verrou de publication et arrêt sur
échec de persistance. Déterminer la réserve à partir de ces bornes, pas d'un
percentile de latence. Ces travaux ne sont pas implémentés ici.

## Validation et retour arrière

Validation locale Windows, Python 3.12.10 : **293 tests réussis**, dont les
11 nouveaux tests de qualification ; Ruff, compileall et pip check réussis ;
audit combiné production/développement : **aucune vulnérabilité connue**.
Le premier audit dans le bac à sable a échoué lors de la préparation de son
environnement temporaire ; le même audit avec accès réseau autorisé a réussi.
Les résultats CI sur le SHA livré sont consignés dans la PR.
Commandes de validation :

```text
python -m unittest discover -s tests -v
python -m ruff check .
python -m compileall -q poster.py cadence_simulator.py newsbot tools tests
python -m pip check
python -m pip_audit -r requirements.txt -r requirements-dev.txt --progress-spinner off
```

Seuls le présent rapport et les tests sont ajoutés. Le format et le contenu de
`state.json`, le cron 15 minutes, les sources, les secrets et tous les modules
de production sont inchangés. Les trois fichiers locaux non suivis présents
avant la mission sont préservés et exclus du commit.

Avant fusion, fermer la PR suffit au retour arrière. Après une fusion
expressément autorisée, un revert de cette livraison retire seulement tests
et documentation : aucune restauration de `state.json`, réémission ou
réinitialisation de file n'est nécessaire ni souhaitable. Ne pas rétablir un
ancien snapshot d'état pour revenir en arrière.

**Aptitude à revue : oui, comme qualification de non-réduction. Réduction du
budget et mise en production d'une nouvelle politique : non qualifiées.**
