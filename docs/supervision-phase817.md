# Phase 8.1.7 — diagnostics expurgés, sans nouvelle connexion

La première vérification a produit uniquement un message générique. Aucun
compteur ou détail antérieur n'est disponible ; sa cause et le nombre exact de
GET ne peuvent pas être reconstitués. Aucun credential réel n'est lu pour cette
analyse et aucun nouvel appel Telegram n'est autorisé pendant cette phase.

## Causes possibles de l'ancien message

Le bloc except de la CLI masquait indifféremment : configuration locale,
credential inaccessible/format/ACL/changement concurrent, initialisation du
transport, DNS, TLS, connexion, timeout, réponse HTTP, parsing API, identité du
bot, identité/type/visibilité du canal et permissions administrateur. La
présence du credential et un contrôle ACL préalable réussi n'excluaient pas
un refus lors de la relecture. Aucun élément ne permet de privilégier une
cause réseau ou une erreur de token aujourd'hui.

## Diagnostics désormais disponibles

La sortie JSON inclut un code issu d'une liste fermée, un message fixe, une
étape locale et les compteurs du transport. Aucun texte d'exception originale,
URL, chemin du credential, identifiant personnel ou corps distant n'est affiché.

| Codes | Interprétation |
| --- | --- |
| CREDENTIAL_PATH / READ / FORMAT / CHANGED | Chemin refusé, lecture impossible, format ou changement concurrent |
| CREDENTIAL_ACL | Contrôle ACL refusé ou inspection indisponible |
| DNS_ERROR / TLS_ERROR | Type d'exception DNS ou TLS identifié |
| NETWORK_TIMEOUT / NETWORK_CONNECTION | Timeout ou échec/interruption de connexion |
| RESPONSE_INTERRUPTED | Lecture HTTP interrompue |
| HTTP_400 / 401 / 403 / 404 / 429 / OTHER | Statut HTTP observé, sans interprétation du texte distant |
| HTTP_REDIRECT | Redirection refusée |
| API_INVALID | JSON, schéma ou taille de réponse invalide |
| BOT_IDENTITY | Réponse valide mais identité du bot incorrecte |
| CHANNEL_IDENTITY | Réponse valide mais ID/type/titre/visibilité du canal incorrect |
| ADMIN_PERMISSION | Réponse valide mais bot/droits administrateur non concordants |
| NETWORK_UNCONFIRMED / INTERNAL_ERROR | Catégorie non déterminée, sans prétendre identifier une phase |

Un HTTP 400/403/404 à l'étape getChat indique un canal non vérifié ou inaccessible,
sans prouver s'il s'agit d'un mauvais identifiant ou d'un défaut d'accès. Un
401 indique un refus d'authentification, sans révéler le credential. Un statut
HTTP d'erreur reste rapporté même si son corps est invalide. Le délai éventuel
429 n'autorise jamais un réessai. Les refus locaux restent sans appel réseau.
Pour sendMessage, les règles antérieures sont conservées : une réponse
malformée reste ambiguë et bloque tout nouvel envoi, quel que soit son statut.

## Sens exact des compteurs

- `prepared` : méthode et paramètres autorisés, avant préparation TLS ;
- `attempted` : ouverture de connexion commencée, avant résolution/connexion ;
- `request_started` par étape : entrée dans l'écriture HTTP, sans preuve de réception ;
- `http_responses` : en-têtes HTTP reçus, y compris réponses en erreur ;
- `confirmed` : réponse API de succès lue et structurellement valide. Cela ne
  confirme ni l'identité attendue ni une décision humaine.

Chaque étape ne conserve que le nom de méthode autorisé, la phase technique,
les booléens, un éventuel statut HTTP et le code borné. Une panne DNS peut donc
donner prepared=1, attempted=1, request_started=false, confirmed=0. Une réponse
getMe valide mais d'un autre bot donne confirmed=1 puis BOT_IDENTITY. Les trois
identités/droits ne sont validés que si la CLI termine avec status=verified.
La phase connect regroupe DNS/TCP/TLS : seule l'exception peut préciser la cause.
Le compteur ne prouve jamais qu'une requête ambiguë a été reçue par Telegram.

## Reprise future, toujours à autoriser

Après une nouvelle autorisation seulement, un unique --verify-real affichera
ces métadonnées, ou les compteurs à zéro si le credential est refusé avant le
transport. Aucun stockage de diagnostic persistant ni lancement automatique
n'est ajouté. Aucun compteur n'est branché sur une logique de retry. Les règles
d'envoi unique et de journal restent inchangées ; cette phase n'autorise aucun
sendMessage, nouvelle vérification réelle ou arbitrage interactif.
