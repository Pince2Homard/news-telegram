# Supervision Telegram — préparation Phase 8.0

Le canal de notifications sera **l'ancien canal de test existant**, passé en
mode privé par le propriétaire. Il est nommé **Codex** et son identifiant
fourni par le propriétaire est **-1003833675690**. Aucun canal supplémentaire
n'est créé. Cet identifiant est configuré uniquement pour la supervision ; il
n'a pas été vérifié par un appel Telegram pendant cette phase.

## Séparation des destinations

Le watcher et son registre restent les points de suivi. Le futur bot dédié
sera un adaptateur de notification, sans second orchestrateur ni accès à
l'état de production. Aucun branchement de transport n'est activé dans cette
phase. Les notifications Windows et l'installation existante restent inchangées.

Le contrat optionnel `tools/windows_watcher/supervision.example.json` prévoit :

- `notifications_channel_id` : `-1003833675690`, destination de supervision
  fournie par le propriétaire, à vérifier avant activation ;
- `arbitration_private_chat_id` : conversation privée directe avec le bot dédié,
  distincte du canal de notifications et de ses éventuelles discussions ;
- `owner_user_id` : identité Telegram numérique du propriétaire, vérifiée
  séparément, jamais un nom public ou un titre ;
- `mode: simulation` et `send_enabled: false` : seuls réglages acceptés ici.

Les identifiants privés d'arbitrage et du propriétaire restent `null` dans
l'exemple ; aucun n'est déduit du canal Codex. Le futur fichier réel sera
`%LOCALAPPDATA%\NewsTelegramWatcher\supervision.json`, hors du dépôt et non
versionné ; il ne contiendra pas le token. Aucun paramètre de
supervision ne reprend par défaut `TELEGRAM_CHAT`, le token ou les secrets de
production. Aucun accès aux variables de production n'est implémenté.

## Simulation disponible

`supervision.py` est un contrat hors ligne appelé par les tests, sans CLI,
réseau, stockage, planification ou fonction d'envoi. Il simule un compteur
d'événements et la comparaison d'identifiants privés. Il refuse le mode réel,
les noms publics de canal et les options inconnues. Il n'est pas déployé dans
l'installation Windows actuelle. Les tests d'identité utilisent des identifiants
synthétiques ; le test de configuration du canal Codex reste aussi entièrement
hors ligne. Aucun identifiant n'est contacté.

## Conditions d'une future activation, à autoriser séparément

Avant tout envoi, vérifier via `getChat` l'identifiant et le type `channel` du
canal existant, avec le bot dédié et ses droits minimaux de publication. Vérifier
son identité attendue séparément ; le préfixe numérique seul ne prouve ni le
type ni la propriété d'un canal. Une URL publique n'est pas requise.

Pour les arbitrages futurs, n'accepter que les messages issus de l'API Telegram
authentifiée pour ce bot, avec `chat.type == private`, `chat.id` et `from.id`
égaux aux identifiants préalablement vérifiés, et `from.is_bot == false`.
Refuser groupes, canaux, expéditeurs anonymes, transferts et messages via un
autre bot. Ne pas faire confiance à un JSON arbitraire ou au nom d'affichage.
La simulation de correspondance n'est pas une authentification réelle.

Une identité reconnue ne suffit pas à autoriser une action : l'arbitrage devra
porter explicitement sur un UUID, un SHA, une action et une expiration, avec
déduplication et refus des références obsolètes dans le registre existant.
Aucun texte Telegram ou commentaire GitHub ne sera exécuté comme commande.
Les fusions, modifications sensibles et lancements d'assistant restent soumis
à validation humaine spécifique. Aucun traitement d'arbitrage n'est implémenté.

Le futur transport devra conserver l'intention durable avant envoi, respecter
les délais Telegram et arrêter sur résultat ambigu sans réessai automatique.
Une livraison ne doit jamais être confondue avec une autorisation. Les détails
de reprise seront testés avant toute activation ; cette phase ne garantit
aucune livraison réelle.

Référence officielle pour les objets et contrôles futurs :
[Telegram Bot API](https://core.telegram.org/bots/api).

Aucun jeton, appel Telegram, modification de production, tâche Windows ou fusion
n'est nécessaire à cette préparation. La PR #10 fusionnée reste intacte ; le
travail est isolé sur `codex/supervision-private-channel`, dans une PR dédiée.
