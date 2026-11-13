# Watcher GitHub local — Phase 7.1, Windows 10

Ce watcher lit GitHub et prépare une boîte locale ; il ne lance jamais Codex,
ChatGPT, Git ou Telegram. Les commentaires restent des données non fiables,
y compris ceux d'un auteur connu. Aucune fusion n'est effectuée.

## Prérequis et vérification

Python **3.12**, Windows PowerShell/WinForms, accès HTTPS à api.github.com pour
les relevés réels. Notifications : session utilisateur ouverte ; leur affichage
reste soumis à Windows et à l'Assistant de concentration. Aucun jeton requis
pour le dépôt public. Aucun outil de développement requis pour l'installation.

Depuis la copie de développement, avec un Python dont la version est vérifiée :

```powershell
py -3.12 --version
py -3.12 tools\windows_watcher\watcher.py --simulate
py -3.12 tools\windows_watcher\manage.py install
py -3.12 tools\windows_watcher\manage.py check
```

Les commandes sont à exécuter explicitement par l'utilisateur, pas par les tests.
`install` copie seulement les fichiers connus vers
`%LOCALAPPDATA%\NewsTelegramWatcher\app`, crée la configuration si absente et
conserve configuration, état et journaux existants. Pour une mise à jour,
réexécuter install depuis la copie de développement. Un manifeste conserve les
empreintes des fichiers et le chemin absolu de Python ; ne pas supprimer cet
interpréteur. Préférer Python installé durablement à un venv de travail.
`check` contrôle ces fichiers, Python, la configuration et l'état sans réseau,
sans notification, sans écriture et sans réparation automatique. Ses avertissements
sur la session interactive ne constituent pas une preuve d'affichage.

## Relevé et traitement local

```powershell
$root = Join-Path $env:LOCALAPPDATA 'NewsTelegramWatcher'
py -3.12 "$root\app\watcher.py" --once --config "$root\config.json"
py -3.12 "$root\app\watcher.py" --list --config "$root\config.json"
py -3.12 "$root\app\watcher.py" --list --json --config "$root\config.json"
py -3.12 "$root\app\watcher.py" --ack IDENTIFIANT --status acknowledged --config "$root\config.json"
py -3.12 "$root\app\watcher.py" --ack IDENTIFIANT --status done --config "$root\config.json"
```

`ignored` est également disponible. Les statuts locaux sont `new`,
`acknowledged`, `done`, `ignored`. Aucun ne vaut approbation d'une directive ou
permission GitHub. La liste donne identifiant, statut, préfixe, classification
de l'auteur et lien ; elle ne copie pas le corps des commentaires. L'export JSON
permet de transmettre les références à un assistant sans recopier les rapports.
`--history` conserve le journal récent des notifications, distinct du traitement.
La livraison d'une bulle ne retire pas l'événement de la boîte à traiter.

Le premier relevé est un baseline silencieux. Le code relit ensuite les sources
de commentaires même si la date du fil n'a pas changé : les empreintes détectent
les éditions dans la même seconde. Un dernier relevé est fait pour une PR suivie
qui vient de fermer ; les commentaires ultérieurs à ce dernier relevé ne sont
pas surveillés. Seuls les préfixes du [protocole](PROTOCOL.md) sont notifiés,
pas les corps des PR/issues ni les résultats CI. Les anciennes installations
schema_version=1 sont reprises sans effacer leurs curseurs. Les anciennes éditions
sans empreinte reçoivent un point de départ ; leur ancien contenu ne peut pas
être reconstruit. La comparaison d'empreintes garde les 4096 derniers commentaires
par fil ; une édition ancienne hors de cette fenêtre dépend de son horodatage.

## Quota, interruptions et erreurs

Le client fait uniquement des GET, sans authentification. GitHub autorise 60
requêtes anonymes par heure/IP, quota partagé avec les autres programmes. Coût
minimal d'un passage : 3 + 3 × nombre de PR ouvertes, plus 2 appels par PR
référencée dans le registre pour les CI, davantage avec pagination, fermeture
récente et préparation de transfert (3 relectures finales par paquet). 30 minutes convient pour quelques PR ; utiliser 60 minutes
si l'activité augmente. La valeur JSON ne lance aucun timer à elle seule.

403/429 : le prochain passage respecte Retry-After, le reset du quota quand il
est épuisé et au moins 60 secondes. L'échéance est persistée sans avancer de
curseurs partiels. Aucun réessai immédiat ; 50 requêtes maximum par processus.
Une réponse réussie épuisant le quota empêche la requête suivante. Une longue
pagination dépassant ce budget peut empêcher un cycle complet : diagnostiquer
ce cas avant de prévoir une authentification séparée. Les autres erreurs réseau
sont reportées au prochain lancement, sans effacer l'état.

