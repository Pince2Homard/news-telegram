"""Essai ponctuel ; modes reels explicites, jamais appeles automatiquement."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

if __package__:
    from . import watcher
else:
    import watcher

CHANNEL = -1003833675690
BOT = "Codex2PinceBot"
TRIAL = "phase81-codex-single-notification-v1"
MESSAGE = ("[TEST SUPERVISION CODEX — notification unique]\n"
           "Vérification ponctuelle du bot @Codex2PinceBot dans le canal Codex.\n"
           "Aucune action sur le bot de production. Aucune décision demandée.\n"
           "Référence : phase81-codex-single-notification-v1")


class Refused(Exception):
    """Erreur technique volontairement sans contenu de reponse ou secret."""


def approval_digest():
    return hashlib.sha256(json.dumps([TRIAL, BOT, CHANNEL, MESSAGE],
        ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def read_external_token(path, repository_root):
    """Point futur, jamais appele par la CLI ; aucun repli sur la production."""
    try:
        path = Path(path)
        resolved = path.resolve(strict=True)
        if path.is_symlink() or resolved.is_relative_to(Path(repository_root).resolve()):
            raise ValueError()
        if not resolved.is_file() or resolved.stat().st_size > 256:
            raise ValueError()
        if os.name != "nt" and resolved.stat().st_mode & 0o077:
            raise ValueError()
        if os.name == "nt":
            if __package__:
                from . import credential_acl
            else:
                import credential_acl
            credential_acl.verify(path)
        before = resolved.stat()
        value = resolved.read_text(encoding="utf-8").strip()
        after = resolved.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError()
        if os.name == "nt":
            credential_acl.verify(path)
        if not value or any(char.isspace() for char in value):
            raise ValueError()
        return value
    except Exception:
        raise Refused("Fichier de credential refuse ; aucun contenu affiche.") from None


def result_of(transport, method, payload):
    try:
        response = transport(method, payload)
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise ValueError()
        result = response["result"]
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except Exception:
        raise Refused("Verification Telegram non confirmee ; aucun envoi.") from None


def verify(transport):
    me = result_of(transport, "getMe", {})
    bot_id = me.get("id")
    if type(bot_id) is not int or bot_id <= 0 or me.get("is_bot") is not True or me.get("username") != BOT:
        raise Refused("Identite du bot non concordante.")
    chat = result_of(transport, "getChat", {"chat_id": CHANNEL})
    if (type(chat.get("id")) is not int or chat["id"] != CHANNEL or chat.get("type") != "channel"
            or chat.get("title") != "Codex" or chat.get("username") or chat.get("active_usernames")):
        raise Refused("Canal prive attendu non concordant.")
    member = result_of(transport, "getChatMember", {"chat_id": CHANNEL, "user_id": bot_id})
    user = member.get("user")
    if (not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] != bot_id
            or user.get("is_bot") is not True or member.get("status") != "administrator"
            or member.get("can_post_messages") is not True):
        raise Refused("Droit de publication du bot non confirme.")


def send_once(transport, directory, approval, journal_guard=None):
    """Transport injecte ; aucun appel depuis watcher, tableau ou decision recue."""
    if approval != approval_digest():
        raise Refused("Approbation exacte du message necessaire.")
    directory = Path(directory)
    journal = directory / "telegram-once.json"
    with watcher.single_instance_lock(directory / "telegram-once.lock"):
        if journal.exists():
            try:
                saved = json.loads(journal.read_text(encoding="utf-8"))
                if (saved.get("trial") != TRIAL or saved.get("approval_digest") != approval
                        or saved.get("status") not in {"in_flight", "confirmed", "uncertain", "rejected"}
                        or saved.get("attempts") != 1):
                    raise ValueError()
            except (OSError, ValueError, AttributeError):
                raise Refused("Journal incompatible ; aucun nouvel envoi.") from None
            return {**saved, "already_attempted": True}
        verify(transport)
        saved = {"trial": TRIAL, "approval_digest": approval, "status": "in_flight", "attempts": 1}
        watcher.save_state(journal, saved)  # Durable AVANT l'unique tentative.
        if journal_guard is not None:
            journal_guard(journal)
            journal_guard(directory / 'telegram-once.lock')
        try:
            response = transport("sendMessage", {"chat_id": CHANNEL, "text": MESSAGE})
            if isinstance(response, dict) and response.get("ok") is False:
                saved["status"] = "rejected"
                if response.get("error_code") == 429:
                    delay = response.get("parameters", {}).get("retry_after")
                    if type(delay) is int and delay > 0:
                        saved["retry_after_seconds"] = delay
            else:
                result = response["result"]
                if (response.get("ok") is not True or type(result.get("message_id")) is not int
                        or result["message_id"] <= 0 or result.get("text") != MESSAGE
                        or type(result.get("chat", {}).get("id")) is not int
                        or result.get("chat", {}).get("id") != CHANNEL
                        or result["chat"].get("type") != "channel"):
                    raise ValueError()
                saved.update(status="confirmed", message_id=result["message_id"])
        except Exception:
            saved["status"] = "uncertain"  # Jamais de retry, meme apres redemarrage.
        watcher.save_state(journal, saved)
        return saved


def simulate():
    calls = []

    def fake(method, payload):
        calls.append(method)
        results = {"getMe": {"id": 12345, "is_bot": True, "username": BOT},
                   "getChat": {"id": CHANNEL, "type": "channel", "title": "Codex"},
                   "getChatMember": {"user": {"id": 12345, "is_bot": True},
                                     "status": "administrator", "can_post_messages": True},
                   "sendMessage": {"message_id": 1, "text": MESSAGE,
                                   "chat": {"id": CHANNEL, "type": "channel"}}}
        return {"ok": True, "result": results[method]}

    with tempfile.TemporaryDirectory(prefix="news-phase81-simulation-") as directory:
        first = send_once(fake, directory, approval_digest())
        second = send_once(fake, directory, approval_digest())
    return {"simulation": True, "calls": calls, "status": first["status"],
            "second_call_blocked": second["already_attempted"], "real_messages_sent": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--simulate", action="store_true")
    modes.add_argument("--verify-real", action="store_true")
    modes.add_argument("--send-real", action="store_true")
    parser.add_argument("--approve-digest")
    args = parser.parse_args()
    try:
        if args.simulate:
            print(json.dumps({"message": MESSAGE, "approval_digest": approval_digest(), **simulate()}, ensure_ascii=True))
            return 0
        if args.send_real and args.approve_digest != approval_digest():
            raise Refused('Approbation exacte requise avant lecture du credential')
        if __package__:
            from .telegram_http import TelegramHTTP
        else:
            from telegram_http import TelegramHTTP
        root = Path(os.environ['LOCALAPPDATA']) / 'NewsTelegramWatcher'
        directory = root / 'telegram-once'
        repository = Path(__file__).resolve().parents[2]
        if root.resolve().is_relative_to(repository):
            raise Refused('Emplacement durable refuse')
        token = read_external_token(root / 'credentials' / 'supervision-token.txt', repository)
        transport = TelegramHTTP(token, allow_send=args.send_real)
        if args.verify_real:
            verify(transport)
            print('Identite du bot, canal prive et permissions confirmes ; aucun message envoye.')
        else:
            directory.mkdir(parents=True, exist_ok=True)
            if os.name == 'nt':
                if __package__:
                    from . import credential_acl
                else:
                    import credential_acl
                credential_acl.verify(directory)
                for name in ('telegram-once.json', 'telegram-once.lock'):
                    if (directory / name).exists():
                        credential_acl.verify(directory / name)
            guard = credential_acl.verify if os.name == 'nt' else None
            print(json.dumps(send_once(transport, directory, args.approve_digest, journal_guard=guard), ensure_ascii=True))
        return 0
    except Exception:
        print('Operation refusee ou non confirmee ; aucun retry automatique.')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
