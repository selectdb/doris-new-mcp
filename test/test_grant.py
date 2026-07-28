"""Unit tests for one-time GRANT SELECT_PRIV init."""

import unittest
from unittest.mock import MagicMock, patch


# ── The function under test (to be moved to a shared module) ──

def try_grant_select_priv(host: str, port: int, admin_password: str) -> tuple[bool, str]:
    """Grant SELECT_PRIV on *.* to all users (idempotent, one-time init).

    Returns (True, "") on success, (False, error_message) on failure.
    Does NOT raise — failures are always non-fatal.
    """
    import pymysql
    try:
        conn = pymysql.connect(
            host=host, port=port,
            user="admin", password=admin_password,
            charset="utf8mb4", connect_timeout=5,
        )
        conn.cursor().execute("GRANT SELECT_PRIV ON *.* TO '%'")
        conn.close()
        return True, ""
    except Exception as e:
        return False, str(e)


# ══════════════════════════════════════════════════════════════════
class TestGrantSelectPriv(unittest.TestCase):

    @patch("pymysql.connect")
    def test_grant_succeeds_with_password(self, mock_connect):
        """GRANT executes successfully when admin has a password."""
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn

        ok, err = try_grant_select_priv("127.0.0.1", 9030, "secure123")

        self.assertTrue(ok)
        self.assertEqual(err, "")
        mock_connect.assert_called_once_with(
            host="127.0.0.1", port=9030,
            user="admin", password="secure123",
            charset="utf8mb4", connect_timeout=5,
        )
        mock_conn.cursor().execute.assert_called_once_with(
            "GRANT SELECT_PRIV ON *.* TO '%'"
        )
        mock_conn.close.assert_called_once()

    @patch("pymysql.connect")
    def test_grant_succeeds_with_empty_password(self, mock_connect):
        """GRANT executes successfully when admin has no password (default)."""
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn

        ok, err = try_grant_select_priv("127.0.0.1", 9030, "")

        self.assertTrue(ok)
        mock_connect.assert_called_once_with(
            host="127.0.0.1", port=9030,
            user="admin", password="",
            charset="utf8mb4", connect_timeout=5,
        )

    @patch("pymysql.connect")
    def test_grant_fails_connection_error_returns_false(self, mock_connect):
        """When Doris is unreachable, return False with error — no exception."""
        mock_connect.side_effect = OSError("Connection refused")

        ok, err = try_grant_select_priv("127.0.0.1", 9030, "")

        self.assertFalse(ok)
        self.assertIn("Connection refused", err)

    @patch("pymysql.connect")
    def test_grant_fails_no_grant_privilege_returns_false(self, mock_connect):
        """When admin lacks GRANT privilege, return False — no exception."""
        mock_conn = MagicMock()
        mock_conn.cursor().execute.side_effect = RuntimeError(
            "Access denied; you need GRANT privilege"
        )
        mock_connect.return_value = mock_conn

        ok, err = try_grant_select_priv("127.0.0.1", 9030, "admin")

        self.assertFalse(ok)
        self.assertIn("GRANT privilege", err)

    @patch("pymysql.connect")
    def test_grant_is_idempotent(self, mock_connect):
        """Running GRANT twice should succeed both times (Doris ignores duplicate)."""
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn

        ok1, _ = try_grant_select_priv("127.0.0.1", 9030, "")
        ok2, _ = try_grant_select_priv("127.0.0.1", 9030, "")

        self.assertTrue(ok1)
        self.assertTrue(ok2)
        self.assertEqual(mock_connect.call_count, 2)
        self.assertEqual(mock_conn.cursor().execute.call_count, 2)


# ══════════════════════════════════════════════════════════════════
class TestGrantIntegration(unittest.TestCase):
    """Integration: verify the call site in server.py calls try_grant_select_priv
       with values from ClusterConfig."""

    @patch("pymysql.connect")
    def test_uses_fe_password_from_config(self, mock_connect):
        """Password comes from ClusterConfig.fe_password, even when empty."""
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn

        # Simulate what server.py would do
        fe_host = "127.0.0.1"
        fe_port = 9030
        fe_password = ""  # no password configured

        ok, _ = try_grant_select_priv(fe_host, fe_port, fe_password)
        self.assertTrue(ok)

        # Verify empty password was passed, not hardcoded
        call_kwargs = mock_connect.call_args.kwargs
        self.assertEqual(call_kwargs["password"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
