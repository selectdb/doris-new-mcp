"""Unit tests for core.sql_validator.validate_readonly (offline, pure function).

Covers:
  - Allowed: SELECT / WITH / UNION / SHOW / DESC / DESCRIBE / EXPLAIN
  - Allowed: Doris full-text MATCH predicates and BM25 score() queries
  - Allowed: future/unsupported Doris SELECT syntax through safe tokenization
  - Blocked: INSERT / UPDATE / DELETE / DROP / CREATE / GRANT / TRUNCATE / ALTER
  - Multiple statements (stacked queries) rejected
  - Comment-based bypass attempts rejected
  - Empty / whitespace-only input rejected
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from core.sql_validator import validate_readonly  # noqa: E402
from tools.query import execute_query  # noqa: E402


class TestValidateReadonlyAllowed(unittest.TestCase):
    """Read-only statements must be accepted."""

    def _assert_allowed(self, sql: str) -> None:
        ok, err = validate_readonly(sql)
        self.assertTrue(ok, f"Expected allowed, got error: {err} (sql={sql!r})")
        self.assertEqual(err, "")

    def test_simple_select(self):
        self._assert_allowed("SELECT 1")

    def test_select_with_where_and_trailing_semicolon(self):
        self._assert_allowed("SELECT * FROM dw.orders WHERE id = 1;")

    def test_with_cte(self):
        self._assert_allowed("WITH x AS (SELECT 1 AS a) SELECT * FROM x")

    def test_union(self):
        self._assert_allowed("select * from t union select * from u")

    def test_show(self):
        self._assert_allowed("SHOW DATABASES")
        self._assert_allowed("SHOW TABLES FROM mysql")

    def test_desc_and_describe(self):
        self._assert_allowed("DESC dw.orders")
        self._assert_allowed("DESCRIBE dw.orders")

    def test_explain(self):
        self._assert_allowed("EXPLAIN SELECT count(*) FROM dw.orders")

    def test_leading_comments_on_select(self):
        """Comments around a legitimate SELECT do not change the verdict."""
        self._assert_allowed("/* comment */ SELECT 1")
        self._assert_allowed("-- comment\nSELECT 1")

    def test_doris_full_text_match_operators(self):
        for operator in (
            "MATCH",
            "MATCH_ANY",
            "MATCH_ALL",
            "MATCH_PHRASE",
            "MATCH_PHRASE_PREFIX",
            "MATCH_PHRASE_EDGE",
            "MATCH_REGEXP",
        ):
            with self.subTest(operator=operator):
                self._assert_allowed(
                    f"SELECT * FROM lease_chunks WHERE content {operator} 'base rent'"
                )

    def test_match_in_select_cte_and_subquery(self):
        self._assert_allowed(
            "SELECT content MATCH_ANY 'rent' AS matched FROM lease_chunks LIMIT 1"
        )
        self._assert_allowed(
            "WITH matched AS ("
            "SELECT * FROM lease_chunks WHERE content MATCH_PHRASE 'base rent'"
            ") SELECT * FROM matched"
        )
        self._assert_allowed(
            "SELECT * FROM ("
            "SELECT * FROM lease_chunks WHERE content MATCH_ALL 'base rent'"
            ") AS matched"
        )

    def test_match_using_analyzer(self):
        self._assert_allowed(
            "SELECT * FROM lease_chunks "
            "WHERE content match_phrase 'base rent' USING ANALYZER english"
        )

    def test_ranked_bm25_query(self):
        self._assert_allowed(
            "SELECT content, score() AS bm25_score "
            "FROM lease_chunks "
            "WHERE content MATCH_PHRASE 'Maximum Repair Obligation' "
            "ORDER BY bm25_score DESC LIMIT 10"
        )

    def test_doris_tablet_and_tablesample_dialect(self):
        self._assert_allowed(
            "SELECT * FROM t1 TABLET(10001) "
            "TABLESAMPLE(1000 ROWS) REPEATABLE 2 LIMIT 1000"
        )

    def test_show_create_table_remains_allowed(self):
        self._assert_allowed("SHOW CREATE TABLE lease_chunks")

    def test_dialect_fallback_ignores_keywords_in_literals_and_identifiers(self):
        self._assert_allowed(
            "SELECT `drop`, 'DELETE FROM t' AS sample_text "
            "FROM t TABLET(10001) TABLESAMPLE(10 ROWS) REPEATABLE 2"
        )

    def test_dialect_fallback_allows_functions_named_like_write_statements(self):
        for expression in (
            "REPLACE(name, 'old', 'new')",
            "INSERT(name, 1, 2, 'new')",
        ):
            with self.subTest(expression=expression):
                self._assert_allowed(
                    f"SELECT {expression} FROM t TABLET(10001) "
                    "TABLESAMPLE(10 ROWS) REPEATABLE 2"
                )


class TestValidateReadonlyBlocked(unittest.TestCase):
    """Write / DDL / admin statements must be rejected."""

    def _assert_blocked(self, sql: str) -> None:
        ok, err = validate_readonly(sql)
        self.assertFalse(ok, f"Expected blocked: {sql!r}")
        self.assertNotEqual(err, "")

    def test_insert(self):
        self._assert_blocked("INSERT INTO dw.orders VALUES (1)")

    def test_update(self):
        self._assert_blocked("UPDATE dw.orders SET a = 1")

    def test_delete(self):
        self._assert_blocked("DELETE FROM dw.orders")

    def test_drop(self):
        self._assert_blocked("DROP TABLE dw.orders")

    def test_create(self):
        self._assert_blocked("CREATE TABLE t (id INT)")

    def test_grant(self):
        self._assert_blocked("GRANT SELECT_PRIV ON *.* TO u")

    def test_truncate(self):
        self._assert_blocked("TRUNCATE TABLE t")

    def test_alter(self):
        self._assert_blocked("ALTER TABLE t ADD COLUMN c INT")

    def test_use_and_set(self):
        self._assert_blocked("USE dw")
        self._assert_blocked("SET x = 1")

    def test_multiple_statements(self):
        self._assert_blocked("SELECT 1; SELECT 2")

    def test_stacked_write_after_select(self):
        self._assert_blocked("SELECT 1; DROP TABLE t")

    def test_comment_bypass_attempts(self):
        """Comments must not smuggle a write statement past the validator."""
        self._assert_blocked("/* x */ DROP TABLE t")
        self._assert_blocked("DROP TABLE t -- trailing comment")

    def test_match_does_not_allow_stacked_write(self):
        self._assert_blocked(
            "SELECT * FROM t WHERE content MATCH_ANY 'rent'; DROP TABLE t"
        )

    def test_incomplete_match_is_rejected(self):
        self._assert_blocked("SELECT * FROM t WHERE content MATCH_PHRASE")

    def test_doris_dialect_fallback_blocks_select_into_outfile(self):
        self._assert_blocked("SELECT * FROM t INTO OUTFILE 's3://bucket/result'")

    def test_unsupported_dialect_cannot_hide_stacked_write(self):
        self._assert_blocked(
            "SELECT * FROM t TABLET(10001) TABLESAMPLE(10 ROWS) REPEATABLE 2; "
            "DROP TABLE t"
        )

    def test_unsupported_dialect_cte_cannot_end_in_delete(self):
        self._assert_blocked(
            "WITH sampled AS ("
            "SELECT * FROM t TABLET(10001) TABLESAMPLE(10 ROWS) REPEATABLE 2"
            ") DELETE FROM t"
        )

    def test_explain_only_accepts_read_only_queries(self):
        for sql in (
            "EXPLAIN DELETE FROM t",
            "EXPLAIN UPDATE t SET value = 1",
            "EXPLAIN INSERT INTO t SELECT 1",
        ):
            with self.subTest(sql=sql):
                self._assert_blocked(sql)

    def test_nested_write_statement_is_rejected(self):
        for sql in (
            "WITH changed AS (DELETE FROM t) SELECT * FROM changed",
            "SELECT * FROM (UPDATE t SET value = 1) AS changed",
            "SELECT * FROM (WITH c AS (SELECT 1) DELETE FROM t) AS changed",
        ):
            with self.subTest(sql=sql):
                self._assert_blocked(sql)


class TestValidateReadonlyEmpty(unittest.TestCase):
    def test_empty_inputs_rejected(self):
        for sql in ("", "   ", ";"):
            with self.subTest(sql=sql):
                ok, err = validate_readonly(sql)
                self.assertFalse(ok)
                self.assertEqual(err, "Empty SQL statement")


class TestValidateReadonlyPrefixBoundary(unittest.TestCase):
    def test_explain_prefix_requires_a_keyword_boundary(self):
        ok, _ = validate_readonly("EXPLAINXXX weird")
        self.assertFalse(ok)

    def test_show_prefix_requires_a_keyword_boundary(self):
        ok, _ = validate_readonly("SHOWxxx nonsense")
        self.assertFalse(ok)


class TestExecuteQueryMatchForwarding(unittest.IsolatedAsyncioTestCase):
    async def test_original_bm25_sql_is_forwarded_unchanged(self):
        sql = (
            "SELECT content, score() AS bm25_score FROM lease_chunks "
            "WHERE content MATCH_PHRASE 'Maximum Repair Obligation' "
            "ORDER BY bm25_score DESC LIMIT 10"
        )

        class RecordingPool:
            received_sql = ""

            async def execute(self, received_sql, database=None, max_rows=None):
                self.received_sql = received_sql
                return [], ["content", "bm25_score"]

        pool = RecordingPool()
        result = json.loads(await execute_query(pool, sql))

        self.assertTrue(result["success"])
        self.assertEqual(pool.received_sql, sql)


if __name__ == "__main__":
    unittest.main(verbosity=2)
