"""Tests for the e-mail allowlist."""

import os
import unittest
from unittest import mock

from fastmcp.exceptions import AuthorizationError

import ads_mcp.access_control as ac


def _token(email):
    t = mock.Mock()
    t.claims = {"email": email}
    return t


class AccessControlTest(unittest.TestCase):
    def test_no_token_allows(self):
        with mock.patch.object(ac, "get_access_token", return_value=None):
            ac.check_access()

    def test_default_owner_allowed(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ADS_ALLOWED_EMAILS", None)
            with mock.patch.object(ac, "get_access_token", return_value=_token("WHABALO@gmail.com")):
                ac.check_access()

    def test_stranger_denied(self):
        os.environ.pop("ADS_ALLOWED_EMAILS", None)
        with mock.patch.object(ac, "get_access_token", return_value=_token("x@example.com")):
            with self.assertRaises(AuthorizationError):
                ac.check_access()

    def test_missing_email_denied(self):
        t = mock.Mock()
        t.claims = {}
        with mock.patch.object(ac, "get_access_token", return_value=t):
            with self.assertRaises(AuthorizationError):
                ac.check_access()

    def test_env_list(self):
        with mock.patch.dict(os.environ, {"ADS_ALLOWED_EMAILS": "a@x.pl, b@y.pl"}):
            with mock.patch.object(ac, "get_access_token", return_value=_token("b@y.pl")):
                ac.check_access()
            with mock.patch.object(ac, "get_access_token", return_value=_token("whabalo@gmail.com")):
                with self.assertRaises(AuthorizationError):
                    ac.check_access()


if __name__ == "__main__":
    unittest.main()
