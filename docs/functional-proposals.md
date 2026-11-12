# Propositions fonctionnelles différées

Ces pistes ne sont pas implémentées par la séparation des responsabilités.
Elles doivent donner lieu à des changements distincts, chacun avec comparaison
de comportement et décision explicite. Les données disponibles et leurs limites
figurent dans [maintenance-observations.md](maintenance-observations.md).

## File principale et tampon d'admission

**Proposition à évaluer :** une seule séquence FIFO pour les articles déjà admis,
avec admission explicite et capacité correspondant au fonctionnement actuel.
Le bénéfice recherché est la disparition des promotions persistantes
`admission_buffer → file`, pas l'introduction de statuts équivalents ailleurs.

La référence possède une file principale de 1 000 et un tampon FIFO de 1 000.
Les articles de la file précèdent ceux du tampon ; les nouveaux articles ne
dépassent pas le tampon même lorsqu'une place se libère dans la file.
**2 000 n'est toutefois pas la limite absolue de tous les articles déjà admis** :
un article de quarantaine redevenu frais lorsque ces deux structures sont pleines
reste dans `date_quarantine` avec `reason=admission_deferred` et `admitted_at`.
Il ne doit plus expirer pour vieillissement. Ces articles partagent la capacité
de 500 de la quarantaine avec les dates encore douteuses.

| Scénario | Référence à préserver ou changement à faire valider |
| --- | --- |
| Place disponible, tampon vide | Nouvel article frais ajouté derrière la file existante |
| Tampon non vide | Admission derrière les articles précédemment admis, sans dépassement |
| File et tampon pleins | Nouveau candidat non persisté : report, pas de marquage « vu », curseur bloqué pour permettre une reprise |
| Date corrigée, capacités pleines | Candidat conservé durablement comme `admission_deferred`, sans nouvelle expiration de fraîcheur |
| Article admis depuis plus de 24 h | Maintien en attente, puis publication selon son ordre |
| Date inconnue ou future | Quarantaine distincte des articles publiables |

Une file unique de capacité 1 000 réduirait la capacité et reporterait davantage
de candidats. Une limite de 2 000 qui supprimerait `admission_deferred` changerait
aussi le sort de certains candidats déjà admis. À l'inverse, déplacer ces derniers
dans une file pouvant contenir 2 500 éléments modifierait la répartition des
capacités entre nouveaux candidats et dates douteuses. Aucune de ces variantes
n'est considérée équivalente sans preuve.

Avant de choisir : construire des copies d'états avec 999/1 000 articles, tampon
vide/plein, quarantaine pleine et mélange de motifs ; inclure les métadonnées
historiques absentes. Comparer les articles retenus, l'ordre complet des envois,
les identifiants/URL mémorisés, les reports et les curseurs après interruption.
Vérifier aussi le point d'admission relatif des articles devenus frais en
quarantaine : la transformation ne doit pas inventer une chronologie globale
absente de l'ancien état.

La migration exige une copie représentative de l'ancien état et un résultat
attendu vérifié. Aucun format nouveau n'est retenu ici. Le retour arrière doit
soit convertir sans perte le nouvel état vers l'ancien format, soit restaurer
une sauvegarde cohérente avec réconciliation des publications intervenues depuis.
Un retour à l'ancien code seul ne suffit pas ; restaurer un ancien état peut
répéter des publications déjà acceptées par Telegram. Définir cette procédure
avant tout déploiement de migration, sans vider les files ni l'historique.

## Décision de doublon

**Proposition à évaluer :** centraliser la priorité et les effets de la décision
de doublon en conservant les historiques existants. La référence examine
notamment les articles en quarantaine et en attente, les identifiants déjà vus,
les empreintes URL persistées ou actives et les curseurs par flux. Une réponse
booléenne seule peut masquer les mutations nécessaires de ces historiques et
des curseurs ; la décision doit rendre ses effets explicites.

