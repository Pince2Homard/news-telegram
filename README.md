# news-telegram

Bot de collecte de flux RSS/Atom et de publication Telegram.

## Branches et sécurité

- `main` est la branche de production.
- Les branches `hardening/*` servent aux corrections et validations avant fusion.
- Les tests automatisés n'appellent jamais l'API Telegram.
- Le workflow de qualité vérifie les modifications et les pull requests vers `main`.
- Le workflow de production s'exécute toutes les 10 minutes sur `main`.

## Configuration GitHub

Secrets requis dans le dépôt :
- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT`

Le workflow de production active explicitement `ALLOW_TELEGRAM_SEND=1` et `DRY_RUN=0`.
Le programme reste en simulation par défaut si cette autorisation explicite manque.

## Exécution locale sans publication

```bash
python -m pip install -r requirements.txt
python poster.py
```

Pour une simulation explicite :

```bash
DRY_RUN=1 python poster.py
```

Ne jamais activer l'autorisation d'envoi en local sans avoir vérifié le canal cible.

## Tests

```bash
python -m unittest discover -s tests -v
```

Les tests utilisent des transports simulés et ne publient aucun message Telegram.

## Collecte et file d'attente

- Les flux RSS/Atom sont téléchargés avec un délai de connexion de 5 s et de lecture de 20 s.
- La collecte dispose d'un budget de 3 minutes et reprend au prochain flux après chaque point de sauvegarde. L'exécution complète du bot est plafonnée à 12 minutes, sous la limite de 14 minutes du workflow.
- Le curseur global et le dernier article traité par flux sont écrits dans `state.json` par lots de cinq flux et à la fin de la collecte ; le curseur par flux protège aussi des doublons lorsque l'historique global de 5 000 IDs est tronqué. Le workflow de production ne crée qu'un commit d'état en fin d'exécution, comme auparavant.
- La file d'attente est plafonnée à 1 000 articles. Quand elle est pleine, les nouveaux articles sont différés et ne sont pas marqués comme vus : ils seront retentés au passage suivant.
- Au premier démarrage, les articles déjà présents dans les flux sont initialisés silencieusement. Lorsqu'un flux est ajouté à un bot déjà initialisé, ses articles historiques sont ignorés et son article le plus récent est traité normalement ; s'il ne peut pas entrer dans une file pleine, il reste retentable.
- Les erreurs Telegram définitives sont conservées dans la liste `erreurs`, limitée à 500 entrées.
- Les flux YouTube par identifiant de chaîne utilisent directement le flux RSS public YouTube. L'ancien format GDATA reste pris en charge par compatibilité.
- Les journaux de collecte donnent une durée et un résumé par flux. Les erreurs récurrentes sont suivies dans l'état, et les identifiants de journal n'exposent pas les paramètres d'URL.

## Garantie de livraison Telegram

La file assure une livraison « au moins une fois ». Si Telegram accepte un message puis que le processus s'interrompt avant la sauvegarde qui le retire de la file, l'article sera renvoyé après reprise et un doublon est possible. Une erreur réseau ambiguë n'est pas retentée dans le même passage, mais l'article reste en file pour la prochaine exécution. L'architecture actuelle ne garantit pas une livraison exactement une fois.

## État persistant

Pour le moment, `state.json` reste versionné dans Git afin de préserver la file et l'historique de déduplication. Son déplacement vers un stockage externe nécessite une migration dédiée et un secret supplémentaire ; ne pas supprimer le fichier Git avant validation de cette migration.
