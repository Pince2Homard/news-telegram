# Vérifier la destination avant renommage

Ce workflow temporaire ne publie aucun message et ne modifie ni secret ni état.
Il utilise uniquement `getChat`, avec le token déjà présent dans GitHub Actions.
Il n'a aucun déclencheur automatique, aucun checkout, aucune dépendance Python
externe et aucune permission du GITHUB_TOKEN. L'upload de résultat utilise le
jeton d'artefact fourni par Actions. Il ne participe pas à la concurrence de
publication : aucune file n'est chargée et aucune méthode d'envoi n'est appelée.

## Première vérification (après revue et fusion explicitement autorisées)

1. Dans Telegram, relever le **titre exact** du canal actuel portant le nom
   public `@LeBazaarCryptoCulture`. Ne pas déduire le titre de ce nom public.
2. Sur `main`, lancer manuellement « Vérifier le canal Telegram (lecture seule) ».
   Renseigner ce titre ; laisser `verify_configured_destination` à `false`.
3. Attendre la réussite : type `channel`, nom public actuel et titre exact doivent
   tous correspondre. Une erreur ne produit aucun résultat exploitable.
4. Télécharger l'artefact `verified-telegram-channel` du run réussi. Le fichier
   JSON fournit `verified_channel_id`, récupéré dans la réponse réelle de Telegram,
   et les indicateurs de vérification. Il ne contient ni token ni valeur brute
   de TELEGRAM_CHAT. L'ID peut être masqué dans les logs s'il était déjà secret ;
   l'artefact permet de le récupérer sans contourner le masquage des journaux.
   Sa conservation est limitée à un jour ; l'accès suit les droits du dépôt.

## Mise à jour et seconde vérification (action distincte à autoriser)

1. Avant de renommer le canal, ouvrir les paramètres du dépôt : Secrets and
   variables → Actions → secret `TELEGRAM_CHAT` → Update.
2. Saisir **exactement** l'identifiant numérique du résultat réussi, avec son
   signe négatif. Ne pas l'inventer et ne pas modifier TELEGRAM_TOKEN.
   Utiliser l'interface évite de placer l'identifiant dans un argument de commande.
3. Relancer le même workflow sur `main`, avec le même titre attendu et
   `verify_configured_destination=true`.
4. Exiger sa réussite et `configured_numeric_destination_verified=true` dans
   le nouvel artefact. Le workflow exige un secret numérique égal à l'ID obtenu
   par le nom public actuel, puis consulte aussi `getChat` avec cette destination.
5. Seulement après cette confirmation, changer le nom public dans Telegram vers
   `@AgoraCitoyenneCulturelle`, sans créer ni remplacer le canal et sans changer
   les droits du bot. Le workflow est volontairement fixé à l'ancien nom et
   n'est pas destiné à être relancé après renommage.

La production continue normalement ; cette procédure n'envoie aucun message de
test. En cas d'échec, ne pas renommer : conserver la destination existante ou
faire vérifier la saisie avant toute nouvelle modification. Les erreurs réseau
sont rapportées sans URL, token, corps brut ou traceback. Une panne réseau n'est
pas une preuve de changement d'identité.

Après l'opération, le workflow temporaire pourra être retiré par une autre petite
PR. Cette préparation n'autorise ni sa fusion, ni son déclenchement, ni la mise à
jour du secret ou le renommage du canal.
