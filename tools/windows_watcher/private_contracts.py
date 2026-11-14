"""Contrats purs de futures réponses privées ; aucune méthode API ni réception."""
import json
from html import unescape

from . import private_router


class ResponseRefused(ValueError):
    pass


def decode_envelope(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 65536:
        raise ResponseRefused("RESPONSE_SIZE")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(data, dict) or type(data.get("ok")) is not bool:
            raise ValueError()
        return data
    except (ValueError, UnicodeError, RecursionError):
        raise ResponseRefused("RESPONSE_INVALID") from None


def validate_updates(raw, config):
    """Un lot refusé n'autorise aucune progression du curseur.

    Seuls les messages directs authentifiés sont retournés. Les autres updates
    avec ID fiable sont rendus comme refus minimaux, sans contenu tiers.
    """
    data = decode_envelope(raw)
    values = data.get("result")
    if set(data) != {"ok", "result"} or data["ok"] is not True or not isinstance(values, list) or len(values) > 20:
        raise ResponseRefused("UPDATES_INVALID")
    result, last = [], -1
    for value in values:
        if not isinstance(value, dict) or type(value.get("update_id")) is not int or not last < value["update_id"] < 2**52:
            raise ResponseRefused("UPDATE_ID_INVALID")
        last = value["update_id"]
        message = value.get("message")
        if (set(value) != {"update_id", "message"} or not private_router.authenticate(message, config)
                or any(field in message for field in ("is_from_offline", "via_business_bot"))):
            result.append({"update_id": last, "disposition": "rejected"})
            continue
        if (type(message.get("message_id")) is not int or not 0 < message["message_id"] < 2**52
                or type(message.get("date")) is not int or not 0 <= message["date"] < 2**52
                or not isinstance(message.get("text"), str) or not 0 < len(message["text"]) <= 256):
            result.append({"update_id": last, "disposition": "rejected"})
            continue
        result.append({"update_id": last, "message": {"message_id": message["message_id"],
            "date": message["date"], "text": message["text"],
            "from": {"id": message["from"]["id"], "is_bot": False},
            "chat": {"id": message["chat"]["id"], "type": "private"}}})
    return result


def validate_message(raw, http_status, private_chat, expected_html):
    if (type(private_chat) is not int or not 0 < private_chat < 2**52
            or not isinstance(expected_html, str) or not 0 < len(expected_html) <= 2048):
        raise ResponseRefused("DESTINATION_INVALID")
    data = decode_envelope(raw)
    if (type(http_status) is int and 400 <= http_status < 500 and data["ok"] is False
            and type(data.get("error_code")) is int and data["error_code"] == http_status
            and set(data) <= {"ok", "error_code", "description", "parameters"}):
        parameters = data.get("parameters", {})
        if not isinstance(parameters, dict) or set(parameters) - {"retry_after"}:
            raise ResponseRefused("REJECTION_INVALID")
        delay = parameters.get("retry_after", 0)
        if type(delay) is not int or not 0 <= delay <= 86400 or (delay and http_status != 429):
            raise ResponseRefused("REJECTION_INVALID")
        return {"status": "rejected", "code": http_status, "retry_after": delay}
    result = data.get("result")
    if (http_status != 200 or type(http_status) is not int or data["ok"] is not True
            or set(data) != {"ok", "result"} or not isinstance(result, dict)
            or type(result.get("message_id")) is not int or not 0 < result["message_id"] < 2**52
            or not isinstance(result.get("chat"), dict)
            or type(result["chat"].get("id")) is not int or result["chat"]["id"] != private_chat
            or result["chat"].get("type") != "private" or result.get("text") != unescape(expected_html)
            or any(field in result for field in private_router.PROVENANCE_FIELDS)):
        raise ResponseRefused("MESSAGE_UNCERTAIN")
    return {"status": "confirmed", "message_id": result["message_id"]}
