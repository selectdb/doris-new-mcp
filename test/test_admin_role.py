"""Unit tests for Doris admin-role based Semantic Web UI authorization."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from server import (  # noqa: E402
    _doris_user_has_admin_role,
    _roles_include_admin,
    _session_has_admin_access,
)


class TestAdminRoleParsing(unittest.TestCase):
    def test_admin_role_is_recognized(self):
        for roles in ("admin", "analyst, admin", "'admin'", "ADMIN"):
            with self.subTest(roles=roles):
                self.assertTrue(_roles_include_admin(roles))

    def test_similar_role_names_are_not_admin(self):
        for roles in (None, "", "analyst", "super_admin", "admin_readonly"):
            with self.subTest(roles=roles):
                self.assertFalse(_roles_include_admin(roles))


class TestDorisAdminRoleLookup(unittest.TestCase):
    @staticmethod
    def _connection(roles: str, role_column: str = "Roles") -> MagicMock:
        cursor = MagicMock()
        cursor.description = [
            ("UserIdentity",),
            (role_column,),
            ("GlobalPrivs",),
        ]
        cursor.fetchall.return_value = [("'user'@'%'", roles, "NULL")]
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        return connection

    def test_non_admin_username_with_admin_role_is_allowed(self):
        connection = self._connection("analyst,admin")
        self.assertTrue(_doris_user_has_admin_role(connection, "alice"))
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.execute.assert_called_once_with("SHOW GRANTS")

    def test_non_admin_role_is_read_only(self):
        self.assertFalse(
            _doris_user_has_admin_role(self._connection("analyst"), "alice")
        )

    def test_builtin_admin_remains_allowed_without_roles_column(self):
        connection = self._connection("", role_column="Comment")
        self.assertTrue(_doris_user_has_admin_role(connection, "admin"))

    def test_role_lookup_failure_does_not_elevate_regular_user(self):
        connection = MagicMock()
        connection.cursor.side_effect = RuntimeError("SHOW GRANTS unavailable")
        self.assertFalse(_doris_user_has_admin_role(connection, "alice"))


class TestAdminWebSession(unittest.TestCase):
    def test_role_derived_admin_flag_is_reused(self):
        self.assertTrue(
            _session_has_admin_access({"doris_user": "alice", "is_admin": True})
        )

    def test_admin_username_is_not_recomputed_from_session_name(self):
        self.assertFalse(
            _session_has_admin_access({"doris_user": "admin", "is_admin": False})
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
