"""Offline tests for table-scoped grants derived from semantic YAML files."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from server import _grant_select_on_semantic_tables  # noqa: E402
from store.bootstrap import collect_physical_tables  # noqa: E402


class SemanticGrantTests(unittest.TestCase):
    def test_collects_all_supported_physical_table_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            models_dir = Path(tmp)
            (models_dir / "models.yaml").write_text(
                """
semantic_model:
  name: orders
  db_table: dw.orders
---
semantic_model:
  name: users
  node_relation:
    database: internal
    schema_name: dw
    alias: users
---
time_config:
  calendar:
    - table: dw.dim_date
---
project_configuration:
  time_spines:
    - node_relation:
        schema_name: shared
        alias: calendar
""",
                encoding="utf-8",
            )

            self.assertEqual(
                collect_physical_tables(models_dir),
                {"dw.orders", "internal.dw.users", "dw.dim_date", "shared.calendar"},
            )

    def test_grants_each_semantic_table_without_global_wildcard(self) -> None:
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        _grant_select_on_semantic_tables(conn, {"dw.users", "dw.orders"})

        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertEqual(
            statements,
            [
                "GRANT SELECT_PRIV ON `dw`.`orders` TO '%'",
                "GRANT SELECT_PRIV ON `dw`.`users` TO '%'",
            ],
        )
        self.assertTrue(all("*.*" not in statement for statement in statements))

    def test_rejects_invalid_table_before_granting(self) -> None:
        conn = MagicMock()
        with self.assertRaisesRegex(ValueError, "Invalid semantic table name"):
            _grant_select_on_semantic_tables(conn, {"dw.orders", "dw.orders; DROP TABLE dw.users"})
        conn.cursor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
