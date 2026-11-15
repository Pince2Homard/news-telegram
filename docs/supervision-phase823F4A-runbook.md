# Premier essai privé — proposition issue de F4-A, exécution interdite

**NO-GO aujourd'hui.** Ce runbook n'est pas une autorisation et ne contient pas
de commande de connexion Telegram. Toutes les lignes bloquantes de la
[matrice F4-A](supervision-phase823F4A-qualification.md) doivent être satisfaites
et approuvées avant une autorisation distincte d'essai. Ne pas commencer F4-B
sur la seule publication de ce document.

## Fiche d'autorisation à arrêter avant le lancement

Conserver cette fiche localement, hors des livrables GitHub. Aucun token dans
la fiche, ce chat ou les rapports. Les IDs réels restent locaux.

| Champ | Exigence |
| --- | --- |
| Version | SHA exact revu, dépendances et runtime qualifiés ; modification ultérieure = nouvelle revue |
| Poste / compte | Un seul PC Windows approuvé et compte dédié ; chemins privés vérifiés avec toute leur chaîne |
| Identités | Bot privé distinct de publication, propriétaire et chat privé numériques manuellement épinglés ; aucune auto-détection depuis un message |
| Session | Génération et essai uniques, état durable cohérent et désarmé, aucun ancien worker douteux ou registre retired à contourner |
| Méthodes | getMe, getWebhookInfo, getUpdates et réponses sendMessage seulement, sous autorisation réseau exacte future |
| Durée | Au plus 300 s depuis l'autorisation durable C3 ; délai monotone couvrant aussi préflights et opérations bloquantes |
| Lectures | Au plus 20 tentatives, dont un getMe et un getWebhookInfo ; au plus 18 réceptions ensuite |
| Réponses | Au plus 6 tentatives admises, confirmées ou non ; pas de remboursement, retry, fractionnement ou prolongation |
| Commandes | Exactement /help et /status, sans paramètre, suffixe, casse ou espaces ambigus |
| Arrêt | Opérateur présent, terminal de secours ouvert, ticket/handles et procédure de clôture qualifiés sous ce compte |
| Révocation | Qui décide, qui peut révoquer le credential privé chez son fournisseur et comment l'attester ; aucune action implicite sur le bot de publication |

## Porte zéro : le poste peut-il être admis ?

1. Prouver maintenance Windows/ESU et état de redémarrage ; qualifier un runtime
   Python/TLS maintenu. La version actuelle observée n'a pas ces preuves.
   Toute remédiation système est une action séparément autorisée, pas une étape
   automatique de ce runbook.
2. Vérifier compte, chemins, propriétaires, DACL, parents, annexes, espace/quota,
   sauvegarde cohérente et restauration désarmée. Une ACL refusée ou non lisible
   impose l'arrêt ; ne pas l'assouplir pour démarrer.
3. Qualifier le fournisseur privé ciblé et son transfert, sans découverte ou
   fallback. Il doit refuser absence, mauvaise cible, bot ou génération. La
   présence d'une entrée ne remplace pas l'autorisation d'essai.
4. Prouver le transport distant borné et l'arrêt sur ce poste, hors production.
   Le transport F2 actuel reste uniquement 127.0.0.1 : ne pas remplacer son hôte
   ou désactiver son audit pour tenter l'essai réel.
5. Attester un seul lecteur et un seul hôte ; tous les autres clients arrêtés.
   Un heartbeat absent, un verrou libre ou un job introuvable ne sont pas des
   preuves de terminaison. Aucun hôte de secours.
6. Approuver explicitement les limites F1 : pas d'anti-rollback coordonné ou
   de confinement d'un OS/admin hostile ; cela ne permet jamais de contourner
   une corruption, un état ambigu ou un refus ACL constaté.

Une seule réponse « inconnu » ou « refusé » maintient NO-GO. Aucun token réel,
préflight ou observation Telegram n'est permis pour essayer de franchir cette
porte sans l'autorisation spécifique correspondante.

## Lancement manuel futur, après GO distinct

1. Relever SHA, hôte, compte, génération, essai et checkpoint. Comparer l'identité
   et l'intégrité du stockage, sans reset ni choix arbitraire d'une vieille image.
2. Ouvrir la barrière C2 avant le magasin, begin puis touch_store avant toute
   ouverture B1/B2 susceptible d'écrire. L'état non sealed bloque une nouvelle
   session ; retired et RESTORE_BARRIER ne sont pas des drapeaux à effacer.
3. Ouvrir le terminal de secours et identifier le chemin local du ticket prévu,
   hors secrets. Charger le credential par l'interface approuvée et armer le
   budget C3 seulement après autorisation explicite ; aucun boot, CI, watcher,
   simple import ou présence du credential ne doit démarrer le lecteur.
