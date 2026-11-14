# Supervision Telegram — préparation Phase 8.0

La préparation ponctuelle suivante et ses simulations sont décrites dans
[Phase 8.1](supervision-phase81.md). Le transport reste exclusivement manuel ;
aucune notification automatique n'est activée.

## Bilan de validation avant intégration — 14 novembre 2026

Les notes 8.0 à 8.1.7 ci-dessous décrivent les étapes successives de préparation.
Le parcours manuel ponctuel a depuis été validé avec les autorisations du
propriétaire : identité de @Codex2PinceBot, canal privé Codex et droit
can_post_messages confirmés par trois réponses API HTTP 200. Un contrôle
précédent avait signalé ADMIN_PERMISSION ; le propriétaire a ensuite activé
uniquement la publication. L'ancien premier échec non instrumenté ne peut
toujours pas être attribué rétrospectivement à une cause certaine.

L'unique message autorisé a été confirmé sous l'identifiant Telegram **1947**.
Le journal durable reste **confirmed, attempts=1**, avec la référence
phase81-codex-single-notification-v1 et l'empreinte approuvée. L'autorisation est
consommée. Aucun nouvel essai réel n'est nécessaire à la finalisation de la PR.
Le blocage d'une répétition est vérifié hors ligne sur une copie du journal ;
le fichier réel n'est ni modifié, ni déplacé, ni supprimé.

La configuration send_enabled=false concerne le contrat de supervision ; elle
ne lance rien. Le parcours manuel séparé --send-real exige sa propre approbation
exacte et reste bloqué par le journal de cet essai. Il n'existe pas de liaison
automatique entre les événements du watcher et Telegram. Les commandes réelles
ne sont pas des commandes d'installation ni des exemples à rejouer.

Compatibilité : le watcher Windows conserve son registre, sa configuration et
son manifeste d'installation actuels. Les modules Telegram ne sont pas ajoutés
automatiquement par manage.py install et s'utilisent explicitement depuis le
dépôt de développement. La fusion ne constitue ni une installation locale ni
une activation de service ou de tâche.

Risques résiduels : résultat réseau ambigu sans preuve de non-livraison, perte
du verrou anti-doublons si le propriétaire supprime volontairement le journal,
timeouts par opération sans budget total, contrôles ACL conservateurs et
administrateurs locaux considérés de confiance. Les compteurs API ne remplacent
pas les contrôles d'identité. Aucun arbitrage interactif n'est implémenté.

Retour arrière : arrêter d'utiliser les commandes de supervision ; une éventuelle
annulation Git doit retirer uniquement les ajouts de supervision. Ne jamais
restaurer un ancien state.json de production. Conserver le journal confirmé,
les reçus, la configuration et le credential protégé ; revenir à un ancien code
n'autorise pas à réutiliser l'approbation d'envoi. L'installation Windows actuelle
n'étant pas modifiée par cette PR, elle n'a pas à être restaurée pour l'annuler.

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
