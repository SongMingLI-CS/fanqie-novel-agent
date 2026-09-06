"""Hermetic unit tests for the bearer-token gate (novel_agent/auth.py)."""
import unittest

from novel_agent import auth


class _Config:
    def __init__(self, token):
        self.auth_token = token


class _Handler:
    def __init__(self, address="203.0.113.7", header=""):
        self.client_address = (address, 4321)
        self.headers = {"Authorization": header}


class AuthTests(unittest.TestCase):
    def setUp(self):
        auth.reset_throttle()
        self.addCleanup(auth.reset_throttle)

    # -- open mode ----------------------------------------------------------

    def test_no_token_configured_allows_everything(self):
        self.assertTrue(auth.authorize(_Handler(header=""), _Config("")))

    # -- matching -----------------------------------------------------------

    def test_exact_bearer_token_authorizes(self):
        handler = _Handler(header="Bearer s3cr3t-token")
        self.assertTrue(auth.authorize(handler, _Config("s3cr3t-token")))

    def test_wrong_token_is_rejected(self):
        handler = _Handler(header="Bearer wrong-token")
        self.assertFalse(auth.authorize(handler, _Config("s3cr3t-token")))

    def test_missing_or_malformed_authorization_is_rejected(self):
        cfg = _Config("s3cr3t-token")
        self.assertFalse(auth.authorize(_Handler(header=""), cfg))
        # Scheme is case-sensitive and must be exactly "Bearer <token>".
        self.assertFalse(auth.authorize(_Handler(header="bearer s3cr3t-token"), cfg))
        self.assertFalse(auth.authorize(_Handler(header="Basic s3cr3t-token"), cfg))
        # Token must be non-empty (nothing after the scheme).
        self.assertFalse(auth.authorize(_Handler(header="Bearer "), cfg))
        # Extra characters beyond the exact token are not accepted.
        self.assertFalse(auth.authorize(_Handler(header="Bearer s3cr3t-token "), cfg))
        self.assertFalse(auth.authorize(_Handler(header="Bearer s3cr3t-tokenX"), cfg))

    def test_rejection_is_independent_of_token_length(self):
        # Behavioural guarantee: both a much longer and a shorter wrong token
        # are rejected the same way (the compare path hides the true length).
        cfg = _Config("short")
        self.assertFalse(auth.authorize(_Handler(header="Bearer " + "x" * 5000), cfg))
        self.assertFalse(auth.authorize(_Handler(header="Bearer x"), cfg))

    # -- throttling ---------------------------------------------------------

    def test_repeated_failures_are_throttled_but_success_resets(self):
        cfg = _Config("s3cr3t-token")
        handler = _Handler(header="Bearer wrong")
        for _ in range(auth._THROTTLE_AFTER + 1):
            self.assertFalse(auth.authorize(handler, cfg))
        # The same client can still authenticate afterwards (state cleared).
        handler.headers["Authorization"] = "Bearer s3cr3t-token"
        self.assertTrue(auth.authorize(handler, cfg))
        # A different client is throttled independently of the first one.
        other = _Handler(address="203.0.113.9", header="Bearer nope")
        self.assertFalse(auth.authorize(other, cfg))
        # A fresh client has no residual penalty after reset_throttle().
        auth.reset_throttle()
        fresh = _Handler(address="203.0.113.9", header="Bearer nope")
        self.assertFalse(auth.authorize(fresh, cfg))


if __name__ == "__main__":
    unittest.main()