4. Faire une seule vérification getMe puis une getWebhookInfo, chacune débitée
   avant tentative et incluse dans les 300 s/20 lectures. Bot inattendu, webhook
   présent, conflit 409 ou doute : arrêt. Aucune suppression de webhook ni purge.
5. Effectuer au plus une réception à la fois. Authentifier avant toute vue ;
   classer et committer les reçus avec le curseur avant l'offset suivant. Refuser
   globalement un lot non fiable, sans acquitter d'updates non sauvegardées.
6. Pour chaque commande admise, vérifier fraîcheur 120 s et texte exact. /help
   ne lit aucune vue ; /status ne déclenche aucune collecte. Préparer une réponse
   figée privée, intention durable B2 et quota C3 avant reprise du worker.
   Respecter aussi les limites B2 : au moins 2 s entre intentions et au plus
   10 par minute ; ne pas les contourner pour atteindre les six réponses.
7. Exiger identité/session/destination/contenu/ID/délai cohérents puis commit
   atomique B2. HTTP 200 seul ne vaut pas confirmed. Rejet cohérent terminal ;
   réponse perdue, tardive, contradictoire ou commit douteux : uncertain et arrêt,
   sans renvoi ni essai avec un nouveau secret pour contourner l'ambiguïté.
8. Finir au premier plafond, arrêt opérateur ou doute. 300 s est un maximum,
   pas un objectif à atteindre. Pas de session prolongée pour vider un backlog,
   finir les six réponses ou compenser un appel refusé.

## Arrêt et vérification

Le futur point d'entrée doit avoir un finally garantissant l'appel à shutdown.
Il doit être qualifié avant réel ; F4-A n'en installe aucun. Ordre obligatoire :

1. Fermer immédiatement l'admission et signaler l'événement latché.
2. Terminer le Job Object sans attendre le worker ni commencer par un fsync.
3. Exiger les handles OS connus, l'heure de création/appartenance attendues,
   ActiveProcesses=0 et la racine signalée ; récolter puis fermer les handles.
4. Décharger les buffers de secret maîtrisés, persister les intentions ambiguës,
   annuler les prepared et désarmer. Conserver les causes recovery_required,
   expired/exhausted et toutes les preuves ; ne pas les masquer par un reset.
5. Fermer le magasin puis seal seulement après clôture sûre attestée. Rapport
   expurgé de codes/compteurs/états, sans corps privé, token ou IDs personnels.

Pour les **fixtures seulement**, le secours déjà qualifié s'appelle depuis un
autre terminal du compte approuvé :

```text
python -m tools.windows_watcher.private_stop --fixture-stop "<fixture>/control/stop-ticket.json"
```

Il ferme l'admission, ouvre et termine le job connu, contrôle un handle racine
réel et le job vide. worker_stopped=true avec controller_closure=false prouve
le worker capturé, pas la clôture SQLite du contrôleur. La transposition de cette
commande vers une entrée réelle reste à qualifier et autoriser ; ne pas lui
passer un chemin de production sous prétexte de réutiliser une fixture.

Un retour STOP_UNPROVEN ou code non nul, ticket manquant/périmé, PID/heure
discordants, job absent ou handle perdu impose le maintien du blocage. Ne pas
annoncer « arrêté » sur la seule demande de terminaison. Le contrôleur conserve
quarantaine/verrous si sa propre preuve manque. Pas de taskkill par PID seul,
second lecteur, nouveau trial ou effacement de registre comme secours.

## Incidents et clôture humaine

| Incident | Conduite obligatoire |
| --- | --- |
| Perte de réponse après acceptation possible | Une seule intention uncertain, conservation durable, aucune réémission |
| Reboot ou mort du contrôleur | Essai annulé, C3 désarmé au diagnostic, C2 non repris sans preuve ; dirty peut rester retired même après arrêt OS prouvé |
| Corruption, quota ou disque plein | Arrêt réseau d'abord ; conserver la base et les journaux, ne jamais recréer une base vide |
| Restauration ou checkpoint perdu | Restauration désarmée ; ne pas réactiver une vieille autorisation ou rejouer une outbox |
| Soupçon de credential exposé | Arrêt, diagnostic et révocation fournisseur par l'opérateur autorisé ; retirer une entrée locale ne suffit pas |
| Autre lecteur ou webhook | Arrêter ; contrôle humain, aucune compétition ou correction implicite |
| Doute sur arrêt ou clôture | Aucun nouveau lancement ; conserver les preuves et déclarer NO-GO |

L'opérateur atteste ensuite la clôture réellement observée et les états finaux.
Une confirmation reçue mais non sauvegardée reste ambiguë. Une nouvelle commande
future ne transforme pas l'ancienne tentative en échec certain. Les résultats
de cet essai n'autorisent ni reprise automatique, ni basculement, ni autre phase.
