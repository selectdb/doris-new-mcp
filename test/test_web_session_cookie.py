"""Unit tests for the Web UI session-cookie encoding / decoding helpers."""

import sys
import unittest
from pathlib import Path

# Allow ``from server import ...`` when running from the repo root.
_src = Path(__file__).resolve().parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from server import _decode_webui_session_cookie, _encode_webui_session_cookie  # noqa: E402


class WebUiSessionCookieRoundTripTests(unittest.TestCase):
    """Normal encode → decode round-trips across private IPv4 ranges."""

    def test_round_trip_10_prefix(self) -> None:
        cookie = _encode_webui_session_cookie("tok", "10.24.8.15")
        self.assertEqual(cookie, "tok.10.24.8.15")
        self.assertEqual(_decode_webui_session_cookie(cookie), ("tok", "10.24.8.15"))

    def test_round_trip_172_16_prefix(self) -> None:
        cookie = _encode_webui_session_cookie("abc123", "172.16.0.1")
        self.assertEqual(cookie, "abc123.172.16.0.1")
        self.assertEqual(_decode_webui_session_cookie(cookie), ("abc123", "172.16.0.1"))

    def test_round_trip_192_168_prefix(self) -> None:
        cookie = _encode_webui_session_cookie("Z9x_q", "192.168.1.1")
        self.assertEqual(cookie, "Z9x_q.192.168.1.1")
        self.assertEqual(_decode_webui_session_cookie(cookie), ("Z9x_q", "192.168.1.1"))

    def test_session_id_with_hyphens_and_underscores(self) -> None:
        # token_urlsafe produces A-Za-z0-9-_ so hyphens/underscores are normal.
        sid = "Kj8-_xQ2mZ9vR5w"
        cookie = _encode_webui_session_cookie(sid, "10.0.0.1")
        self.assertEqual(cookie, f"{sid}.10.0.0.1")
        self.assertEqual(_decode_webui_session_cookie(cookie), (sid, "10.0.0.1"))


class DecodeRejectsEdgeCases(unittest.TestCase):
    """``_decode_webui_session_cookie`` must return ``None`` for invalid inputs."""

    def test_none(self) -> None:
        self.assertIsNone(_decode_webui_session_cookie(None))

    def test_empty_string(self) -> None:
        self.assertIsNone(_decode_webui_session_cookie(""))

    def test_no_dot(self) -> None:
        self.assertIsNone(_decode_webui_session_cookie("session_token"))

    def test_leading_dot(self) -> None:
        self.assertIsNone(_decode_webui_session_cookie(".10.24.8.15"))

    def test_trailing_dot(self) -> None:
        self.assertIsNone(_decode_webui_session_cookie("session_token."))

    def test_text_in_ip_position(self) -> None:
        self.assertIsNone(_decode_webui_session_cookie("session_token.not-an-ip"))

    def test_public_ipv4(self) -> None:
        self.assertIsNone(_decode_webui_session_cookie("session_token.8.8.8.8"))

    def test_ipv6(self) -> None:
        self.assertIsNone(_decode_webui_session_cookie("session_token.fd00::1"))

    def test_ipv4_too_many_octets(self) -> None:
        self.assertIsNone(_decode_webui_session_cookie("tok.10.0.0.0.1"))

    def test_multiple_dots_in_session_like_value(self) -> None:
        # partition() splits on the *first* dot, so the "IP" becomes
        # "def.10.0.0.1" → invalid → None.
        self.assertIsNone(_decode_webui_session_cookie("abc.def.10.0.0.1"))

    def test_whitespace_only(self) -> None:
        self.assertIsNone(_decode_webui_session_cookie("   "))


class EncodeRejectsEdgeCases(unittest.TestCase):
    """``_encode_webui_session_cookie`` must raise ``ValueError`` for bad inputs."""

    def test_empty_session_id(self) -> None:
        with self.assertRaises(ValueError):
            _encode_webui_session_cookie("", "10.0.0.1")

    def test_dot_in_session_id(self) -> None:
        with self.assertRaises(ValueError):
            _encode_webui_session_cookie("session.token", "10.0.0.1")

    def test_public_ipv4(self) -> None:
        with self.assertRaises(ValueError):
            _encode_webui_session_cookie("tok", "8.8.8.8")

    def test_ipv6(self) -> None:
        with self.assertRaises(ValueError):
            _encode_webui_session_cookie("tok", "fd00::1")

    def test_ipv4_string_but_not_an_ip(self) -> None:
        with self.assertRaises(ValueError):
            _encode_webui_session_cookie("tok", "not.an.ip.address")


if __name__ == "__main__":
    unittest.main()
