# Reprise des anciens états

Le format courant reste **v2**. La séparation des modules et les simplifications
à faible risque n'introduisent pas de nouveau stockage ni de nouvelle version
d'état. Le fichier reste versionné dans Git et sauvegardé localement de manière
atomique avant sa sauvegarde distante par le workflow.

## Chargement et compatibilité

`load_state` complète les champs absents nécessaires au fonctionnement, notamment
`admission_buffer`, `date_quarantine` et `url_seen` par des listes vides. Il
préserve la file existante et son ordre, les curseurs, l'historique, les champs
connus et les données historiques supplémentaires. Un ancien marqueur de version
est normalisé vers `v=2` sans vider la file. Les bornes normales des historiques
continuent de s'appliquer lors du chargement ou de la sauvegarde.

Les anciennes entrées sans `queued_at`, `first_seen_at` ou `admitted_at` ne
reçoivent pas de dates fabriquées pour les mesures d'attente. L'historique peut
contenir des GUID sans association avec leurs anciennes URL : il ne permet pas
de reconstruire toute la déduplication inter-flux antérieure.

Le retrait du résolveur GDATA n'efface pas `yt` ni les anciennes adresses dans
`feeds`. Ces traces restent compatibles ; elles ne réactivent pas le résolveur.
Les flux actuellement configurés utilisent des URL directes. Pour reprendre une
ancienne configuration GDATA, fournir explicitement le flux direct correspondant.

Un fichier absent permet un véritable premier démarrage. Un JSON illisible ou
une structure invalide provoque une erreur : il n'est pas remplacé par un état
vide. Ne pas supprimer le fichier pour contourner ce refus. Conserver la copie
défectueuse, rechercher une version saine et réconcilier les publications avant
de choisir un état de reprise.

## Sauvegarde et retour arrière

La sauvegarde écrit un fichier temporaire dans le même répertoire, le synchronise
et remplace atomiquement la cible. La synchronisation du répertoire est tentée
lorsque la plateforme le permet. Ce mécanisme évite un JSON partiellement écrit ;
il ne rend pas transactionnels ensemble Telegram, le disque local et GitHub.

Pour revenir sur les extractions ou simplifications sans changement de format,
restaurer les sources par un commit de retour arrière en **conservant le dernier
état sain**. Inclure les modules `newsbot`, les outils et workflows concernés,
puis relancer les tests. Ne pas restaurer tout l'arbre depuis une ancienne
référence en écrasant `state.json` : cela remettrait également en attente des
articles déjà publiés et ferait perdre les admissions récentes.

Si une récupération d'état est réellement nécessaire, arrêter toute publication
concurrente, conserver les copies et commits de départ, examiner les journaux
depuis la sauvegarde choisie et définir la réconciliation avant reprise. Un
message accepté par Telegram avant une sauvegarde échouée peut être répété.
L'identifiant de message enregistré aide au diagnostic, sans permettre au bot de
retrouver automatiquement tout le canal.

Toute future modification de format exige une migration testée sur copie et une
conversion inverse ou procédure de restauration explicitement vérifiée. Les
pistes de file unique ne sont pas une migration déjà disponible.


## Métadonnées de reprise automatique

Le format reste v2 ; v1 reconnu et ancien état actif sans `init` restent admis.
Les champs optionnels `delivery_retry` d'un article et `last_attempt_at` de l'état
ne constituent ni un manifeste ni une preuve de livraison. `delivery_retry`
contient `uncertain_count` (entier 0 à 3), `last_attempt_at` et `not_before`
(dates finies positives cohérentes). Leur absence conserve le comportement
historique : zéro tentative incertaine connue, aucune date fabriquée.

Un état existant doit contenir `file` ; `vus` est requis s'il existe des preuves
d'activité. Les articles de file ont un texte et un lien HTTP(S). Versions
inconnues, nombres non finis et structures inutilisables sont refusés sans
écrasement. En publication réelle, un fichier absent est refusé ; le véritable
amorçage doit être préparé explicitement hors publication. Aucun ancien fichier
n'est restauré automatiquement.

Les essais d'écriture (trois au maximum, pauses 1 puis 5 secondes) répètent le
même candidat validé, jamais l'envoi réseau. La rétention existante de 500 entrées
`erreurs` reste bornée ; les retraits supplémentaires sont journalisés et comptés
par `quarantine_pruned`. Le contenu d'un article mis à l'écart reste consultable
jusqu'à cette rétention, et dans les commits qui l'ont enregistré.
