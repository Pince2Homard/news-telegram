"""Regression tests for P1/P2, with actual workflow conditions and no network."""
import copy
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from newsbot import publication
from tools.persistence_health import recovery_mode


class WorkflowRouteTests(unittest.TestCase):
    def route(self, mode, probe_error=None, sync_error=None):
        lines = Path('.github/workflows/poster.yml').read_text(encoding='utf-8').splitlines()
        conditions = {}
        current = None
        for line in lines:
            line = line.strip()
            if line.startswith('- name:'):
                current = None
            if line.startswith('id:'):
                current = line.split(':', 1)[1].strip()
            if current and line.startswith('if:'):
                conditions[current] = line.split(':', 1)[1].strip()
        outcomes = {'probe': 'skipped', 'checkpoint': 'skipped', 'publish': 'skipped'}
        calls = []

        def allowed(step):
            expression = conditions[step].replace('steps.health.outputs.mode', repr(mode))
            for key, value in outcomes.items():
                expression = expression.replace(f'steps.{key}.outcome', repr(value))
            expression = expression.replace('&&', ' and ').replace('||', ' or ')
            return eval(expression, {'__builtins__': {}})

        for step, error in (('probe', probe_error), ('checkpoint', sync_error), ('publish', None)):
            if allowed(step):
                calls.append(step)
                outcomes[step] = 'failure' if error else 'success'
        return calls, outcomes

    def test_persistent_api_outage_with_verified_git_publishes_each_run(self):
        for failure in (requests.Timeout(), requests.HTTPError(), ValueError('invalid JSON')):
            for run_id in (11, 12, 13):
                with self.subTest(failure=type(failure).__name__, run=run_id):
                    mode = recovery_mode('fixture/repo', run_id, 1, Mock(side_effect=failure))
                    self.assertEqual(mode, 'degraded')
                    calls, outcomes = self.route(mode)
                    self.assertEqual(calls, ['probe', 'checkpoint', 'publish'])
                    self.assertEqual(outcomes['checkpoint'], 'success')

    def test_failed_local_or_remote_probe_never_authorizes_publication(self):
        for kwargs in ({'probe_error': OSError()}, {'sync_error': TimeoutError()},
                       {'sync_error': subprocess.CalledProcessError(1, 'fixture')}):
            calls, _ = self.route('degraded', **kwargs)
            self.assertNotIn('publish', calls)
        calls, _ = self.route('degraded', probe_error=OSError())
        self.assertNotIn('checkpoint', calls)

    def test_normal_and_known_recovery_routes_unchanged(self):
        self.assertEqual(self.route('normal')[0], ['publish'])
        self.assertEqual(self.route('recovery')[0], ['probe'])
        self.assertEqual(recovery_mode('fixture/repo', 1, 2, Mock()), 'recovery')
        get = Mock(return_value={'workflow_runs': [{'id': 2, 'status': 'in_progress'}]})
        self.assertEqual(recovery_mode('fixture/repo', 1, 1, get), 'recovery')

    def test_degraded_run_reserves_preflight_time(self):
        import poster
        for mode, expected in (('normal', 720), ('degraded', 660)):
            state = {'file': [], 'vus': []}
            with patch.dict('os.environ', {'DRY_RUN': '0', 'ALLOW_TELEGRAM_SEND': '1',
                            'TELEGRAM_TOKEN': 'synthetic', 'TELEGRAM_CHAT': '42',
                            'PERSISTENCE_MODE': mode}, clear=True), \
                    patch.object(poster, 'load_feeds', return_value=[]), \
                    patch.object(poster, 'load_state', return_value=state), \
                    patch.object(poster, 'collect_articles', return_value={}), \
                    patch.object(poster, 'save_state'), \
                    patch.object(poster, 'process_queue') as process, \
                    patch.object(poster.time, 'monotonic', return_value=100), \
                    patch('builtins.print'):
                poster.main()
                self.assertEqual(process.call_args.kwargs['time_limit'], expected)


class RateLimitTests(unittest.TestCase):
    def response(self, parameters):
        http = Mock()
        http.post.return_value.status_code = 429
        http.post.return_value.json.return_value = {
            'ok': False, 'error_code': 429, **parameters}
        return http

    def test_missing_or_invalid_delay_is_rejected_without_immediate_retry(self):
        cases = [{}, {'parameters': None}, {'parameters': {}}]
        cases += [{'parameters': {'retry_after': v}} for v in (None, True, '30', 0, -1)]
        for case in cases:
            with self.subTest(case=case):
                http = self.response(case)
                sleep = Mock()
                result = publication.send_message({'text': 'synthetic'}, 'fake', '42',
                                                  http=http, sleep=sleep)
                self.assertEqual(result.disposition, 'rejected')
                self.assertEqual(result.retry_after, 900)
                http.post.assert_called_once()
                sleep.assert_not_called()

    def test_repeated_known_rejections_preserve_queue_and_uncertainty_counter(self):
        state = {'file': [
            {'text': 'first', 'link': 'https://example.invalid/1', 'delivery_retry': {
                'uncertain_count': 2, 'last_attempt_at': 0, 'not_before': 0}},
            {'text': 'second', 'link': 'https://example.invalid/2'}], 'erreurs': []}
        original = copy.deepcopy(state['file'])
        http = self.response({})
        sender = lambda item, token, chat: publication.send_message(item, token, chat, http=http)
        with patch('builtins.print'):
            for now in (1000, 1900, 2800):
                publication.process_queue(state, {}, 'fake', '42', dry_run=False,
                                          sender=sender, save=Mock(), clock=lambda: now)
                self.assertEqual(state['file'][0]['delivery_retry']['uncertain_count'], 2)
                self.assertEqual(state['file'][0]['delivery_retry']['not_before'], now + 900)
                self.assertEqual(state['erreurs'], [])
        self.assertEqual([i['text'] for i in state['file']], [i['text'] for i in original])
        self.assertEqual(http.post.call_count, 3)

    def test_valid_delay_is_never_shortened(self):
        for wait in (1, 60, 61, 3600):
            with self.subTest(wait=wait):
                http = self.response({'parameters': {'retry_after': wait}})
                sleep = Mock()
                result = publication.send_message({'text': 'synthetic'}, 'fake', '42',
                                                  http=http, sleep=sleep)
                self.assertEqual(result.retry_after, wait)
                self.assertEqual(result.disposition, 'rejected')
                if wait <= 60:
                    self.assertEqual([c.args[0] for c in sleep.call_args_list], [wait, wait])
                else:
                    sleep.assert_not_called()
                    http.post.assert_called_once()

    def test_incoherent_429_envelope_remains_uncertain(self):
        http = self.response({})
        http.post.return_value.json.return_value['error_code'] = 500
        self.assertEqual(publication.send_message({'text': 'synthetic'}, 'fake', '42',
                                                 http=http).disposition, 'uncertain')

    def test_fifteen_second_cadence_is_preserved(self):
        state = {'file': [{'text': str(i), 'link': f'https://example.invalid/{i}'}
                          for i in range(2)], 'erreurs': []}
        now = [1000]
        calls = []

        def sender(*args):
            calls.append(now[0])
            return True

        def sleep(seconds):
            now[0] += seconds

        with patch('builtins.print'):
            publication.process_queue(state, {}, 'fake', '42', dry_run=False, sender=sender,
                                      save=Mock(), interval_seconds=15, sleep=sleep,
                                      clock=lambda: now[0], monotonic=lambda: now[0])
        self.assertEqual(calls, [1000, 1015])
