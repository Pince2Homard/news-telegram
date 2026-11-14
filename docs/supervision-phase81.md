# Phase 8.1 — connexion ponctuelle préparée, non activée

Aucun travail Phase 8.1 antérieur n'a été trouvé lors de la reprise de la
branche. La Phase 8.0, son exemple et ses verrous restent inchangés.

`tools/windows_watcher/telegram_once.py` réutilise le verrou système et les
sauvegardes atomiques du watcher. Son moteur accepte un transport injecté,
exclusivement simulé dans cette phase. Aucun adaptateur HTTP réel ni commande
d'utilisation d'un token n'est exposé. Il n'est appelé ni par le tableau,
ni par le watcher, ni par un service. Aucun fichier installé n'est modifié.

## Contrôles préparés

Avant chaque première tentative : `getMe` doit identifier le bot
`@Codex2PinceBot`, puis `getChat` le canal privé `Codex`, ID
`-1003833675690`, type channel, sans nom public. `getChatMember` doit confirmer
que le même bot est administrateur avec `can_post_messages=true`. Ces méthodes
ne sont pas appelées réellement ici. Aucun bot n'est ajouté administrateur.
Référence : [Telegram Bot API](https://core.telegram.org/bots/api).

Le canal, le bot et le message sont verrouillés dans l'essai. L'empreinte
d'approbation lie ces trois éléments et la référence d'essai. Le journal
`telegram-once.json`, sous le verrou `telegram-once.lock`, est écrit durablement
avant sendMessage. Tout journal existant bloque une seconde tentative, même
après interruption, refus explicite ou 429. Retry-After est enregistré sans
attente suivie d'un renvoi. Le résultat ambigu reste incertain ; aucun historique
accessible via Bot API ne permet ici de prouver une absence d'envoi. Ne jamais
effacer le journal ou changer de répertoire pour réessayer. Un échec disque
après envoi laisse l'intention in_flight, qui interdit aussi le renvoi.

Pour un futur essai réel, le répertoire devra être durable et fixe sous
`%LOCALAPPDATA%\NewsTelegramWatcher\telegram-once`, jamais dans le dépôt ou dans
un répertoire temporaire. Le mode CLI actuel utilise uniquement un temporaire
pour ses simulations, sans toucher aux données locales du watcher.

## Credential : aucune utilisation réelle à cette étape

Le futur token du bot dédié devra rester hors dépôt, hors configuration
versionnée, hors arguments, journaux et commentaires. Aucun repli vers le token
de production ou ses variables n'est prévu. La fonction de lecture isolée
refuse les chemins dans le dépôt, liens directs, fichiers trop grands et,
sous POSIX, permissions ouvertes aux autres comptes. Elle n'est testée qu'avec
des fixtures locales, sans token réel. Sous Windows, elle refuse toute lecture
tant qu'un contrôle des ACL du fichier et de ses parents n'a pas été ajouté et
validé : ce point est un blocage volontaire avant connexion réelle. Aucun token
n'est demandé ni enregistré pendant cette phase. Ne jamais copier une erreur
HTTP brute susceptible de contenir l'URL d'authentification.

## Message exact proposé, non approuvé pour envoi

```text
[TEST SUPERVISION CODEX — notification unique]
Vérification ponctuelle du bot @Codex2PinceBot dans le canal Codex.
Aucune action sur le bot de production. Aucune décision demandée.
Référence : phase81-codex-single-notification-v1
```

Cette notification ne recueille aucune décision. L'authentification privée,
UUID/SHA/actions/expiration et validation humaine restent un chantier séparé.
Aucune correspondance simulée d'identité ou statut local n'autorise un lancement
d'assistant, une modification GitHub ou une fusion.

## Vérification locale sans credential

Depuis la branche de développement, sans mettre à jour l'installation :

```powershell
py -3.12 tools/windows_watcher/telegram_once.py --simulate
py -3.12 -m unittest discover -s tests -p test_telegram_once.py -v
```

La sortie affiche le message, son empreinte et les appels simulés ; elle doit
indiquer confirmed, second_call_blocked=true, real_messages_sent=0.

Avant une future utilisation réelle : autoriser séparément le traitement du
credential et les vérifications sans envoi ; implémenter et valider l'adaptateur
HTTP avec TLS, timeouts bornés, aucun retry/redirect et erreurs expurgées, ainsi
que les ACL Windows. Vérifier alors les trois identités/droits par appels réels
autorisés. Soumettre le SHA, le texte exact, l'empreinte et la procédure bornée
à une nouvelle approbation d'envoi. Aucun lancement réel n'est prêt ni autorisé
par cette phase seule.
