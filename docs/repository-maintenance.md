# Entretien ciblé du dépôt — 13 novembre 2026

Base : main `f58093c`. Aucun AGENTS.md trouvé dans le dépôt ou ses parents.
Avant ce lot : pas de Ruff, Dependabot, pip-audit ni configuration de couverture.
La PR Windows #10 et sa branche restent indépendantes ; aucun état de production
ou workflow de publication n'est modifié. Aucun mécanisme de chargement .env ajouté.

## Protections et contrôles

- .env et ses variantes ignorés ; .env.example et .env.*.example restent
  versionnables. Les caches Ruff et résultats locaux de couverture sont ignorés.
- Aucun fichier .env, clé privée ou fichier d'identifiants suivi repéré dans
  l'arbre courant. Recherche de signatures de clé privée/token Telegram sans
  affichage de valeurs : aucun candidat dans l'arbre suivi ou la branche Windows.
  Ce contrôle ciblé ne constitue pas un audit exhaustif de tout l'historique ou
  de tous les formats de secrets ; aucun historique n'est réécrit.
- requirements-dev.txt sépare Ruff 0.14.0, pip-audit 2.9.0 et coverage 7.10.7 de
  la production. Versions fixes, maintenables ensuite par PR Dependabot.
- Ruff : F401 et F821/F822/F823, plus analyse syntaxique intrinsèque. Aucun
  formatage, tri des imports, correctif automatique ou exclusion par fichier.
  Cinq imports de tests inutilisés retirés ; la constante de publication reste
  réexportée explicitement pour préserver l'interface du module.
- Dependabot : Python et Actions hebdomadaires, limites de 3 et 2 PR de mise à
  jour de versions respectivement. Ces limites ne plafonnent pas les PR de
  sécurité de GitHub. Aucune fusion automatique et aucune dépendance mise à jour
  dans ce lot ; après intégration, Dependabot pourra proposer ses propres PR.
- Qualité : permissions contents:read, déclenchements inchangés, pushes contenant
  seulement state.json toujours exclus, PR main toujours contrôlées. Tests avant
  pip-audit pour que les résultats fonctionnels restent visibles en cas d'avis.

## Résultats et limites

Mesure initiale unique : **110 tests réussis**, **88 % lignes et branches
combinées** (1 275 instructions, 462 branches), sur newsbot, poster et simulateur.
Pas de seuil ni service externe. Principaux points : publication 91 %, admission
85 %, persistance 76 %, dates 74 %. Les branches non exercées concernent notamment
la validation de certains états, les replis de parsing de date, l'épuisement des
réessais 429 et certains chemins de réexamen de quarantaine. Cela ne démontre
pas à lui seul un défaut et n'appelle pas une campagne de tests de pourcentage.

Un test paramétré a été ajouté : des enregistrements de reprise invalides dans
admission_buffer/date_quarantine doivent être refusés sans réécrire l'état.
Il couvre 14 sous-cas (structure, item, UID, empreinte et timestamps invalides).
Après modifications : **111 tests réussis**, Ruff réussi. La couverture n'a pas
été remesurée. Tests avec transports simulés ; aucun envoi Telegram ou changement
de l'état réel. Les chemins Windows de fsync de répertoire ne reproduisent pas
toutes les possibilités du runner Linux ; la CI apporte cette autre plateforme.

**Constat initial conservé : l'audit échouait sur requests 2.32.4**, identifié comme
PYSEC-2026-2275 / CVE-2026-25645 / GHSA-gc5v-m9x4-r6x2.
Selon l'[avis GitHub](https://github.com/advisories/GHSA-gc5v-m9x4-r6x2), il concerne
la réutilisation d'un fichier temporaire par extract_zipped_paths, avec attaquant
local capable de préparer ce fichier. L'usage standard n'est pas affecté ; aucun
appel direct à cet utilitaire trouvé dans le dépôt. Applicabilité non établie au
bot actuel, sans annuler le constat de version affectée.

**Correction intégrée séparément par la [PR #17](https://github.com/Pince2Homard/news-telegram/pull/17)** :
Requests 2.34.2, commit de fusion `361fa866401719dc60886d72606af566ba09b220`.
La [CI du correctif](https://github.com/Pince2Homard/news-telegram/actions/runs/43443726108)
et la [validation préalable de la composition avec #16](https://github.com/Pince2Homard/news-telegram/actions/runs/43443797197)
ont réussi : tests et audit sans vulnérabilité connue. La branche #16 a ensuite
été actualisée avec main, en conservant son état courant, pour exécuter sa propre
CI. Aucun ignore-vuln ni continue-on-error n'a été ajouté ; l'échec initial reste
documenté et n'a pas été masqué. L'audit inclut les dépendances transitives
résolues ; les résultats peuvent varier selon la date et la plateforme.

## Métadonnées proposées, non appliquées

Description actuelle vide ; aucun topic configuré lors du relevé.

Description proposée : « Bot Python de collecte RSS/Atom et de diffusion Telegram,
exécuté sur GitHub Actions avec reprise de file persistante. »

Topics proposés : telegram, rss, atom, python, github-actions.
La licence reste à décider par le propriétaire ; aucune licence ou badge ajouté.

Retour arrière : annuler ce lot de configuration, documentation, imports et test,
sans toucher au dernier state.json. Cadence effective, secrets, sources, capacités,
dates, stockage et architecture du simulateur restent hors périmètre.
