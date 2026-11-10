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
- La file d'attente est plafonnée à 1 000 articles. Quand elle est pleine, les nouveaux articles sont différés et ne sont pas marqués comme vus : ils seront retentés au passage suivant.
- Les erreurs Telegram définitives sont conservées dans la liste `erreurs`, limitée à 500 entrées.
- Les flux YouTube par identifiant de chaîne utilisent directement le flux RSS public YouTube. L'ancien format GDATA reste pris en charge par compatibilité.

## État persistant

Pour le moment, `state.json` reste versionné dans Git afin de préserver la file et l'historique de déduplication. Son déplacement vers un stockage externe nécessite une migration dédiée et un secret supplémentaire ; ne pas supprimer le fichier Git avant validation de cette migration.
