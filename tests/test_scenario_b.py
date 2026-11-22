"""Scenario B: synthetic HTTP/clocks and local Git fixtures only."""
import contextlib
import copy
import json
import math
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import poster
from newsbot import persistence, publication, settings
from tools.persistence_health import synchronize


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class ScenarioBTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch('builtins.print'))
        stack.enter_context(patch('socket.socket', side_effect=AssertionError('network forbidden')))
        stack.enter_context(patch('socket.getaddrinfo', side_effect=AssertionError('DNS forbidden')))
        self.clock = Clock()

    def state(self, count):
        return {'file': [{'text': str(i), 'link': f'https://example.invalid/{i}'}
                         for i in range(count)], 'vus': [], 'erreurs': []}

    def process(self, state, **kwargs):
        options = dict(dry_run=False, save=Mock(), sender=Mock(return_value=True),
                       clock=self.clock, monotonic=self.clock, sleep=self.clock.sleep,
                       interval_seconds=15, time_limit=900)
        options.update(kwargs)
        return publication.process_queue(state, {}, 'synthetic', '42', **options)

    def test_constants_and_only_intended_workflow_change(self):
        self.assertEqual((settings.RUN_BUDGET, settings.COLLECT_BUDGET,
                          settings.MAX_PAR_RUN, settings.MAX_TELEGRAM_SEND_BUDGET_SECONDS),
                         (900, 180, 50, 210))
        workflow = Path('.github/workflows/poster.yml').read_text(encoding='utf-8')
        for text in ('timeout-minutes: 18', 'cron: "2/5 * * * *"',
                     'group: poster-production', 'cancel-in-progress: false', 'queue: max'):
            self.assertIn(text, workflow)

    def test_checkpoint_uses_already_saved_state_and_never_more_than_25_confirmations(self):
        durable = []
        checkpoints = []
        state = self.state(51)
        metrics = {}

        def save(current, *args):
            durable[:] = copy.deepcopy(current['file'])

        def checkpoint():
            checkpoints.append(len(durable))
            self.assertEqual(durable, state['file'])

        self.assertEqual(self.process(state, save=save, remote_checkpoint=checkpoint,
                                      time_limit=2000, metrics=metrics), 50)
        self.assertEqual(checkpoints, [26, 1])
        self.assertEqual(metrics['max_confirmed_since_remote_checkpoint'], 25)
        self.assertEqual(metrics['confirmed_since_remote_checkpoint'], 0)
        self.assertEqual(state['file'][0]['text'], '50')

    def test_checkpoint_failure_stops_before_26th_attempt_and_sanitizes_error(self):
        for error in (TimeoutError('sensitive'), ValueError('sensitive conflict'),
                      subprocess.CalledProcessError(1, 'sensitive')):
            state = self.state(40)
            sender = Mock(return_value=True)
            with self.subTest(error=type(error).__name__), self.assertRaisesRegex(
                    RuntimeError, '^Checkpoint distant non vérifié : publication arrêtée$'):
                self.process(state, sender=sender, remote_checkpoint=Mock(side_effect=error))
            self.assertEqual(sender.call_count, 25)
            self.assertEqual(state['file'][0]['text'], '25')

    def test_due_checkpoint_has_its_own_60_seconds_beyond_network_reserve(self):
        checkpoint = Mock()
        sender = Mock(return_value=True)
        self.assertEqual(self.process(self.state(26), sender=sender, remote_checkpoint=checkpoint,
                                      time_limit=24 * 15 + 210 + 60 - .1), 24)
        checkpoint.assert_not_called()
        self.assertEqual(sender.call_count, 24)

    def test_oversleep_never_starts_a_post_with_exhausted_budget(self):
        sender = Mock(return_value=True)

        def oversleep(seconds):
            self.clock.sleep(seconds + 700)

        self.assertEqual(self.process(self.state(2), sender=sender, sleep=oversleep), 1)
        sender.assert_called_once()

    def test_save_failure_before_checkpoint_never_pushes_or_continues(self):
        saved = [0]

        def save(*args):
            saved[0] += 1
            if saved[0] == 26:
                raise OSError('synthetic disk failure')

        sender = Mock(return_value=True)
        checkpoint = Mock()
        with self.assertRaises(OSError):
            self.process(self.state(40), sender=sender, save=save, remote_checkpoint=checkpoint)
        self.assertEqual(sender.call_count, 25)
        checkpoint.assert_not_called()

    def test_interruption_before_and_after_remote_confirmation(self):
        for after in (False, True):
            remote = self.state(40)
            state = copy.deepcopy(remote)

            def interrupted_checkpoint():
                if after:
                    remote.clear()
                    remote.update(copy.deepcopy(state))
                raise KeyboardInterrupt('synthetic runner death')

            sender = Mock(return_value=True)
            with self.subTest(after=after), self.assertRaises(KeyboardInterrupt):
                self.process(state, sender=sender, remote_checkpoint=interrupted_checkpoint)
            self.assertEqual(sender.call_count, 25)
            self.assertEqual(remote['file'][0]['text'], '25' if after else '0')
            # Before verification, duplicates remain possible; no article silently lost.
            self.assertEqual(remote['file'][-1]['text'], '39')

    def test_uncertain_and_rejected_head_at_boundary_stop_without_next_article(self):
        for disposition in ('uncertain', 'rejected'):
            state = self.state(40)
            sender = Mock(side_effect=[True] * 24 + [publication.TelegramSendResult(
                False, disposition=disposition, retry_after=900)])
            checkpoint = Mock()
            self.assertEqual(self.process(state, sender=sender, remote_checkpoint=checkpoint), 24)
            self.assertEqual(sender.call_count, 25)
            self.assertEqual(state['file'][0]['text'], '24')
            self.assertEqual(state['file'][0]['delivery_retry']['uncertain_count'],
                             1 if disposition == 'uncertain' else 0)
            checkpoint.assert_not_called()

    def test_http_timeout_and_429_at_boundary_keep_transport_policy(self):
        for outcome in ('timeout', '429-short', '429-missing'):
            http = Mock()
            if outcome == 'timeout':
                http.post.side_effect = requests.Timeout('synthetic')
            else:
                payload = {'ok': False, 'error_code': 429}
                if outcome == '429-short':
                    payload['parameters'] = {'retry_after': 2}
                http.post.return_value.status_code = 429
                http.post.return_value.json.return_value = payload
            calls = []

            def sender(item, *args):
                calls.append(item['text'])
                if len(calls) <= 24:
                    return True
                return publication.send_message(item, 'synthetic', '42', http=http,
                                                clock=self.clock, monotonic=self.clock,
                                                sleep=self.clock.sleep)

            checkpoint = Mock()
            state = self.state(40)
            with self.subTest(outcome=outcome):
                self.assertEqual(self.process(state, sender=sender, remote_checkpoint=checkpoint), 24)
                self.assertEqual(len(calls), 25)
                self.assertEqual(http.post.call_count, 3 if outcome == '429-short' else 1)
                self.assertEqual(state['file'][0]['delivery_retry']['uncertain_count'],
                                 1 if outcome == 'timeout' else 0)
                checkpoint.assert_not_called()

    def test_overlong_checkpoint_stops_before_any_further_post(self):
        sender = Mock(return_value=True)
        self.process(self.state(40), sender=sender,
                     remote_checkpoint=lambda: self.clock.sleep(900))
        self.assertEqual(sender.call_count, 25)

    def test_checkpoint_time_counts_and_cadence_survives_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fixture.json'
            times = []

            def sender(*args):
                times.append(self.clock())
                return True

            def checkpoint():
                self.clock.sleep(60)

            metrics = {}
            self.process(self.state(50), save=persistence.save_state, state_path=path,
                         sender=sender, remote_checkpoint=checkpoint, metrics=metrics)
            self.assertEqual(metrics['remote_checkpoint_seconds'], 60)
            restored = persistence.load_state(path, allow_new=False)
            self.process(restored, save=persistence.save_state, state_path=path,
                         sender=sender, max_per_run=1, remote_checkpoint=checkpoint)
            self.assertTrue(all(b - a >= 15 for a, b in zip(times, times[1:])))

    def replay(self, budget, collection, checkpoint_cost, enabled=True):
        self.clock = Clock()
        self.clock.sleep(collection)
        metrics = {}

        def sender(*args):
            date = math.floor(self.clock())
            self.clock.sleep(19.494 / 31)
            return publication.TelegramSendResult(True, message_id=7, telegram_date=date)

        self.process(self.state(186), sender=sender, time_limit=budget - collection,
                     remote_checkpoint=(lambda: self.clock.sleep(checkpoint_cost)) if enabled else None,
                     metrics=metrics)
        return metrics

    def test_replay_performance_with_normal_slow_and_full_collection(self):
        for collection in (16.209, 47.246, 180):
            baseline = self.replay(720, collection, 0, enabled=False)
            for cost in (0, 2, 60):
                with self.subTest(collection=collection, checkpoint_cost=cost):
                    candidate = self.replay(900, collection, cost)
                    self.assertGreater(candidate['published'], baseline['published'])
                    self.assertLessEqual(candidate['published'], 50)
                    self.assertLessEqual(candidate['max_confirmed_since_remote_checkpoint'], 25)
                    self.assertLessEqual(candidate['publication_seconds'], 900 - collection - 210)

    def test_main_wires_real_checkpoint_and_subtracts_collection_time(self):
        for mode, expected in (('normal', 720), ('degraded', 660)):
            with patch.dict(os.environ, {'DRY_RUN': '0', 'ALLOW_TELEGRAM_SEND': '1',
                            'TELEGRAM_TOKEN': 'synthetic', 'TELEGRAM_CHAT': '42',
                            'PERSISTENCE_MODE': mode}, clear=True), \
                    patch.object(poster, 'load_feeds', return_value=[]), \
                    patch.object(poster, 'load_state', return_value=self.state(0)), \
                    patch.object(poster, 'collect_articles', return_value={}), \
                    patch.object(poster, 'save_state'), \
                    patch.object(poster, 'process_queue') as process, \
                    patch.object(poster.time, 'monotonic', side_effect=[0, 180, 180, 180]):
                poster.main()
            self.assertIs(process.call_args.kwargs['remote_checkpoint'], poster.checkpoint_remote)
            self.assertEqual(process.call_args.kwargs['time_limit'], expected)

    def test_wrapper_reuses_existing_synchronizer_with_bounded_git_commands(self):
        def synchronize_fixture(run):
            self.assertEqual(run(['git', 'fixture'], timeout=7), 'synthetic')

        with patch.object(poster, 'synchronize', side_effect=synchronize_fixture), \
                patch.object(poster.subprocess, 'run', return_value=Mock(stdout='synthetic')) as run:
            poster.checkpoint_remote()
        run.assert_called_once_with(['git', 'fixture'], capture_output=True, text=True,
                                    check=True, timeout=7)

    def test_dry_run_never_calls_remote_checkpoint(self):
        checkpoint = Mock()
        self.process(self.state(40), dry_run=True, remote_checkpoint=checkpoint)
        checkpoint.assert_not_called()

    def test_real_mode_rejects_untracked_state_target_before_collection(self):
        with patch.dict(os.environ, {'DRY_RUN': '0', 'ALLOW_TELEGRAM_SEND': '1',
                        'TELEGRAM_TOKEN': 'synthetic', 'TELEGRAM_CHAT': '42',
                        'STATE_FILE': 'untracked-fixture.json'}, clear=True), \
                patch.object(poster, 'load_feeds', return_value=[]), \
                patch.object(poster, 'load_state') as load, \
                patch.object(poster, 'collect_articles') as collect:
            with self.assertRaisesRegex(RuntimeError, 'checkpoint Git limité'):
                poster.main()
        load.assert_not_called()
        collect.assert_not_called()

    def test_local_git_checkpoint_then_runner_restart_does_not_replay_confirmed_prefix(self):
        # No production Git command: all repositories below are temporary fixtures.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare, work = root / 'remote.git', root / 'work'

            def command(args, cwd=root):
                return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                                      check=True, timeout=15).stdout

            command(['git', 'init', '--bare', '--initial-branch=main', str(bare)])
            command(['git', 'clone', str(bare), str(work)])
            path = work / 'state.json'
            state = self.state(40)
            persistence.save_state(state, {}, path)
            command(['git', 'add', 'state.json'], work)
            command(['git', '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
                     'commit', '-m', 'fixture'], work)
            command(['git', 'push', 'origin', 'main'], work)
            pushes = []

            def run(args, timeout):
                result = subprocess.run(args, cwd=work, capture_output=True, text=True,
                                        check=True, timeout=timeout)
                if 'push' in args:
                    pushes.append(args)
                    raise subprocess.CalledProcessError(1, 'synthetic lost acknowledgement')
                return result.stdout

            self.native_checkpoint_durations = []

            def checkpoint():
                started = time.perf_counter()
                with patch('tools.persistence_health.load_state',
                           side_effect=lambda *a, **k: persistence.load_state(path, allow_new=False)):
                    synchronize(run)
                self.native_checkpoint_durations.append(time.perf_counter() - started)

            self.process(state, state_path=path, save=persistence.save_state,
                         remote_checkpoint=checkpoint, max_per_run=25)
            restored = json.loads(command(['git', '--git-dir=' + str(bare),
                                          'show', 'main:state.json']))
            self.assertEqual(restored['file'][0]['text'], '25')
            self.assertEqual(len(pushes), 1)
            sender = Mock(return_value=True)
            self.process(restored, sender=sender, max_per_run=1,
                         state_path=path, save=persistence.save_state)
            self.assertEqual(sender.call_args.args[0]['text'], '25')
            checkpoint()  # Reuse the same synchronizer again for the final state.
            final = json.loads(command(['git', '--git-dir=' + str(bare),
                                       'show', 'main:state.json']))
            self.assertEqual(final['file'][0]['text'], '26')
            self.assertEqual(len(pushes), 2)

    def test_real_synchronizer_conflict_stops_publication_at_checkpoint(self):
        answers = ['base', 'old', 'candidate', 'state.json', '', '', '', 'remote', 'other']
        git = Mock(side_effect=answers)

        def conflict():
            with patch('tools.persistence_health.load_state'):
                synchronize(git)

        sender = Mock(return_value=True)
        with self.assertRaises(RuntimeError):
            self.process(self.state(40), sender=sender, remote_checkpoint=conflict)
        self.assertEqual(sender.call_count, 25)
        self.assertFalse(any('push' in c.args[0] for c in git.call_args_list))
