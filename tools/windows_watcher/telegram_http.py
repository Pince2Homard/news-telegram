"""HTTP direct Telegram : pas de proxy, retry, redirect, log ou secours."""
import http.client
import json
import ssl
import socket
from urllib.parse import urlencode

if __package__:
    from . import telegram_diagnostics as diagnostic
else:
    import telegram_diagnostics as diagnostic


class TransportError(diagnostic.DiagnosticError):
    pass


class TelegramHTTP:
    def __init__(self, token, allow_send=False):
        self.__token = token
        self.allow_send = allow_send
        self.attempted = False
        self.bot_id = None
        self.steps = []

    def __repr__(self):
        return '<TelegramHTTP credential=redacted>'

    def report(self):
        return {**{key: sum(step[key] for step in self.steps)
                   for key in ('prepared', 'attempted', 'confirmed', 'http_responses')},
                'steps': [dict(step) for step in self.steps]}

    def __call__(self, method, payload):
        # Import tardif : telegram_once n'importe cet adaptateur qu'en mode reel.
        if __package__:
            from . import telegram_once as once
        else:
            import telegram_once as once
        allowed = {"getMe", "getChat", "getChatMember", "sendMessage"}
        if method not in allowed or not isinstance(payload, dict):
            raise TransportError('ROUTE_REFUSED')
        if (method == 'getMe' and payload != {} or method == 'getChat' and payload != {'chat_id': once.CHANNEL}
                or method == 'getChatMember' and (self.bot_id is None or payload != {'chat_id': once.CHANNEL, 'user_id': self.bot_id})):
            raise TransportError('ROUTE_REFUSED')
        if method == 'sendMessage':
            if not self.allow_send or self.attempted or payload != {'chat_id': once.CHANNEL, 'text': once.MESSAGE}:
                raise TransportError('ROUTE_REFUSED')
            self.attempted = True
        step = {'method': method, 'phase': 'tls_setup', 'prepared': True, 'attempted': False,
                'request_started': False, 'http_responses': False, 'confirmed': False}
        self.steps.append(step)
        connection = None
        try:
            context = ssl.create_default_context()
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            connection = http.client.HTTPSConnection('api.telegram.org', timeout=5, context=context)
            connection.set_debuglevel(0)
            step.update(phase='connect', attempted=True)
            connection.connect()
            connection.sock.settimeout(15)
            path = '/bot' + self.__token + '/' + method
            sending = method == 'sendMessage'
            body = json.dumps(payload).encode() if sending else None
            if not sending and payload:
                path += '?' + urlencode(payload)
            step.update(phase='request', request_started=True)
            connection.request('POST' if sending else 'GET', path, body=body,
                               headers={'Content-Type': 'application/json', 'Connection': 'close'})
            step['phase'] = 'response_headers'
            response = connection.getresponse()
            step.update(http_responses=True, http_status=response.status)
            try:
                if 300 <= response.status < 400:
                    raise TransportError('HTTP_REDIRECT')
                if response.status >= 400 and not sending:
                    step['error_code'] = diagnostic.http_code(response.status)
                    clean = {'ok': False, 'error_code': response.status}
                    if response.status == 429:
                        try:
                            raw = response.read(65537)
                            data = json.loads(raw) if len(raw) <= 65536 else {}
                            parameters = data.get('parameters') if isinstance(data, dict) else None
                            delay = parameters.get('retry_after') if isinstance(parameters, dict) else None
                            if type(delay) is int and delay > 0:
                                clean['parameters'] = {'retry_after': delay}
                        except Exception:
                            pass  # Statut 429 etabli, corps facultatif non fiable.
                    step['phase'] = 'http_rejected'
                    return clean
                step['phase'] = 'response_body'
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
                    step.update(phase='api_confirmed', confirmed=True)
                    return {'ok': True, 'result': data['result']}
                if response.status >= 400:
                    step.update(phase='http_rejected', error_code=diagnostic.http_code(response.status))
                    clean = {'ok': False, 'error_code': response.status}
                    parameters = data.get('parameters')
                    if response.status == 429 and isinstance(parameters, dict):
                        delay = parameters.get('retry_after')
                        if type(delay) is int and delay > 0:
                            clean['parameters'] = {'retry_after': delay}
                    return clean
                raise ValueError()
            finally:
                response.close()
        except diagnostic.DiagnosticError as error:
            step['error_code'] = diagnostic.error_code(error)
            raise
        except Exception as error:
            if isinstance(error, socket.gaierror):
                code = 'DNS_ERROR'
            elif isinstance(error, ssl.SSLError):
                code = 'TLS_ERROR'
            elif isinstance(error, TimeoutError):
                code = 'NETWORK_TIMEOUT'
            elif isinstance(error, http.client.IncompleteRead):
                code = 'RESPONSE_INTERRUPTED'
            elif isinstance(error, (OSError, http.client.RemoteDisconnected)):
                code = 'NETWORK_CONNECTION'
            elif isinstance(error, (ValueError, TypeError, KeyError, http.client.HTTPException)):
                code = 'API_INVALID'
            else:
                code = 'NETWORK_UNCONFIRMED'
            step['error_code'] = code
            raise TransportError(code) from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
