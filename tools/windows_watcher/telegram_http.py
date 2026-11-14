"""HTTP direct Telegram : pas de proxy, retry, redirect, log ou secours."""
import http.client
import json
import ssl
from urllib.parse import urlencode


class TransportError(Exception):
    pass


class TelegramHTTP:
    def __init__(self, token, allow_send=False):
        self.__token = token
        self.allow_send = allow_send
        self.attempted = False
        self.bot_id = None

    def __repr__(self):
        return '<TelegramHTTP credential=redacted>'

    def __call__(self, method, payload):
        # Import tardif : telegram_once n'importe cet adaptateur qu'en mode reel.
        if __package__:
            from . import telegram_once as once
        else:
            import telegram_once as once
        allowed = {"getMe", "getChat", "getChatMember", "sendMessage"}
        if method not in allowed or not isinstance(payload, dict):
            raise TransportError('Route refusee')
        if (method == 'getMe' and payload != {} or method == 'getChat' and payload != {'chat_id': once.CHANNEL}
                or method == 'getChatMember' and (self.bot_id is None or payload != {'chat_id': once.CHANNEL, 'user_id': self.bot_id})):
            raise TransportError('Parametres refuses')
        if method == 'sendMessage':
            if not self.allow_send or self.attempted or payload != {'chat_id': once.CHANNEL, 'text': once.MESSAGE}:
                raise TransportError('Envoi refuse')
            self.attempted = True
        connection = None
        try:
            context = ssl.create_default_context()
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            connection = http.client.HTTPSConnection('api.telegram.org', timeout=5, context=context)
            connection.set_debuglevel(0)
            connection.connect()
            connection.sock.settimeout(15)
            path = '/bot' + self.__token + '/' + method
            sending = method == 'sendMessage'
            body = json.dumps(payload).encode() if sending else None
            if not sending and payload:
                path += '?' + urlencode(payload)
            connection.request('POST' if sending else 'GET', path, body=body,
                               headers={'Content-Type': 'application/json', 'Connection': 'close'})
            response = connection.getresponse()
            try:
                if 300 <= response.status < 400:
                    raise ValueError()
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise ValueError()
                data = json.loads(raw)
                if not isinstance(data, dict) or type(data.get('ok')) is not bool:
                    raise ValueError()
                if data['ok']:
                    if response.status != 200 or not isinstance(data.get('result'), dict):
                        raise ValueError()
                    if method == 'getMe':
                        self.bot_id = data['result'].get('id')
                    return {'ok': True, 'result': data['result']}
                if response.status < 400:
                    raise ValueError()
                clean = {'ok': False, 'error_code': response.status}
                parameters = data.get('parameters')
                if response.status == 429 and isinstance(parameters, dict):
                    delay = parameters.get('retry_after')
                    if type(delay) is int and delay > 0:
                        clean['parameters'] = {'retry_after': delay}
                return clean
            finally:
                response.close()
        except Exception:
            raise TransportError('Reponse Telegram non confirmee ; aucun retry') from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
