"""Phase 6: synthetic responses and in-memory checkpoints; no Telegram network."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import Mock, patch

import requests
from newsbot import publication


class TelegramRefusalDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch('socket.socket', side_effect=AssertionError('network forbidden')))
        stack.enter_context(patch('socket.getaddrinfo', side_effect=AssertionError('DNS forbidden')))

    def http(self, description, status=400, invalid=False):
        http = Mock()
        http.post.return_value.status_code = status
        http.post.return_value.json.return_value = {
            'ok': False, 'error_code': status, 'description': description}
        if invalid:
            http.post.return_value.json.side_effect = ValueError('sensitive truncated JSON')
        return http

    def records(self, text):
        return [json.loads(line.split(' | ', 1)[1]) for line in text.splitlines()
                if line.startswith('TELEGRAM_REFUSAL | ')]

    def send(self, http):
        return publication.send_message({'text': 'PRIVATE_ARTICLE'}, 'PRIVATE_TOKEN',
                                        'PRIVATE_CHAT', http=http, clock=lambda: 1000,
                                        monotonic=lambda: 0, sleep=Mock())

    def test_400_classification_and_normalized_reason_matrix(self):
        for description, reason, cls in (
            ('chat not found', 'chat_not_found', publication.GlobalTelegramError),
            ("can't parse entities", 'parse_entities', publication.PermanentTelegramError),
            ('message is too long', 'too_long', publication.PermanentTelegramError),
            ('message text is empty', 'empty', publication.PermanentTelegramError),
            ('unknown sensitive detail', 'unknown_400', publication.GlobalTelegramError),
        ):
            with self.subTest(reason=reason), contextlib.redirect_stdout(io.StringIO()) as out:
                http = self.http('Bad Request: ' + description)
                with self.assertRaises(cls):
                    self.send(http)
                record, = self.records(out.getvalue())
                self.assertEqual(record, {'http_status': 400, 'reason': reason,
                    'classification': 'global' if cls is publication.GlobalTelegramError else 'permanent',
                    'duration_seconds': 0, 'attempt': 1})
                http.post.assert_called_once()

    def test_invalid_or_incoherent_json_is_uncertain_without_inferred_cause(self):
        for payload in (None, [], {}, {'ok': False, 'error_code': 500,
                                      'description': 'chat not found'}):
            http = self.http('unused')
            http.post.return_value.json.return_value = payload
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(self.send(http).disposition, 'uncertain')
            record, = self.records(out.getvalue())
            self.assertEqual((record['reason'], record['classification']),
                             ('invalid_response', 'uncertain'))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.send(self.http('', invalid=True)).disposition, 'uncertain')
        self.assertEqual(self.records(out.getvalue())[0]['reason'], 'invalid_response')

    def test_no_sensitive_data_in_transport_logs(self):
        sensitive = ['PRIVATE_TOKEN', 'PRIVATE_CHAT', 'PRIVATE_ARTICLE',
                     'PRIVATE_RESPONSE', 'https://api.telegram.org/']
        with contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(publication.PermanentTelegramError):
                self.send(self.http("can't parse entities " + ' '.join(sensitive)))
        for value in sensitive:
            self.assertNotIn(value, out.getvalue())

    def test_other_categories_and_429_attempts_remain_available(self):
        for status, expected in ((401, 'unauthorized'), (403, 'forbidden'),
                                 (404, 'not_found'), (500, 'upstream_error'), (429, 'rate_limited')):
            http = self.http('private', status)
            if status == 429:
                http.post.return_value.json.return_value['parameters'] = {'retry_after': 2}
            with contextlib.redirect_stdout(io.StringIO()) as out:
                try:
                    result = self.send(http)
                except publication.GlobalTelegramError:
                    self.assertIn(status, (401, 403, 404))
            records = self.records(out.getvalue())
            self.assertTrue(all(r['reason'] == expected for r in records))
            self.assertEqual([r['attempt'] for r in records], [1, 2, 3] if status == 429 else [1])
            if status == 429:
                self.assertEqual((result.disposition, result.retry_after), ('rejected', 2))

    def state(self, size):
        return {'file': [{'text': str(i), 'link': f'https://example.invalid/{i}'}
                         for i in range(size)], 'vus': [], 'erreurs': []}

    def test_global_preserves_fifo_and_permanent_advances_only_its_article(self):
        for global_error in (True, False):
            http = self.http('chat not found' if global_error else "can't parse entities")
            state = self.state(2)
            saved = []
            calls = []
            def sender(item, token, chat):
                calls.append(item['text'])
                if len(calls) > 1:
                    return publication.TelegramSendResult(True, message_id=7)
                return publication.send_message(item, token, chat, http=http)
            with contextlib.redirect_stdout(io.StringIO()):
                publication.process_queue(state, {}, 'synthetic', '42', dry_run=False,
                    sender=sender, save=lambda s, *a:saved.append(copy.deepcopy(s)), metrics={})
            self.assertEqual(len(calls), 1 if global_error else 2)
            self.assertEqual(len(state['erreurs']), 0 if global_error else 1)
            self.assertEqual(len(state['file']), 2 if global_error else 0)
            self.assertEqual(saved[-1]['file'], state['file'])

    def test_permanent_errors_respect_attempt_cap_and_metrics(self):
        http = self.http("can't parse entities")
        state = self.state(128)
        metrics = {}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            publication.process_queue(state, {}, 'synthetic', '42', dry_run=False,
                sender=lambda i,t,c: publication.send_message(i,t,c,http=http),
                save=Mock(), metrics=metrics)
        self.assertEqual((http.post.call_count, len(state['erreurs']), len(state['file'])), (50, 50, 78))
        self.assertEqual((metrics['published'], metrics['telegram_attempts']), (0, 50))
        self.assertEqual(len(self.records(out.getvalue())), 50)

    def test_network_uncertainty_restart_does_not_retry_before_delay(self):
        state = self.state(2)
        http = Mock()
        http.post.side_effect = requests.Timeout('PRIVATE_TOKEN')
        saved = []
        sender = lambda i,t,c:publication.send_message(i,t,c,http=http)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            publication.process_queue(state, {}, 'synthetic', '42', dry_run=False,
                sender=sender, save=lambda s,*a:saved.append(copy.deepcopy(s)), clock=lambda:1000)
            restored = saved[-1]
            publication.process_queue(restored, {}, 'synthetic', '42', dry_run=False,
                sender=sender, save=Mock(), clock=lambda:1899)
        http.post.assert_called_once()
        self.assertEqual(restored['file'][0]['delivery_retry']['not_before'], 1900)
        self.assertEqual(len(restored['file']), 2)
        self.assertNotIn('PRIVATE_TOKEN', out.getvalue())

    def test_diagnostic_failure_does_not_change_global_decision(self):
        with patch('builtins.print', side_effect=OSError('synthetic output failure')):
            with self.assertRaises(publication.GlobalTelegramError):
                self.send(self.http('chat not found', 401))
