# news-telegram

Bot de collecte de flux RSS/Atom et de publication Telegram.

## Branches et sécurité

- `main` est la branche de production.
- `hardening/phase-1` est la branche de durcissement et de validation.
- Les tests automatisés n'appellent jamais l'API Telegram.
- Le workflow de contrôle manuel est limité à `hardening/phase-1`.
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
