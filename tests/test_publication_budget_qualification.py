"""Offline qualification of the one-attempt candidate, never enabled in production."""
import contextlib
import copy
import math
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from urllib3.util.connection import create_connection

from newsbot import persistence, publication, settings


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class PublicationBudgetQualificationTests(unittest.TestCase):
    def setUp(self):
        # Even an accidental unmocked HTTP call must fail before opening a socket.
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('socket.socket', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch('socket.getaddrinfo', side_effect=AssertionError('DNS forbidden')))
        self.stack.enter_context(patch('builtins.print'))
        self.clock = Clock()

    def state(self, size=2):
        return {'file': [{'text': str(i), 'link': f'https://example.invalid/{i}'}
                         for i in range(size)], 'vus': [], 'erreurs': []}

    def candidate(self, http):
        # Laboratory-only policy: production constants and source stay unchanged.
        with patch.object(publication, 'TELEGRAM_MAX_ATTEMPTS', 1):
            return publication.send_message(
                {'text': 'synthetic'}, 'synthetic', '42', http=http,
                clock=self.clock, monotonic=self.clock, sleep=self.clock.sleep,
            )

    def test_candidate_defers_all_valid_429_without_sleep_or_second_post(self):
        for delay in (1, 60, 61, 3600, None, True, 0, -1, '30'):
            with self.subTest(delay=delay):
                http = Mock()
                http.post.return_value.status_code = 429
                http.post.return_value.json.return_value = {
                    'ok': False, 'error_code': 429, 'parameters': {'retry_after': delay}}
                result = self.candidate(http)
                self.assertEqual(result.disposition, 'rejected')
                expected = delay if type(delay) is int and delay > 0 else 900
                self.assertEqual(result.retry_after, expected)
                self.assertEqual(result.attempts, 1)
                http.post.assert_called_once()
                self.assertEqual(self.clock(), 1000)

    def test_candidate_network_errors_never_retry(self):
        for error in (requests.Timeout(), requests.ConnectionError(), requests.ReadTimeout()):
            http = Mock()
            http.post.side_effect = error
            result = self.candidate(http)
            self.assertEqual(result.disposition, 'uncertain')
            http.post.assert_called_once()

    def test_candidate_invalid_success_and_incoherent_429_remain_uncertain(self):
        for status, payload in ((200, {}), (200, {'ok': True, 'result': {}}),
                                (429, {'ok': False, 'error_code': 500})):
            http = Mock()
            http.post.return_value.status_code = status
            http.post.return_value.json.return_value = payload
            self.assertEqual(self.candidate(http).disposition, 'uncertain')
            http.post.assert_called_once()

    def test_candidate_truncated_json_is_uncertain(self):
        http = Mock()
        http.post.return_value.status_code = 200
        http.post.return_value.json.side_effect = ValueError('synthetic truncation')
        self.assertEqual(self.candidate(http).disposition, 'uncertain')
        http.post.assert_called_once()

    def test_dns_is_not_bounded_by_connection_timeout(self):
        # Exercise urllib3's real connection helper, but simulate DNS without sockets.
        def dns(*args):
            self.clock.sleep(360)
            return []

        with patch.object(socket, 'getaddrinfo', side_effect=dns), self.assertRaises(OSError):
            create_connection(('fixture.invalid', 443), timeout=30)
        self.assertEqual(self.clock() - 1000, 360)

    def test_single_post_can_outlive_even_the_existing_reserve(self):
        http = Mock()

        def slow_post(*args, **kwargs):
            self.assertEqual(kwargs['timeout'], 30)
            # Model incremental reads each below the inactivity timeout.
            for _ in range(12):
                self.clock.sleep(29)
            return Mock(status_code=200, json=Mock(return_value={
                'ok': True, 'result': {'message_id': 7, 'date': 1348, 'chat': {'id': 42}}}))

        http.post.side_effect = slow_post
        result = self.candidate(http)
        self.assertTrue(result)
        self.assertGreater(result.duration_seconds, settings.MAX_TELEGRAM_SEND_BUDGET_SECONDS)
        http.post.assert_called_once()

    def test_retry_state_survives_restart_and_blocks_early_reemission(self):
        for disposition, delay, count in (('rejected', 2, 0), ('rejected', 900, 0),
                                           ('uncertain', 0, 1)):
            with self.subTest(disposition=disposition, delay=delay), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'fixture.json'
                state = self.state()
                sender = Mock(return_value=publication.TelegramSendResult(
                    False, disposition=disposition, retry_after=delay))
                publication.process_queue(state, {}, 'synthetic', '42', state_path=path,
                                          dry_run=False, sender=sender, clock=lambda: 1000)
                restored = persistence.load_state(path, allow_new=False)
                self.assertEqual([i['text'] for i in restored['file']], ['0', '1'])
                retry = restored['file'][0]['delivery_retry']
                self.assertEqual(retry['uncertain_count'], count)
                expected = delay if disposition == 'rejected' else 900
                self.assertEqual(retry['not_before'], 1000 + expected)
                publication.process_queue(restored, {}, 'synthetic', '42', state_path=path,
                                          dry_run=False, sender=sender,
                                          clock=lambda: 1000 + expected - 1)
                sender.assert_called_once()

    def test_budget_exhaustion_preserves_durable_fifo_without_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fixture.json'
            state = self.state()
            sender = Mock()
            publication.process_queue(state, {}, 'synthetic', '42', state_path=path,
                                      dry_run=False, sender=sender,
                                      time_limit=settings.MAX_TELEGRAM_SEND_BUDGET_SECONDS - 1)
            sender.assert_not_called()
            self.assertEqual(persistence.load_state(path)['file'], state['file'])

    def test_slow_initial_save_consumes_budget_before_any_send(self):
        sender = Mock()
        publication.process_queue(self.state(), {}, 'synthetic', '42', dry_run=False,
                                  sender=sender, clock=self.clock, monotonic=self.clock,
                                  save=lambda *args: self.clock.sleep(511), time_limit=720)
        sender.assert_not_called()

    def test_save_failure_after_confirmation_stops_before_next_article(self):
        state = self.state()
        durable = []

        def save(current, *args):
            if durable:
                raise OSError('synthetic disk failure')
            durable.append(copy.deepcopy(current))

        sender = Mock(return_value=publication.TelegramSendResult(True, message_id=7))
        with self.assertRaises(OSError):
            publication.process_queue(state, {}, 'synthetic', '42', dry_run=False,
                                      sender=sender, save=save)
        sender.assert_called_once()
        self.assertEqual(len(durable[0]['file']), 2)  # Duplicate risk on restart remains explicit.

    def replay(self, reserve):
        clock = Clock()
        calls = []
        state = self.state(186)

        def sender(*args):
            calls.append(clock())
            date = math.floor(clock())
            clock.sleep(19.494 / 31)  # Mean observed request time, not a tail-latency bound.
            return publication.TelegramSendResult(True, message_id=len(calls), telegram_date=date)

        with patch.object(publication, 'MAX_TELEGRAM_SEND_BUDGET_SECONDS', reserve):
            sent = publication.process_queue(
                state, {}, 'synthetic', '42', dry_run=False, sender=sender, save=Mock(),
                clock=clock, monotonic=clock, sleep=clock.sleep, interval_seconds=15,
                time_limit=720 - 16.209,
            )
        self.assertTrue(all(b - a >= 15 for a, b in zip(calls, calls[1:])))
        self.assertEqual([i['text'] for i in state['file']], [str(i) for i in range(sent, 186)])
        return sent, len(state['file'])

    def test_observed_run_replay_and_unqualified_counterfactual(self):
        self.assertEqual(self.replay(settings.MAX_TELEGRAM_SEND_BUDGET_SECONDS), (31, 155))
        with patch.object(publication, 'TELEGRAM_MAX_ATTEMPTS', 1):
            self.assertEqual(self.replay(settings.MAX_TELEGRAM_SEND_BUDGET_SECONDS), (31, 155))
        self.assertEqual(self.replay(90), (39, 147))  # Sensitivity only; never a safe configuration.
