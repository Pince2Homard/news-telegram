# Surveillance locale GitHub — Windows 10

Prototype en lecture seule pour les commentaires de coordination. Le programme n'appelle que des routes GitHub `GET`; aucun commentaire, log ou contenu RSS n'est interprété comme une commande. Le prototype ne lance pas Codex, n'écrit pas dans GitHub, ne touche pas au bot Telegram et ne crée pas de tâche planifiée.

## Architecture

- Python 3.12 standard library : client REST `urllib`, sans dépendance supplémentaire ni jeton pour ce dépôt public.
- Lecture de l'issue de coordination configurée (défaut `#7`) et des PR ouvertes. Les conversations PR, revues et commentaires en ligne sont relus lorsque la date `updated_at` du fil change.
- Reconnaissance des préfixes `[CHATGPT-DECISION]`, `[OWNER-DECISION]`, `[CODEX-REPORT]`, `[CHATGPT-REVIEW]` et `[CHATGPT-TASK]`. Le login est comparé à une allowlist locale, par défaut `Pince2Homard`. Un auteur non autorisé est marqué « non vérifié »; le commentaire reste une donnée, jamais une instruction.
- Curseurs numériques par fil/type, versions récentes bornées et boîte de notifications locale. L'état est écrit atomiquement sous `%LOCALAPPDATA%\NewsTelegramWatcher\state.json`; l'historique retient au plus 500 résumés sans corps de commentaire. Un verrou système exclusif empêche deux relevés simultanés et se libère à la fermeture/crash du processus.
- Une notification WinForms `NotifyIcon` récapitule le nombre d'événements. Aucun texte de commentaire ni login n'est transmis à PowerShell. La notification est livrée après la persistance de la boîte locale.
- `--simulate` utilise une API factice, un état temporaire et une notification factice; il n'accède pas au réseau et ne modifie pas l'état réel.

Le premier relevé établit silencieusement les curseurs pour éviter un flot de notifications historiques. Les nouveaux commentaires apparaissant pendant ce relevé sont détectés au relevé suivant lorsque leur horodatage est distinct de la photographie initiale. Comme toute surveillance par polling, un commentaire ajouté dans la même seconde que le relevé initial peut se confondre avec le point de départ.

## Prérequis

- Windows 10 avec session utilisateur ouverte pour afficher les notifications.
- Python 3.12 installé.
- Accès HTTPS sortant vers `api.github.com`.
- Dépôt public : aucune authentification ou permission GitHub n'est demandée. Les requêtes non authentifiées sont limitées à 60 par heure par adresse IP; les réponses 403/429 suspendent le relevé sans avancer les curseurs. Avec 30 minutes entre les relevés, le coût normal est de deux requêtes de métadonnées, plus jusqu'à trois routes de commentaires par PR active dont le fil a changé. Au-delà d'environ huit PR ouvertes ou lors d'une forte activité, choisir 60 minutes.

## Vérification hors ligne

Depuis la racine du dépôt :

```powershell
py -3.12 tools\windows_watcher\watcher.py --simulate
py -3.12 -m unittest discover -s tests -v
```

Les tests emploient des réponses factices; ils n'effectuent aucun appel réseau, notification Windows ou planification.

## Installation manuelle (aucune tâche planifiée)

Ces étapes ne sont pas exécutées par Codex. Les faire seulement après validation du propriétaire :

1. Copier `tools\windows_watcher` vers `%LOCALAPPDATA%\NewsTelegramWatcher\app`.
2. Copier `config.example.json` vers `%LOCALAPPDATA%\NewsTelegramWatcher\config.json`. Vérifier le dépôt, l'issue, les auteurs autorisés et l'intervalle `poll_interval_minutes`.
3. Exécuter d'abord `py -3.12 watcher.py --simulate` depuis le dossier `app`.
4. Pour le premier relevé réel en lecture seule, exécuter `py -3.12 watcher.py --once --config "%LOCALAPPDATA%\NewsTelegramWatcher\config.json"`. Il établit un baseline silencieux; les passages suivants créent des notifications et un historique consultable avec `--history`.

Le watcher n'a pas de mode d'installation de tâche. Après validation séparée, l'utilisateur peut créer une tâche dans le Planificateur Windows : exécuter seulement lorsque l'utilisateur est connecté; déclencheur à l'ouverture de session, répétition de 30 minutes indéfiniment (60 minutes si plus de huit PR ouvertes), action `python.exe` avec les arguments `watcher.py --once --config <chemin-config>`, et démarrage depuis le dossier `app`. Activer « démarrer dès que possible après un démarrage planifié manqué »; ne pas demander l'élévation administrateur. Cette tâche affiche seulement des notifications et exécute uniquement ce script fixe.

## Désinstallation

Après arrêt de la tâche par l'utilisateur, ouvrir le Planificateur Windows, supprimer `NewsTelegramGitHubWatcher`, puis supprimer le dossier `%LOCALAPPDATA%\NewsTelegramWatcher\app`. Garder ou sauvegarder `state.json` si une réinstallation doit reprendre ses curseurs. Pour effacer aussi la configuration, l'historique et l'état, supprimer le dossier `%LOCALAPPDATA%\NewsTelegramWatcher` après vérification de son chemin.

## Limites / arbitrages

- Les notifications sont un résumé; les détails sont dans `--history` et le lien GitHub. Le premier baseline est silencieux.
- L'état local protège des doublons usuels et reprend les notifications restées en attente. Une interruption exactement après l'affichage Windows mais avant l'accusé de réception local peut répéter une notification; une livraison exactement-une-fois n'est pas garantie par Windows.
- Les PR fermées ne sont plus interrogées. Pour les suivre à nouveau, elles doivent être rouvertes ou ajoutées dans une évolution du prototype.
- Le jeton GitHub n'est pas nécessaire pour le dépôt public. Si le dépôt devenait privé, concevoir séparément le stockage d'un jeton à portée minimale dans le Gestionnaire d'identifiants Windows.
- Les notifications locales indiquent l'auteur autorisé ou non, mais aucun contenu n'est approuvé automatiquement. Les arbitrages sensibles restent manuels.
