"""HTTP et ACL simules, aucun acces a Telegram ou credential reel."""
import copy
import io
import json
import os
from pathlib import Path, PureWindowsPath
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch
import contextlib
import sys

from tools.windows_watcher import credential_acl as acl, telegram_http as http, telegram_once as once


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.factory = patch.object(http.http.client, 'HTTPSConnection')
        self.make = self.factory.start()
        self.addCleanup(self.factory.stop)
        self.connection = self.make.return_value
        self.response = self.connection.getresponse.return_value
        self.response.status = 200
        self.response.read.return_value = json.dumps({'ok': True, 'result': {'id': 12345}}).encode()
        self.client = http.TelegramHTTP('fixture-not-a-real-token')

    def test_tls_timeouts_direct_get_and_resources(self):
        with patch.dict(os.environ, {'HTTPS_PROXY': 'untrusted', 'SSL_CERT_FILE': 'untrusted'}):
            self.client('getMe', {})
        args = self.make.call_args
        self.assertEqual(args.args, ('api.telegram.org',))
        self.assertEqual(args.kwargs['timeout'], 5)
        context = args.kwargs['context']
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.connection.sock.settimeout.assert_called_once_with(15)
        self.connection.set_debuglevel.assert_called_once_with(0)
        self.connection.request.assert_called_once()
        self.assertEqual(self.connection.request.call_args.args[0], 'GET')
        self.response.close.assert_called_once()
        self.connection.close.assert_called_once()
        self.assertNotIn('fixture-not-a-real-token', repr(self.client))

    def test_tls_timeout_interruption_sanitized_without_retry(self):
        for error in (ssl.SSLError('sensitive'), TimeoutError('sensitive'), ConnectionError('sensitive')):
            with self.subTest(error=type(error).__name__):
                self.connection.request.reset_mock()
                self.connection.request.side_effect = error
                with self.assertRaises(http.TransportError) as caught: self.client('getMe', {})
                self.assertNotIn('sensitive', str(caught.exception))
                self.connection.request.assert_called_once()

    def test_redirect_bad_json_oversize_and_http_errors(self):
        for status, body in ((302, b'ignored'), (200, b'invalid'), (200, b'x' * 65537),
                             (500, b'{"ok":true,"result":{}}')):
            self.response.status = status
            self.response.read.return_value = body
            self.connection.request.reset_mock()
            with self.assertRaises(http.TransportError): self.client('getMe', {})
            self.connection.request.assert_called_once()

    def test_send_is_denied_by_default_or_on_other_destination(self):
        for method, payload in (('sendMessage', {'chat_id': once.CHANNEL, 'text': once.MESSAGE}),
                                ('deleteMessage', {}), ('getChat', {'chat_id': 999})):
            with self.assertRaises(http.TransportError): self.client(method, payload)
        self.make.assert_not_called()

    def test_one_post_even_on_ambiguous_transport_and_429(self):
        self.client = http.TelegramHTTP('fixture-not-a-real-token', allow_send=True)
        self.response.status = 429
        self.response.read.return_value = b'{"ok":false,"description":"sensitive","parameters":{"retry_after":60}}'
        payload = {'chat_id': once.CHANNEL, 'text': once.MESSAGE}
        result = self.client('sendMessage', payload)
        self.assertEqual(result, {'ok': False, 'error_code': 429, 'parameters': {'retry_after': 60}})
        self.assertEqual(self.connection.request.call_args.args[0], 'POST')
        with self.assertRaises(http.TransportError): self.client('sendMessage', payload)
        self.connection.request.assert_called_once()

    def test_getchatmember_is_bound_to_getme_identity(self):
        with self.assertRaises(http.TransportError):
            self.client('getChatMember', {'chat_id': once.CHANNEL, 'user_id': 12345})
        self.client('getMe', {})
        with self.assertRaises(http.TransportError):
            self.client('getChatMember', {'chat_id': once.CHANNEL, 'user_id': 999})

    def test_real_verification_cli_with_mock_http_uses_only_three_gets(self):
        bodies = [{'id': 12345, 'is_bot': True, 'username': once.BOT},
                  {'id': once.CHANNEL, 'type': 'channel', 'title': 'Codex'},
                  {'user': {'id': 12345, 'is_bot': True}, 'status': 'administrator', 'can_post_messages': True}]
        self.response.read.side_effect = [json.dumps({'ok': True, 'result': b}).encode() for b in bodies]
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {'LOCALAPPDATA': folder}), \
                patch.object(once, 'read_external_token', return_value='fixture-not-a-real-token'), \
                patch.object(sys, 'argv', ['telegram_once', '--verify-real']), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(once.main(), 0)
        self.assertEqual([c.args[0] for c in self.connection.request.call_args_list], ['GET'] * 3)
        self.assertNotIn('fixture-not-a-real-token', output.getvalue())


