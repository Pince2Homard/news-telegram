"""Diagnostic borne ; tous les credentials et appels sont factices."""
import contextlib
import copy
from http.client import IncompleteRead
import io
import json
import os
import socket
import ssl
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.windows_watcher import telegram_http as http, telegram_once as once


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.patch = patch.object(http.http.client, 'HTTPSConnection')
        self.factory = self.patch.start()
        self.addCleanup(self.patch.stop)
        self.connection = self.factory.return_value
        self.response = self.connection.getresponse.return_value
        self.response.status = 200
        self.response.read.return_value = b'{"ok":true,"result":{"id":12345}}'
        self.client = http.TelegramHTTP('fixture-not-a-real-token')

    def test_dns_tls_timeout_connection_and_interrupted_read_counters(self):
        cases = [(socket.gaierror('private'), 'DNS_ERROR', 'connect'),
                 (ssl.SSLCertVerificationError('private'), 'TLS_ERROR', 'connect'),
                 (TimeoutError('private'), 'NETWORK_TIMEOUT', 'connect'),
                 (ConnectionError('private'), 'NETWORK_CONNECTION', 'connect'),
                 (IncompleteRead(b'private'), 'RESPONSE_INTERRUPTED', 'body')]
        for error, code, phase in cases:
            with self.subTest(code=code):
                client = http.TelegramHTTP('fixture-not-a-real-token')
                self.connection.connect.side_effect = error if phase == 'connect' else None
                self.response.read.side_effect = error if phase == 'body' else None
                with self.assertRaises(http.TransportError) as caught: client('getMe', {})
                self.assertEqual(caught.exception.code, code)
                report = client.report()
                self.assertEqual((report['prepared'], report['attempted'], report['confirmed']), (1, 1, 0))
                self.assertEqual(report['http_responses'], int(phase == 'body'))
                self.assertEqual(report['steps'][0]['request_started'], phase == 'body')
                self.assertNotIn('private', json.dumps(report)+str(caught.exception))
                self.assertNotIn('fixture-not-a-real-token', json.dumps(report))

    def test_http_status_survives_invalid_or_sensitive_body_without_retry(self):
        for status in (400, 401, 403, 404, 429, 500):
            with self.subTest(status=status):
                self.response.status = status
                self.response.read.return_value = b'private-invalid-body'
                self.connection.request.reset_mock()
                client = http.TelegramHTTP('fixture-not-a-real-token')
                with self.assertRaises(once.Refused) as caught: once.result_of(client, 'getMe', {})
                self.assertEqual(caught.exception.code, 'HTTP_'+str(status) if status != 500 else 'HTTP_OTHER')
                self.connection.request.assert_called_once()
                report = client.report()
                self.assertEqual(report['http_responses'], 1)
                self.assertEqual(report['confirmed'], 0)
                self.assertNotIn('private', json.dumps(report))

    def test_redirect_and_invalid_json_have_distinct_codes(self):
        for status, raw, code in ((302,b'', 'HTTP_REDIRECT'), (200,b'invalid', 'API_INVALID')):
            self.response.status = status
            self.response.read.return_value = raw
            with self.assertRaises(http.TransportError) as caught: self.client('getMe', {})
            self.assertEqual(caught.exception.code, code)

    def test_identity_channel_and_permissions_are_distinct(self):
        base = {'getMe': {'id': 12345, 'is_bot': True, 'username': once.BOT},
                'getChat': {'id': once.CHANNEL, 'title': 'Codex', 'type': 'channel'},
                'getChatMember': {'user': {'id': 12345, 'is_bot': True},
                                  'status': 'administrator', 'can_post_messages': True}}
        for method, field, value, code, calls in (
                ('getMe','username','other','BOT_IDENTITY',1),
                ('getChat','id',999,'CHANNEL_IDENTITY',2),
                ('getChatMember','can_post_messages',False,'ADMIN_PERMISSION',3)):
            rows = copy.deepcopy(base)
            rows[method][field] = value
            seen = []
            def fake(name, payload):
                seen.append(name)
                return {'ok': True, 'result': rows[name]}
            with self.assertRaises(once.Refused) as caught: once.verify(fake)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(len(seen), calls)

    def test_local_credential_failures_report_zero_network_without_reading_real_file(self):
        for code in ('CREDENTIAL_READ', 'CREDENTIAL_ACL', 'CREDENTIAL_FORMAT', 'CREDENTIAL_CHANGED'):
            with patch.object(sys, 'argv', ['telegram_once','--verify-real']), \
                    patch.dict(os.environ, {'LOCALAPPDATA': tempfile.gettempdir()}), \
                    patch.object(once, 'read_external_token', side_effect=once.Refused(code)), \
                    patch.object(http, 'TelegramHTTP') as factory, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(once.main(), 2)
            factory.assert_not_called()
            result = json.loads(output.getvalue())
            self.assertEqual(result['error_code'], code)
            self.assertEqual(result['diagnostics']['attempted'], 0)

    def test_prepared_but_not_attempted_when_tls_setup_fails(self):
        with patch.object(http.ssl, 'create_default_context', side_effect=ssl.SSLError('private')):
            with self.assertRaises(http.TransportError): self.client('getMe', {})
        report = self.client.report()
        self.assertEqual((report['prepared'], report['attempted'], report['confirmed']), (1,0,0))

    def test_confirmed_means_valid_api_response_not_identity_approval(self):
        self.client('getMe', {})
        self.assertEqual(self.client.report()['confirmed'], 1)
        with self.assertRaises(once.Refused): once.verify(lambda *_: {'ok': True, 'result': {'id':12345}})

    def test_malformed_send_failure_remains_ambiguous_without_another_attempt(self):
        client = http.TelegramHTTP('fixture-not-a-real-token', allow_send=True)
        self.response.status = 500
        self.response.read.return_value = b'invalid-sensitive-fixture'
        with tempfile.TemporaryDirectory() as folder, patch.object(once, 'verify'):
            result = once.send_once(client, folder, once.approval_digest())
            self.assertEqual(result['status'], 'uncertain')
            self.assertTrue(once.send_once(client, folder, once.approval_digest())['already_attempted'])
        self.connection.request.assert_called_once()
