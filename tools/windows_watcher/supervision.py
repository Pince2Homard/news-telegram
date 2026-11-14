"""Contrat de supervision hors ligne ; aucun transport ni pouvoir d'exécution."""


def validate_config(config):
    keys = {"mode", "send_enabled", "notifications_channel_id",
            "arbitration_private_chat_id", "owner_user_id"}
    if not isinstance(config, dict) or set(config) != keys:
        raise ValueError("Configuration de supervision invalide")
    if config["mode"] != "simulation" or config["send_enabled"] is not False:
        raise ValueError("Seule la simulation sans envoi est disponible")
    channel = config["notifications_channel_id"]
    if channel is not None and (type(channel) is not int or not str(channel).startswith("-100")
                                or not -(2 ** 52) < channel < 0):
        raise ValueError("Identifiant numerique de canal attendu")
    for key in ("arbitration_private_chat_id", "owner_user_id"):
        value = config[key]
        if value is not None and (type(value) is not int or not 0 < value < 2 ** 52):
            raise ValueError("Identifiant prive numerique attendu")
    return dict(config)


def simulate_notification(config, new_events):
    config = validate_config(config)
    if type(new_events) is not int or new_events < 1:
        raise ValueError("Compteur d'evenements invalide")
    return {"simulation": True, "sent": False,
            "destination_configured": config["notifications_channel_id"] is not None,
            "new_events": new_events}


def simulate_private_identity(config, message):
    """Comparer des fixtures n'authentifie pas un message reel ni une decision."""
    config = validate_config(config)
    identity_matches = False
    if isinstance(message, dict):
        chat, sender = message.get("chat"), message.get("from")
        if isinstance(chat, dict) and isinstance(sender, dict):
            identity_matches = (
                config["owner_user_id"] is not None
                and config["arbitration_private_chat_id"] is not None
                and chat.get("type") == "private"
                and type(chat.get("id")) is int
                and chat["id"] == config["arbitration_private_chat_id"]
                and type(sender.get("id")) is int
                and sender["id"] == config["owner_user_id"]
                and sender.get("is_bot") is False
                and not any(key in message for key in (
                    "sender_chat", "forward_origin", "forward_from", "forward_from_chat", "via_bot",
                ))
            )
    return {"simulation": True, "identity_matches": identity_matches, "decision_authorized": False}