class ACLTests(unittest.TestCase):
    def setUp(self):
        self.paths = [Path('credential').absolute(), Path('.').absolute()]
        self.user = 'S-1-5-21-1-2-3-1001'
        self.snapshot = {'user': self.user, 'rows': [
            {'path': str(p), 'owner': self.user, 'null_dacl': False, 'reparse': False,
             'rules': [{'sid': self.user, 'allow': True, 'rights': 2032127, 'inherit_only': False}]}
            for p in self.paths]}

    def test_safe_acl_and_readonly_parent_access(self):
        self.snapshot['rows'][1]['rules'].append(
            {'sid': 'S-1-1-0', 'allow': True, 'rights': 0x20089, 'inherit_only': False})
        acl.validate(self.snapshot, self.paths)

    def root_fixture(self):
        paths = [PureWindowsPath('C:/Users/Test/credentials/token.txt'),
                 PureWindowsPath('C:/Users/Test/credentials'), PureWindowsPath('C:/')]
        rows = [copy.deepcopy(self.snapshot['rows'][0]) for _ in paths]
        for row, path in zip(rows, paths): row['path'] = str(path)
        rows[-1]['owner'] = acl.TRUSTED_INSTALLER_OWNER
        rows[-1]['rules'] += [
            {'sid': 'S-1-5-11', 'allow': True, 'rights': 4, 'inherit_only': False},
            {'sid': 'S-1-5-11', 'allow': True, 'rights': -536805376, 'inherit_only': True},
            {'sid': 'S-1-5-32-545', 'allow': True, 'rights': 1179817, 'inherit_only': False}]
        return {'user': self.user, 'rows': rows}, paths

    def test_trustedinstaller_root_false_positive_reproduction(self):
        data, paths = self.root_fixture()
        acl.validate(data, paths)

    def test_trustedinstaller_never_exempt_on_credential_or_ordinary_directory(self):
        for index in (0, 1):
            data, paths = self.root_fixture()
            data['rows'][index]['owner'] = acl.TRUSTED_INSTALLER_OWNER
            with self.assertRaises(ValueError): acl.validate(data, paths)

    def test_root_owner_exception_is_exact_and_on_same_local_volume(self):
        for case in ('unknown', 'similar_sid', 'other_drive', 'unc', 'credential_itself'):
            data, paths = self.root_fixture()
            if case == 'unknown': data['rows'][-1]['owner'] = 'S-1-1-0'
            if case == 'similar_sid': data['rows'][-1]['owner'] += '-1'
            if case == 'other_drive':
                paths[-1] = PureWindowsPath('D:/')
                data['rows'][-1]['path'] = str(paths[-1])
            if case == 'unc':
                paths = [PureWindowsPath('//server/share/token'), PureWindowsPath('//server/share/')]
                data['rows'] = [data['rows'][0], data['rows'][-1]]
                for row, path in zip(data['rows'], paths): row['path'] = str(path)
            if case == 'credential_itself': paths, data['rows'] = paths[-1:], data['rows'][-1:]
            with self.subTest(case=case):
                with self.assertRaises(ValueError): acl.validate(data, paths)

    def test_root_exception_never_exempts_trustedinstaller_aces(self):
        data, paths = self.root_fixture()
        data['rows'][-1]['rules'].append({'sid': acl.TRUSTED_INSTALLER_OWNER,
            'allow': True, 'rights': 0x40000, 'inherit_only': False})
        with self.assertRaises(ValueError): acl.validate(data, paths)

    def test_root_exception_keeps_dangerous_rights_and_invalid_acls_blocked(self):
        for case in ('delete', 'delete_child', 'dacl', 'take_owner', 'null', 'reparse', 'missing', 'bad_ace', 'file_read'):
            data, paths = self.root_fixture()
            mask = {'delete': 0x10000, 'delete_child': 0x40, 'dacl': 0x40000, 'take_owner': 0x80000}
            if case in mask:
                data['rows'][-1]['rules'].append({'sid': 'S-1-1-0', 'allow': True,
                    'rights': mask[case], 'inherit_only': False})
            if case == 'null': data['rows'][-1]['null_dacl'] = True
            if case == 'reparse': data['rows'][-1]['reparse'] = True
            if case == 'missing': data['rows'].pop()
            if case == 'bad_ace': data['rows'][-1]['rules'][0]['allow'] = 'true'
            if case == 'file_read':
                data['rows'][0]['rules'].append({'sid': 'S-1-1-0', 'allow': True,
                    'rights': 1, 'inherit_only': False})
            with self.subTest(case=case):
                with self.assertRaises(ValueError): acl.validate(data, paths)

    def test_unsafe_file_or_parent_acl_owner_reparse_null_or_incomplete(self):
        for case in ('read_file', 'write_parent', 'delete_child', 'dacl', 'owner', 'null', 'link', 'missing'):
            with self.subTest(case=case):
                data = copy.deepcopy(self.snapshot)
                if case in ('read_file', 'write_parent', 'delete_child', 'dacl'):
                    index = 0 if case == 'read_file' else 1
                    mask = {'read_file': 1, 'write_parent': 0x100, 'delete_child': 0x40, 'dacl': 0x40000}[case]
                    data['rows'][index]['rules'].append({'sid': 'S-1-1-0', 'allow': True,
                        'rights': mask, 'inherit_only': False})
                if case == 'owner': data['rows'][0]['owner'] = 'S-1-1-0'
                if case == 'null': data['rows'][0]['null_dacl'] = True
                if case == 'link': data['rows'][1]['reparse'] = True
                if case == 'missing': data['rows'].pop()
                with self.assertRaises(ValueError): acl.validate(data, self.paths)

    def test_acl_collector_stdin_no_shell_and_failure_closed(self):
        with patch.dict(os.environ, {'SystemRoot': 'C:/Windows'}), patch.object(acl.subprocess, 'run') as run:
            run.return_value = Mock(returncode=0, stdout=json.dumps(self.snapshot))
            self.assertEqual(acl.inspect(self.paths), self.snapshot)
            self.assertNotIn('shell', run.call_args.kwargs)
            self.assertEqual(json.loads(run.call_args.kwargs['input']), [str(p) for p in self.paths])
            run.return_value.returncode = 2
            with self.assertRaises(ValueError): acl.inspect(self.paths)

    @unittest.skipUnless(os.name == 'nt', 'Windows fixture only')
    def test_windows_fixture_acl_collection_without_reading_any_credential(self):
        with tempfile.TemporaryDirectory() as folder:
            fixture = Path(folder) / 'fixture.txt'
            fixture.write_text('not-a-token', encoding='utf-8')
            snapshot = acl.inspect([fixture, fixture.parent])
            self.assertEqual(len(snapshot['rows']), 2)
            self.assertFalse(snapshot['rows'][0]['null_dacl'])

    def test_simulation_or_missing_send_approval_never_reads_credential(self):
        for args, expected in ((['--simulate'], 0), (['--send-real'], 2)):
            with patch.object(sys, 'argv', ['telegram_once', *args]), \
                    patch.object(once, 'read_external_token', side_effect=AssertionError('no token')), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(once.main(), expected)

    @unittest.skipUnless(os.name == 'nt', 'Windows fixture only')
    def test_token_read_checks_acl_before_and_after_fixture_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'fixture'
            path.write_text('fixture-not-a-real-token', encoding='utf-8')
            with patch.object(acl, 'verify') as check:
                self.assertEqual(once.read_external_token(path, Path(folder) / 'repo'), 'fixture-not-a-real-token')
            self.assertEqual(check.call_count, 2)
