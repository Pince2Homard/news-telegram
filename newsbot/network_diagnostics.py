"""Classification technique bornée : aucun texte d'exception n'est exporté."""
import socket
import ssl
from http.client import IncompleteRead, RemoteDisconnected

import requests
from urllib3 import exceptions as transport_errors


def _exception_chain(error):
    pending = [error]
    seen = set()
    while pending and len(seen) < 16:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        cause = current.__cause__
        if cause is None and not current.__suppress_context__:
            cause = current.__context__
        pending.extend([cause, getattr(current, "reason", None)])
        pending.extend(arg for arg in current.args if isinstance(arg, BaseException))


def network_diagnostic(error):
    """Décrit les classes reconnues, jamais un succès/échec de livraison.

    Une rupture TCP ne localise pas la phase. Une SSLError générique ne prouve
    pas un échec de négociation. L'absence de response attachée ne prouve pas
    que Telegram n'a reçu aucune requête ou émis aucun en-tête.
    """
    chain = list(_exception_chain(error))
    dns = (socket.gaierror,)
    if hasattr(transport_errors, "NameResolutionError"):
        dns += (transport_errors.NameResolutionError,)
    rules = [
        (dns, "dns_failure", "dns"),
        ((ssl.SSLCertVerificationError,), "tls_certificate", "tls_verification"),
        ((requests.exceptions.ChunkedEncodingError, IncompleteRead,
          transport_errors.IncompleteRead), "response_interrupted", "response_body_read"),
        ((requests.exceptions.ContentDecodingError, transport_errors.DecodeError),
         "response_decoding", "response_body_decode"),
        ((requests.exceptions.ReadTimeout, transport_errors.ReadTimeoutError),
         "read_timeout", "unknown"),
        # NewConnectionError hérite de ConnectTimeoutError : tester avant lui.
        ((transport_errors.NewConnectionError, ConnectionRefusedError),
         "connection_failed", "connection_establishment"),
        ((requests.exceptions.ConnectTimeout, transport_errors.ConnectTimeoutError),
         "connect_timeout", "connection_establishment"),
        ((requests.exceptions.SSLError, transport_errors.SSLError, ssl.SSLError),
         "tls_error", "unknown"),
        ((requests.exceptions.ProxyError, transport_errors.ProxyError),
         "proxy_error", "unknown"),
        ((ConnectionResetError, BrokenPipeError, RemoteDisconnected,
          transport_errors.ProtocolError), "connection_interrupted", "unknown"),
        ((requests.exceptions.Timeout, TimeoutError), "timeout", "unknown"),
        ((requests.exceptions.ConnectionError,), "connection_error", "unknown"),
    ]
    category, phase = "request_error", "unknown"
    for classes, candidate, candidate_phase in rules:
        if any(isinstance(node, classes) for node in chain):
            category, phase = candidate, candidate_phase
            break
    attached = any(getattr(node, "response", None) is not None for node in chain)
    response_read = category in ("response_interrupted", "response_decoding")
    return {
        "category": category,
        "phase": phase,
        "http_response_attached": attached,
        "http_response_received": True if attached or response_read else None,
    }