La première version centralisée doit préserver les sorties observables : même
identifiant avec URL modifiée toujours doublon ; nouvel identifiant et même URL
canonique toujours doublon ; même identifiant en quarantaine toujours réexaminable
pour une correction de date. Un GUID opaque reste un identifiant. Une chaîne qui
ressemble à une URL dans `vus` ne suffit pas à reconstruire arbitrairement une
association GUID/URL absente de l'ancien état.

Conserver le hash et la canonisation actuelle : fragment retiré, schéma/hôte et
ports par défaut normalisés, chemin et requête conservés. Supprimer globalement
les paramètres de requête ferait rejeter des ressources distinctes comme
doublons. Utiliser `updated` comme identité ou transformer tous les GUID en URL
ferait accepter ou rejeter d'autres articles sans justification.

Pour chaque réduction de chemin proposée, comparer : identifiant connu/URL
nouvelle ; identifiant nouveau/URL connue ; deux flux présentant la même URL ;
requêtes distinctes ; historique d'identifiants tronqué ; article en attente ou
en quarantaine ; reprise sur état v2 sans correspondance GUID/URL. Les compteurs
actuels agrègent identifiants et URL : ils ne permettent pas de chiffrer leurs
contributions respectives. Une centralisation équivalente peut avancer, mais
une réduction de couverture doit préciser les candidats différemment traités.

## Dates et quarantaine

**Proposition à évaluer :** simplifier les interactions après avoir comparé
chaque variante à la référence ; aucune suppression de règle n'est retenue.
Les 29 exécutions mesurées avec fraîcheur active contiennent 48 rejets trop vieux
et aucune transition de quarantaine de date. Cette courte absence de transition
ne démontre pas l'inutilité de la protection.

| Variante éventuelle | Articles dont le sort changerait |
| --- | --- |
| Accepter immédiatement les dates absentes ou imprécises | Articles actuellement différés, éventuellement historiques, désormais publiables dès la découverte |
| Utiliser la date de mise à jour à la place de la publication | Anciens articles réédités pouvant franchir la fenêtre de 24 h |
| Réduire la tolérance de cinq minutes dans le futur | Candidats actuellement admis désormais mis en quarantaine |
| Rejeter immédiatement les dates futures ou inconnues | Articles dont la source pourrait corriger la date désormais perdus au lieu d'être réexaminés |
| Faire expirer tous les articles après 24 h | Articles déjà admis, y compris `admission_deferred`, désormais supprimés avant publication |
| Unifier tous les délais de quarantaine | Dates futures plausibles jusqu'à sept jours ou dates inconnues expirant à un autre moment |

Le comportement de référence distingue date inconnue, date future et admission
déjà acquise mais différée. Les dates inconnues ont un délai fondé sur la première
observation ; les dates futures plausibles ont une échéance liée à la publication
annoncée, avec une limite de plausibilité. Un article devenu frais peut être
admis ou conservé durablement si les capacités sont pleines. Les erreurs Telegram
propres à un article sont encore une structure distincte (`erreurs`).

Toute proposition doit fournir des exemples déterministes aux frontières de
24 h, cinq minutes et sept jours, une horloge contrôlée, une correction de date
avant/après échéance, les files pleines, les changements d'identifiant et la
reprise. Indiquer pour chaque exemple qui devient accepté, différé ou rejeté.
Les journaux existants peuvent compléter ces scénarios ; aucun nouveau système
de télémétrie n'est requis pour avancer sur les extractions indépendantes.

## Hébergement et limites de livraison

Le Raspberry Pi est indisponible. Aucun achat, provisionnement, configuration,
nouveau stockage ou code spécifique au futur matériel n'est prévu. GitHub Actions,
la persistance Git actuelle et les sauvegardes locales atomiques restent les
contraintes du chantier.

Lors de l'annonce de disponibilité du matériel, ouvrir un chantier distinct
fondé sur la cadence réellement obtenue et souhaitée, la restauration et la
durabilité de l'état, les mises à jour, les sauvegardes, le réseau, l'alimentation,
la supervision et la reprise. Un stockage local ne garantit pas une livraison
exactement une fois : Telegram peut accepter un message avant un échec de
sauvegarde, puis le recevoir de nouveau après reprise. Cette limite reste à
documenter quelle que soit la plateforme.
