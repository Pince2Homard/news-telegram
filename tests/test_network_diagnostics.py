import copy
import socket
import ssl
import unittest
from http.client import RemoteDisconnected
from unittest.mock import Mock, patch

import requests
from urllib3 import exceptions as transport_errors

from newsbot.network_diagnostics import network_diagnostic
from newsbot import publication


class NetworkDiagnosticsTests(unittest.TestCase):
    def test_nested_causes_are_classified_without_guessing_ambiguous_phases(self):
        cases = [
            (socket.gaierror("private-host"), "dns_failure", "dns"),
            (ssl.SSLCertVerificationError("private-cert"), "tls_certificate", "tls_verification"),
            (requests.exceptions.SSLError("private"), "tls_error", "unknown"),
            (ConnectionRefusedError("private"), "connection_failed", "connection_establishment"),
            (requests.exceptions.ConnectTimeout("private"), "connect_timeout", "connection_establishment"),
            (requests.exceptions.ReadTimeout("private"), "read_timeout", "unknown"),
            (transport_errors.ReadTimeoutError(None, "private", "private"), "read_timeout", "unknown"),
            (transport_errors.NewConnectionError(None, "private"), "connection_failed", "connection_establishment"),
            (RemoteDisconnected("private"), "connection_interrupted", "unknown"),
            (ConnectionResetError("private"), "connection_interrupted", "unknown"),
            (requests.exceptions.ChunkedEncodingError("private"), "response_interrupted", "response_body_read"),
            (requests.exceptions.ContentDecodingError("private"), "response_decoding", "response_body_decode"),
            (requests.exceptions.Timeout("private"), "timeout", "unknown"),
            (requests.exceptions.ConnectionError("private"), "connection_error", "unknown"),
        ]
        for cause, category, phase in cases:
            with self.subTest(category=category):
                wrapped = requests.ConnectionError(transport_errors.MaxRetryError(
                    None, "https://private/token", reason=cause,
                ))
                result = network_diagnostic(wrapped)
                self.assertEqual((result["category"], result["phase"]), (category, phase))
                self.assertNotIn("private", str(result))

    def test_dns_subclass_precedes_connect_timeout(self):
        cause = transport_errors.NameResolutionError("private-host", None, socket.gaierror())
        result = network_diagnostic(requests.ConnectionError(cause))
        self.assertEqual(result["category"], "dns_failure")

    def test_chain_is_cycle_safe_and_does_not_claim_absence_of_http_response(self):
        error = requests.ConnectionError("private")
        error.__cause__ = error
        result = network_diagnostic(error)
        self.assertFalse(result["http_response_attached"])
        self.assertIsNone(result["http_response_received"])
        response = requests.Response()
        response.status_code = 400  # Une réponse HTTP en erreur est falsey.
        error.response = response
        self.assertTrue(network_diagnostic(error)["http_response_received"])

    def test_diagnostics_do_not_log_payloads_or_retry_or_change_fifo(self):
        secret = "SENSITIVE_SENTINEL"
        state = {"file": [{"text": secret, "link": "https://example.org/a"}], "erreurs": []}
        before = copy.deepcopy(state["file"])
        error = requests.ConnectionError(secret)
        error.__cause__ = transport_errors.ProtocolError(secret, ConnectionResetError(secret))
        transport = Mock(post=Mock(side_effect=error))
        sleep, save = Mock(), Mock()

        def sender(item, token, chat):
            return publication.send_message(
                item, token, chat, http=transport, sleep=sleep,
                monotonic=lambda: 0, clock=lambda: 1000,
            )

        with patch("builtins.print") as logs:
            sent = publication.process_queue(
                state, {}, secret, secret, sender=sender, save=save,
                dry_run=False, monotonic=lambda: 0, clock=lambda: 1000,
                time_limit=400, sleep=sleep,
            )
        self.assertEqual(sent, 0)
        self.assertEqual(state["file"], before)
        transport.post.assert_called_once()
        sleep.assert_not_called()
        output = str(logs.call_args_list)
        self.assertNotIn(secret, output)
        self.assertNotIn("api.telegram.org", output)
        self.assertIn("connection_interrupted", output)
        self.assertIn("budget_avant_opération_s:", output)
        self.assertIn("400", output)

    def test_diagnostic_failure_still_returns_ambiguous_without_retry(self):
        transport = Mock(post=Mock(side_effect=requests.ConnectionError("private")))
        sleep = Mock()
        with patch.object(publication, "network_diagnostic", side_effect=RuntimeError("private")), \
                patch("builtins.print") as logs:
            result = publication.send_message(
                {"text": "private"}, "private", "private", http=transport,
                sleep=sleep, monotonic=lambda: 0, clock=lambda: 1000,
            )
        self.assertFalse(result)
        transport.post.assert_called_once()
        sleep.assert_not_called()
        self.assertIn("diagnostic_unavailable", str(logs.call_args_list))
        self.assertNotIn("private", str(logs.call_args_list))
