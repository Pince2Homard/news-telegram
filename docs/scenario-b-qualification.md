# Qualification du scénario B — augmentation bornée des lots

## Verdict et périmètre

**GO technique pour proposer un essai contrôlé de 24–48 h, après validation
humaine et CI verte du SHA livré. Aucune autorisation de production implicite.**

Préparation depuis `9dfa13686dc0ae2f8beeb282e48b024824ac03e6` sur une branche
dédiée. La production reste inchangée tant que cette préparation n'est pas
fusionnée par le propriétaire. Aucun test Telegram ni déclenchement manuel.

Les modifications préparées sont :

- `newsbot/settings.py` : budget 900 s ; seuil distant 25 confirmations et
  provision de synchronisation 60 s ; plafond 50, collecte 180 s et réserve
  réseau 210 s inchangés ;
- `.github/workflows/poster.yml` : timeout 18 min uniquement ; cron cinq minutes,
  groupe d'exclusivité, queue max, cancel-in-progress false et protections inchangés ;
- `poster.py` : raccordement synchrone au synchroniseur existant ; refus en mode
  réel d'un STATE_FILE différent du state.json du répertoire courant, car le
  synchroniseur Git ne prend en charge que ce fichier. DRY_RUN reste utilisable
  avec un état synthétique arbitraire ;
- `newsbot/publication.py` : callback de checkpoint après 25 confirmations,
  provision séparée pour ce checkpoint, contrôle monotone après attente et
  métriques ; aucun changement du transport ni des règles métier ;
- tests de budget, orchestration et qualification ; présent rapport.

`tools/persistence_health.py` est **réutilisé sans modification**. Aucun nouveau
format d'état, système de permissions, service ou mécanisme de reprise concurrent.

## Protocole et invariants

1. Conserver la sauvegarde locale avant toute tentative et après chaque succès.
2. Compter les confirmations depuis le dernier checkpoint distant vérifié.
3. Avant l'opération pouvant atteindre 25, exiger **210 + 60 s**, plus l'attente
   de cadence éventuelle. Pour les autres opérations, conserver les 210 s.
4. Après la 25e confirmation et sa sauvegarde locale réussie, appeler le
   synchroniseur, sans commencer la 26e tentative pendant cette opération.
5. Remettre le compteur à zéro seulement après retour normal du synchroniseur.
   Toute exception arrête le contrôleur ; une interruption brutale se propage.
   L'échec est journalisé avec son type, sans stderr Git brut ni credential.
6. Inclure le temps du checkpoint dans le budget de publication. Vérifier de
   nouveau le budget monotone après un sommeil avant d'engager un POST.
7. Conserver la sauvegarde locale et la synchronisation finale du workflow.
   Si un checkpoint intermédiaire échoue, la synchronisation finale peut encore
   réparer la persistance, mais ne relance jamais la publication dans ce passage.

En fonctionnement du contrôleur de production, **au plus 25 confirmations
depuis le dernier checkpoint distant**, y compris pendant sa validation ;
pas une promesse de 25 acceptations Telegram maximum non connues. Les résultats
ambigus ne permettent précisément pas de compter toutes les acceptations.

Le callback est injectable pour les tests et absent par défaut dans les appels
historiques directs à process_queue. `poster.main` le fournit systématiquement
sur son chemin réel. Aucune découverte de transport ou second orchestrateur.

## Réutilisation de Git et reprise

Le synchroniseur valide l'état, fige le blob candidat, interdit les autres
changements suivis, puis vérifie le blob distant. Il conserve son budget de
60 s et ses commandes bornées à 15 s au maximum.

- Conflit sur le blob distant de state.json : refus, aucune fusion d'états.
- Autre commit distant sans changement du blob de base : rebase contrôlé
  existant, puis vérification du candidat. Ne pas effectuer de modification
  manuelle concurrente de state.json pendant l'essai.
- Push accepté, accusé perdu : un fetch vérifie le candidat avant de considérer
  le checkpoint comme réussi. Pas de nouvelle tentative Telegram.