Les écritures sont atomiques et les relevés/accusés sont verrouillés. Au plus
1000 événements non terminés sont conservés ; la saturation arrête la progression
plutôt que de les jeter. Les 500 derniers événements terminés et 500 notifications
sont conservés. Un crash après affichage mais avant sauvegarde peut répéter une
notification ; l'identifiant d'événement et le traitement local restent séparés.
Les journaux UTC tournent sous watcher.log (512 Ko, deux sauvegardes).

## Planification préparée, jamais activée

```powershell
py -3.12 tools\windows_watcher\manage.py prepare-task
```

Cette commande produit seulement `task.disabled.xml` dans le dossier local :
tâche **et déclencheur désactivés**, ouverture de session de l'utilisateur courant,
répétition selon config.json, droits ordinaires, chemins absolus, concurrence
IgnoreNew et limite 15 minutes. Elle n'appelle aucun outil d'enregistrement de tâche.
Une modification d'intervalle nécessite de régénérer le XML.

L'importation dans le Planificateur, la vérification de l'identité et l'activation
nécessitent une autorisation distincte. Nom conseillé : NewsTelegramGitHubWatcher.
Après autorisation, un essai visuel et un relevé réel GET seront nécessaires avant
de déclarer le fonctionnement Windows validé de bout en bout.

## Désinstallation conservatrice

Désactiver et supprimer d'abord toute tâche importée dans le Planificateur, puis :

```powershell
py -3.12 tools\windows_watcher\manage.py uninstall
```

Seuls les fichiers d'application connus, le manifeste et le XML sont supprimés.
Aucune suppression récursive ; configuration, état, journaux et éventuels fichiers
inconnus restent présents. La commande ne manipule pas le Planificateur. Archiver
ces données si nécessaire ; leur purge n'est pas incluse.

## Coordination

Voir [PROTOCOL.md](PROTOCOL.md). Une lecture directe des liens GitHub par
l'assistant évite le copier-coller des corps. Publier des rapports ou transférer
vers une autre conversation exige une autorisation explicite. Aucune API d'assistant,
clé OpenAI, lancement de CLI agent ou automatisation d'interface n'est ajouté.

## Usage simplifié de coordination (Phase 7.3)

Le développement ne met pas à jour l'installation validée en Phase 7.2.
Les commandes ci-dessous supposent une mise à jour explicite de l'installation,
après validation ; aucune tâche planifiée n'est requise.

```powershell
py -3.12 "$root\app\watcher.py" --refresh --config "$root\config.json"
```

Un seul relevé manuel collecte les références, compare les SHA, relève les CI,
prépare les paquets de données de lecture seule admissibles et écrit dashboard.html.
Ouvrir ensuite ce fichier local : sections rapports/revues, directives, décisions,
éléments non vérifiés/conflits, éléments traités ou obsolètes. Les liens vont aux
commentaires GitHub, aux contrôles CI et aux paquets locaux déjà préparés.
Aucun navigateur ni assistant n'est ouvert automatiquement. La vue affiche la
date du relevé et les statuts locaux/métier distincts. Les marqueurs anciens ou
JSON invalides restent visibles séparément ; aucun corps libre n'est affiché.
Le tableau HTML est statique, sans scripts, serveur, formulaires ou ressources
externes. Il ne peut ni accuser réception ni déclencher une commande par clic.

Sans réseau, --dashboard régénère la vue du cache ; une donnée en cache n'est
pas une validation actuelle. --list et --ack EVENT_ID restent disponibles. Pour
préparer une directive isolée plutôt que toutes les directives admissibles :

```powershell
py -3.12 "$root\app\watcher.py" --prepare-transfer UUID --config "$root\config.json"
```

Cette commande relit GitHub et écrit seulement transfers/UUID. Les conditions,
le format et les limites sont précisés dans [PROTOCOL.md](PROTOCOL.md). Un UUID
peut être copié depuis le tableau, mais le contenu du rapport/directive n'a plus
à l'être. Les rapports restent sur GitHub, les paquets locaux n'y sont pas publiés.
Aucune option ne lance Codex/ChatGPT ou ne confère une permission de fusion.

La simulation --simulate couvre maintenant aussi registre, CI factices, tableau
et paquet local dans un répertoire temporaire. Elle ne valide pas les interfaces
d'un assistant réel. L'installation actuelle, sa configuration et ses curseurs
restent intacts tant qu'une mise à jour n'est pas explicitement autorisée.
