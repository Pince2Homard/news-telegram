# Coordination GitHub et notification historiques

Le bot de publication reste sur GitHub Actions. Le transport privé Windows,
ses magasins, credentials, workers et son installateur sont abandonnés par la
[décision #43](https://github.com/Pince2Homard/news-telegram/issues/43#issuecomment-6900486696).
Aucune installation existante sur le poste n'est modifiée.

| Conservé | Raison |
|---|---|
| coordination.py, publication_tracking.py, report_publication.py | Registre, protocoles GitHub, rapports et suivi des publications |
| watcher.py, config.example.json | Configuration, état, verrou et contrôles de chemins encore requis par les rapports et telegram_once ; la CLI historique n'est pas le futur contrôleur |
| telegram_once.py, telegram_http.py, telegram_diagnostics.py | Test ponctuel historique, déduplication et diagnostic associé |
| credential_acl.py | Vérificateur en lecture seule requis par telegram_once, protections inchangées |
| supervision.py, private_router.py, supervision.example.json | Contrats et consultation pure hors ligne ; aucun transport ni action |
| PROTOCOL.md | Contrat de coordination réutilisable |

Le message 1947 et son journal confirmé ne doivent pas être rejoués, modifiés ou
supprimés. Aucun appel Telegram réel n'est autorisé. Les outils conservent leurs
gardes et ne sont pas raccordés automatiquement à la publication.

La cible future est la zone « Diffusion… » du canal privé « Codex » via
@Codex2PinceBot. Hébergement et contrôleur sont différés. Un message de canal ne
prouve pas l'identité humaine : toute commande sensible devra être authentifiée
et confirmée indépendamment. Le routeur actuel ne traite que des fixtures de
conversation directe ; il ne constitue pas l'admission future du canal.

Les rapports retirés restent dans l'historique Git. La branche de revue du
provisionnement est conservée pour traçabilité ; son autorisation d'exécution
est révoquée. Aucun script ou diagnostic Windows n'est à lancer.