- Vérification impossible : arrêt fermé. Le candidat reste local, éventuellement
  déjà présent à distance ; ne pas déduire un échec de livraison des articles.
- Après checkpoint vérifié puis perte du runner : le préfixe synchronisé ne
  figure plus dans la file distante. Les derniers articles non synchronisés
  peuvent toujours réapparaître après redémarrage.

Le synchroniseur peut être appelé plusieurs fois dans le même checkout : le
checkpoint suivant prend comme base le HEAD résultant du précédent. Le test
Git natif couvre checkpoint intermédiaire, reprise et checkpoint final, avec
perte simulée d'accusé de push pour chacun.

## Mesures et estimations de capacité

Référence : [run 43852035955](https://github.com/Pince2Homard/news-telegram/actions/runs/43852035955),
31 confirmations, collecte 16,209 s, temps de requêtes cumulé 19,494 s et
186 articles avant publication. La boucle réelle est exécutée hors ligne
avec horloge simulée, latence moyenne constante 19,494/31 s, dates Telegram
entières et sauvegardes locales instantanées. Les espacements obtenus sont
16 s. Ce ne sont **pas des performances mesurées sur Telegram/GitHub**.

| Configuration simulée | Budget | Collecte | Budget publication après collecte | Coût distant injecté | Confirmations | Durée publication | Marge estimée avant timeout du job* | Maximum de confirmations non synchronisées |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Référence A | 720 s | 16,209 s | 703,791 s | aucun intermédiaire | 31 | 480,420 s | ~323 s | 31 |
| B, checkpoint instantané | 900 s | 16,209 s | 883,791 s | 0 s | 43 | 672,420 s | ~371 s | 25 |
| B, coût indicatif courant | 900 s | 16,209 s | 883,791 s | 2 s | 43 | 672,420 s | ~371 s | 25 |
| B, coût élevé | 900 s | 16,209 s | 883,791 s | 60 s | 40 | 668,420 s | ~375 s | 25 |
| A, collecte lente | 720 s | 47,246 s | 672,754 s | aucun intermédiaire | 29 | 448,383 s | ~324 s | 29 |
| B, collecte lente | 900 s | 47,246 s | 852,754 s | 2 / 60 s | 41 / 38 | 640,383 / 636,383 s | ~372 / 376 s | 25 |
| A, collecte au plafond | 720 s | 180 s | 540 s | aucun intermédiaire | 21 | 320,629 s | ~319 s | 21 |
| B, collecte au plafond | 900 s | 180 s | 720 s | 2 / 60 s | 32 / 30 | 496,629 / 508,629 s | ~383 / 371 s | 25 |

* Marges modélisées : timeout 840/1080 s moins collecte, publication et une
enveloppe indicative de 20 s pour préparation/synchronisation finale/nettoyage,
inspirée des derniers logs. Elles ne sont pas des bornes garanties. La réserve
210 s reste exigée à l'entrée de chaque opération ; les 60 s de checkpoint
s'ajoutent à l'opération au seuil, sans être retranchées à cette réserve.

Un checkpoint de 2 s peut être absorbé par l'attente de cadence qui aurait
suivi le 25e succès ; son coût mesuré dans les métriques reste bien 2 s. Un
checkpoint de 60 s consomme davantage de budget et réduit le nombre d'envois.
Le mode degraded conserve sa réduction de 60 s : budget applicatif 840 s.

Sur ce modèle, B conserve **+29 % à +39 %** sur le passage de référence (40–43
contre 31). Un plafond constant de 50 est suffisant ; il est testé avec une
file de 51 et un budget synthétique très large.

Mesure native Windows sur dépôt Git local synthétique : deux checkpoints
vérifiés, avec push accepté puis accusé perdu simulé, ont pris **1,357 s et
1,484 s** lors d'une exécution. Cela démontre le fonctionnement Git local,
pas un délai garanti de GitHub ; les hypothèses 2 s et 60 s restent des
injections de simulation. Les sauvegardes lentes et dépassements d'attente
sont testés séparément.

## Preuves et limites

La suite spécifique couvre : seuil 25, sauvegarde avant push, plafond 50,
provision 270 s au seuil, dépassement d'attente et de checkpoint, arrêt sur
erreur, perte de runner avant/après checkpoint, rejet/uncertain à la frontière,
timeout HTTP et 429 courts/absents, cadence inter-exécutions après rechargement,
collecte 180 s, branchement réel du contrôleur, DRY_RUN et refus d'un autre
STATE_FILE. La suite existante couvre aussi doublons, validation de réponses,
états invalides, sauvegardes atomiques et verrous d'autorisation.

Un dépôt Git bare et un checkout **locaux temporaires** valident le checkpoint
et le redémarrage. Les sockets et DNS Python sont interdits dans la nouvelle
classe de tests. Aucun dépôt de production n'est poussé par un test ; les
seuls pushes de tests ciblent les remotes locaux synthétiques.

Le groupe du workflow et cancel-in-progress false restent inchangés. La
prévention des chevauchements est vérifiée comme contrat de configuration et
par les routes de récupération existantes, **pas par un nouveau lancement de
production**. Les files d'attente GitHub ne sont pas reproduites nativement.

Limites résiduelles :

- perte de runner avant vérification distante : jusqu'à 25 confirmations de
  ce groupe peuvent manquer dans Git et être réémises après reprise ;
- résultat Telegram incertain : politique différée existante inchangée ;
- confirmation puis échec de sauvegarde locale : arrêt, mais doublon possible
  après reprise depuis l'ancien état ; aucun exactly-once ;
- timeout Requests, DNS, lecture progressive et disque ne sont pas des
  échéances globales strictes ; 210 s reste une provision prudente ;
- contrôles Git ne remplacent pas l'exclusivité entre écrivains ; modification
  externe de state.json ou restauration manuelle restent dangereuses ;
- coûts GitHub et latences extrêmes non qualifiés par les fixtures locales ;
- davantage de durée peut accumuler des runs en attente si le schedule devient
  régulier. Aucun changement de queue ou de cron n'est proposé ici.

## Validation et décision opérateur

Les résultats locaux et les CI sur le SHA exact sont publiés dans la PR.
Validation locale Windows / Python 3.12.10 : **311 tests réussis**, dont 18
nouveaux tests ciblés ; Ruff, compileall, pip check et git diff --check réussis ;
pip-audit production et développement : aucune vulnérabilité connue.
Commandes : unittest complet, Ruff, compileall, pip check, pip-audit production
et développement. Aucun package ni dépendance de production ajouté.

Critères pour autoriser ensuite un essai : CI verte, revue du checkpoint et
des limites ci-dessus, accord humain explicite pour fusion/déploiement.
Pendant 24–48 h, contrôler confirmations par passage, admissions/publications,
solde et âge de file, remote_checkpoints, remote_checkpoint_seconds,
max_confirmed_since_remote_checkpoint, motifs d'arrêt, succès du sync final,
erreurs Telegram et attente des runs. Le compteur depuis checkpoint dans les
métriques finales est pris **avant** la synchronisation finale du workflow.

Succès attendu pour les passages chargés sans incident : capacité supérieure
au régime antérieur, aucun groupe supérieur à 25 confirmations, aucun intervalle
de publication inférieur à 15 s, aucun chevauchement, aucune augmentation des
échecs de persistance. Le débit net doit rester positif sur des fenêtres
comparables ; une heure creuse seule ne démontre pas une résorption durable.

## Retour arrière sans perte d'état

Avant fusion : fermer la PR, production inchangée. Après déploiement autorisé :
laisser finir le passage en cours, puis proposer un revert des modifications
de code/timeout sur le main courant. **Ne jamais restaurer state.json depuis
le commit de base ou un ancien backup**, ni réémettre un article pour annuler
la livraison. Vérifier le checkpoint final et repartir du dernier état distant
valide. En cas de checkpoint non vérifié, diagnostiquer d'abord le candidat et
le blob distant, sans déclenchement manuel de publication.
