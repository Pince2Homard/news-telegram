# Phase 6 — Diagnostic borné des refus Telegram

Un événement `TELEGRAM_REFUSAL | {JSON}` complète les journaux existants.
Il contient seulement `http_status`, `reason`, `classification`,
`duration_seconds` et `attempt`. Aucune description libre, réponse brute,
URL d'API, contenu d'article, token ou identifiant de chat n'est exporté.

Pour une enveloppe d'erreur cohérente avec HTTP 400, les fragments déjà utilisés
par la classification produisent `chat_not_found`, `too_long`, `empty` ou
`parse_entities`. Sinon, le motif est `unknown_400`. Une réponse incohérente ou
illisible porte `invalid_response`, sans déduire une cause de son texte.
Les autres catégories HTTP existantes restent disponibles ; un 429 certain
est journalisé `rejected`, y compris avant un réessai déjà autorisé.

La classification affichée est celle de la branche effectivement suivie :
`permanent`, `global`, `uncertain` ou `rejected`. Le diagnostic ne choisit
aucune action. Les durées du nouvel événement sont bornées à 86400 secondes,
les durées non finies deviennent nulles ; cela ne modifie pas les métriques
existantes. Une défaillance de ce nouvel événement est ignorée pour préserver
la décision de publication.

Exemple synthétique :

```json
{"attempt":1,"classification":"permanent","duration_seconds":0.475,"http_status":400,"reason":"parse_entities"}
```

Les décisions Telegram, la cadence configurée de 15 secondes, le plafond des
tentatives, les budgets, FIFO, déduplication, sauvegardes, reprise différée et
verrous DRY_RUN/ALLOW_TELEGRAM_SEND ne sont pas modifiés. Aucun ancien article
n'est réintroduit. La PR #50 n'est ni intégrée ni requise.

Validation locale Windows / Python 3.12.10 : 301 tests réussis, dont 8 nouveaux
tests, Ruff, compileall, pip check et contrôle du diff conformes. L'audit des
dépendances et les CI sont consignés dans le rapport final de la PR.
Les nouveaux tests interdisent sockets/DNS et utilisent des réponses simulées
ainsi que des sauvegardes en mémoire. Aucun appel Telegram réel.

Limites : ces catégories faciliteront un futur diagnostic sans conserver le
texte exact du refus. Elles ne permettent pas de reconstituer la cause des
128 erreurs du 11 novembre. Un motif inconnu reste inconnu ; il n'autorise
aucune remise en file automatique. Les risques préexistants de doublon après
une réponse incertaine ou un échec de sauvegarde restent inchangés.

Retour arrière : retirer les ajouts de diagnostic par revert du commit sur
le code courant, après autorisation. Ne jamais restaurer un ancien state.json
ni rejouer des articles pour annuler ce changement. Aucune migration d'état.
