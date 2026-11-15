# Frontière de stockage privé — issue #43

Livraison fonctionnelle limitée au [commentaire directeur](https://github.com/Pince2Homard/news-telegram/issues/43#issuecomment-6894829695).
**Exploitation réelle et Telegram : NO-GO.** Aucune nouvelle phase, installation
ou préparation de répertoire opérationnel n'est exécutée.

## Changement et utilisation

Les composants recevaient déjà leurs chemins explicitement. Le contrôle ACL C0
ne couvrait cependant pas uniformément fence/witness, trial et le contrôle
opérateur. `private_boundary.PrivateStorageBoundary` fournit un contrôle passif
commun ; il ne crée ni contrôleur, ni lecteur, ni transport.

Le constructeur exige les cinq chemins absolus distincts `store`, `fence`,
`witness`, `trial` et `control`, une référence d'approbation manuelle et le chemin
explicitement déclaré du stockage de publication à exclure. Aucun chemin n'est
découvert. La référence permet de retrouver la décision humaine ; ce n'est ni une
signature d'autorisation, ni une autorité anti-rollback. Le code appelant reste
de confiance et doit fournir les chemins effectivement approuvés par l'opérateur.

Le même objet est passé par `boundary=` à DeliveryStore/QualifiedStore,
FixtureFence, FixtureTrial et NativeStopGate. FixtureIntegration refuse de
mélanger ressources gardées et non gardées, ou deux frontières différentes.
L'adaptateur local contrôle aussi cette cohérence avant d'acquérir le secret
synthétique ou d'écrire la requête. Le secours `stop_fixture` accepte explicitement
la même frontière ; aucun nouveau démarrage ni CLI opérationnelle n'est ajouté.

Les interfaces historiques sans frontière restent réservées aux fixtures. Elles
ne deviennent pas un chemin de production et conservent leurs restrictions
existantes. Aucun mode réel ni fournisseur de credentials réels n'est ajouté.

## Refus et invariants

- Q (famille `NewsTelegramPrivate-Qualification-*`), Temp/tmp, workspace,
  répertoires Codex/worktrees et emplacements de publication sont refusés
  lexicalement avant inspection. Aucun repli ni réparation automatique.
- Les cinq répertoires doivent déjà exister. `create=True` est interdit avec une
  frontière ; les helpers C0 de sauvegarde ne peuvent pas provisionner une
  destination sous ce mode. Leur approbation opérationnelle reste à définir.
- Inspection Windows en lecture seule via `credential_acl.inspect/validate`,
  règles C0 conservées : propriétaire de confiance, DACL non nulle, contrôle des
  parents, refus des reparse points/liens et fichiers à liens multiples. DACL
  protégée sur les cinq racines ; toute ACE Allow tierce sur les objets privés
  est refusée, y compris inherit-only et `CodexSandboxUsers`, sans exception
  fondée sur le nom du groupe. Les parents gardent les règles C0 de remplacement.
- Tous les descendants présents, dont journaux SQLite, verrous, marqueurs et
  tickets, sont inspectés (plafond de 512 objets/parents). Les sous-dossiers de
  travail doivent rester sous control. Racines/parents identifiés par volume et
  inode ; changement de compte, remplacement ou preuve incomplète : refus.
- Relecture au démarrage, aux entrées d'accès, avant les transactions et leur
  commit. Une anomalie ferme définitivement l'objet en mémoire ; aucune remise
  à zéro n'efface reçus, intentions, epochs ou preuves de génération.
- Reçus/curseur restent dans leur transaction B1. B2 conserve intention avant
  tentative, résultat durable et absence de retry sur uncertain. Une preuve C2
  refusée ne peut pas sceller une session dirty. La restauration C0 reste désarmée.
- L'arrêt OS et la récolte du worker précèdent toujours la tentative d'écrire
  la preuve de fermeture. Un refus disque ne bloque pas le signal d'arrêt ni
  la fermeture des handles déjà détenus ; il interdit de déclarer une reprise sûre.

## Preuves et limites de cette livraison

Tests ciblés sous Python 3.12 sur Windows : métadonnées ACL/filesystem synthétiques,
transactions SQLite réelles sur fixtures isolées, régressions B1/B2/C0/C2/C3/F3.
Les nouveaux tests couvrent refus lexical sans lecture de Q, ACE tierces,
héritage, annexes, principal, liens/remplacement, quota, raccord partiel,
rollback du curseur, uncertain sans réémission, génération dirty et restauration.
Les contrôles ACL natifs positifs du futur emplacement ne sont **pas** réalisés.
Les 446 tests natifs précédents ne sont pas relancés ; le nettoyage des caches et
le test TLS local déjà terminés ne sont pas répétés. Résultats définitifs et CI
sur le SHA livré publiés dans la PR et l'issue #43.

La vérification par snapshots n'est pas une ouverture atomique par handles :
elle ne protège pas contre un administrateur malveillant, un changement concurrent
entre contrôle et accès, ni un rollback coordonné de toutes les preuves locales.
Les inspections PowerShell sont bornées par l'outil existant mais peuvent ajouter
une latence importante ; aucun budget opérationnel ni contexte réel n'est qualifié
par ces simulations. Les protections Windows, runtime, secrets et WAN déjà
identifiées dans F1–F3 restent des conditions indépendantes.

## Point d'arrêt opérateur

La PR est à auditer sans fusion automatique. Le point concret suivant est de
désigner un contexte Windows manuel **hors Codex**, avec compte non administrateur
et cinq chemins sous son profil, distincts de la publication et de Q. Aucun de ces
objets n'est créé ici. Leurs noms absolus, ACL attendues et contrôles natifs devront
être approuvés avant toute préparation effective ; aucun lancement réel ne découle
de l'existence de cette frontière. Aucun changement d'ACL existante ni retrait de
l'ACE observée n'est proposé dans cette livraison.
