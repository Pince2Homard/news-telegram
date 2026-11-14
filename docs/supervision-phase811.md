# Phase 8.1.1 — HTTP et ACL Windows, sans activation

Cette phase remplace les blocages de préparation de la Phase 8.1. Aucun vrai
credential n'est demandé ou lu, aucun appel Telegram n'est exécuté pendant le
développement. L'installation Windows et les workflows restent inchangés.

## Adaptateur ponctuel

`telegram_http.py` utilise exclusivement HTTPSConnection vers api.telegram.org,
la validation des certificats et du nom d'hôte, TLS 1.2 minimum, un timeout de
connexion de 5 secondes et de lecture de 15 secondes. Ces délais ne sont pas un
budget total : une lecture lente peut durer plus longtemps. Il ne lit aucune
configuration de proxy, ne redirige et ne réessaie jamais. La réponse est bornée
à 64 Kio ; réponses et connexions sont fermées même en erreur. Les traces HTTP
de débogage sont désactivées. Les exceptions affichées sont génériques, jamais
les URL, réponses brutes ou descriptions Telegram.

Seuls getMe, getChat, getChatMember et sendMessage sont accessibles. Les trois
premiers utilisent GET ; sendMessage utilise POST. getChatMember est lié à
l'identifiant obtenu par getMe. Le canal et le texte sont imposés. Par défaut,
l'adaptateur interdit sendMessage ; même autorisé, il bloque une seconde
tentative sur la même instance. Le journal durable de Phase 8.1 reste la
protection entre processus et redémarrages. Toute réponse ambiguë reste sans
renvoi ; un 429 conserve le délai sans programmer de nouvelle tentative.

## ACL et credential

Emplacement fixe futur :
`%LOCALAPPDATA%\NewsTelegramWatcher\credentials\supervision-token.txt`.
Journal fixe : `%LOCALAPPDATA%\NewsTelegramWatcher\telegram-once`.
Ni token en arguments, variable de production, fichier Git, sortie ou journal.
La fonction de lecture refuse tout fichier dans le dépôt et recontrôle l'ACL
après lecture, ainsi que l'identité/taille/date du fichier. Le token reste en
mémoire le temps de la commande ; aucun mécanisme ne protège contre un
administrateur local, un débogueur ou une instrumentation système hostile.

`credential_acl.py` appelle explicitement le PowerShell Windows système et son
module Security, sans profil ni shell intermédiaire. Les chemins passent par
stdin JSON et Get-Acl -LiteralPath. Aucun contenu du credential n'est demandé
par ce sous-processus. Les SID acceptés sont le compte courant, SYSTEM et les
Administrateurs. Le propriétaire de chaque élément doit être parmi eux. Une
DACL nulle, reparse point, chaîne incomplète ou inspection échouée bloque tout.

Le fichier ne doit accorder aucun accès direct ou hérité à un autre SID.
Les parents, jusqu'à la racine du volume, ne doivent pas autoriser un tiers à
supprimer/remplacer le chemin ou changer ses attributs, DACL ou propriétaire.
Les droits de création de frères ne suffisent pas à remplacer le chemin
existant. Les ACE inherit-only sont évaluées à leur point d'application dans
la chaîne ; les règles Allow dangereuses sont refusées même si une règle Deny
pourrait les neutraliser. Ce contrôle conservateur n'est pas un calcul complet
des permissions effectives. Il n'altère jamais une ACL. En cas de refus, faire
examiner la configuration par le propriétaire ; ne pas assouplir les ACL des
répertoires système ni lancer la commande administrateur pour contourner le refus.

## Procédures futures — deux autorisations distinctes obligatoires

1. **Vérification réelle sans message**, uniquement après autorisation dédiée.
   Le propriétaire devra préparer localement le credential dans le fichier
   protégé, sans le communiquer dans un chat ou commentaire. Depuis le dépôt :

   ```powershell
   py -3.12 -m tools.windows_watcher.telegram_once --verify-real
   ```

   L'ACL est vérifiée avant lecture ; seuls les trois GET sont permis.
   Le bot doit être @Codex2PinceBot, le canal privé Codex doit être
   -1003833675690, et ce même bot doit avoir can_post_messages. Aucun bot n'est
   automatiquement ajouté ou promu. Une erreur arrête la commande sans retry.

2. **Envoi unique**, après nouvelle autorisation du SHA, texte et empreinte
   documentés en Phase 8.1, et seulement après la vérification précédente.

   ```powershell
   py -3.12 -m tools.windows_watcher.telegram_once --send-real --approve-digest f8a0814b2aeaca72c3a74bafe9d198ed1b6f560c6ed81cd8615f795964cfb4c9
   ```

   L'absence d'approbation exacte bloque avant lecture du credential. Le chemin
   du journal est fixe ; son ACL et ses fichiers existants sont contrôlés.
   L'ACL du journal et du verrou nouvellement créés est aussi vérifiée avant POST,
   pour refuser des permissions héritées dangereuses. Un refus laisse l'intention
   durable bloquante, sans envoi.
   Les identités sont revérifiées, puis l'intention est sauvegardée avant POST.
   Un échec après POST impose de consulter le journal et de signaler l'incertitude,
   jamais de supprimer le journal, changer de répertoire ou renvoyer le message.

Ces commandes sont préparées, **non exécutées**. Le mode --simulate reste sans
token ni réseau. Aucune tâche, service, webhook ou décision interactive ajouté.

Références : [Microsoft : sécurité des fichiers](https://learn.microsoft.com/en-us/windows/win32/fileio/file-security-and-access-rights),
[Get-Acl](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.security/get-acl).
