"""Bearer-token gate for the HTTP API.

Auth is optional: when ``NOVEL_AUTH_TOKEN`` is unset every request is allowed
(the local-development default). When set, ``/api/*`` requests must send an
``Authorization: Bearer <token>`` header.

Hardening:

* The token is compared in constant time over SHA-256 digests of both sides, so
  neither the token's length nor its prefix leaks through timing.
* Failed attempts from the same client are throttled with a bounded exponential
  penalty, slowing an online brute-force guesser. The delay only ever applies
  to the offending client's own request thread.
* Failure state lives only in process memory, expires after a short window, and
  is cleared on the next successful authentication.
"""
import hashlib
import hmac
import threading
import time

_PREFIX = "Bearer "

_WINDOW_SECONDS = 60.0
_THROTTLE_AFTER = 5  # allowed failures per window before a penalty delay kicks in
_BASE_DELAY_SECONDS = 0.25
_MAX_DELAY_SECONDS = 2.0

_lock = threading.Lock()
_failed_at = {}  # client address -> list of recent failure timestamps (monotonic)


def authorize(handler, config):
    """Return True when the request is permitted to reach an ``/api`` route."""
    if not config.auth_token:
        return True
    client = _client_key(handler)
    supplied = handler.headers.get("Authorization", "")
    when = time.monotonic()
    if _matches(supplied, config.auth_token):
        with _lock:
            _failed_at.pop(client, None)
        return True
    failures = _record_failure(client, when)
    if failures >= _THROTTLE_AFTER:
        # Each successive failure adds delay (doubling, bounded) so repeated
        # guesses stall instead of returning 401 as fast as the network allows.
        penalty = min(
            _BASE_DELAY_SECONDS * (2 ** (failures - _THROTTLE_AFTER)),
            _MAX_DELAY_SECONDS,
        )
        time.sleep(penalty)
    return False


def reset_throttle():
    """Clear all in-memory failure state (used by tests and after token rotation)."""
    with _lock:
        _failed_at.clear()


def _client_key(handler):
    client = getattr(handler, "client_address", None)
    return client[0] if client else ""


def _record_failure(client, when):
    with _lock:
        recent = [t for t in _failed_at.get(client, []) if when - t < _WINDOW_SECONDS]
        recent.append(when)
        _failed_at[client] = recent
        return len(recent)


def _matches(supplied, secret):
    """Constant-time bearer check that never discloses the token length."""
    if not supplied.startswith(_PREFIX):
        return False
    token = supplied[len(_PREFIX):]
    if not token:
        return False
    left = hashlib.sha256(token.encode("utf-8")).digest()
    right = hashlib.sha256(secret.encode("utf-8")).digest()
    return hmac.compare_digest(left, right)
