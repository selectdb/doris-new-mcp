"""SQL validation using sqlglot AST parsing for Doris dialect."""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.dialects.doris import Doris


# sqlglot's Doris dialect does not currently model Doris full-text MATCH
# predicates.  Keep the extension local to validation so execute_query still
# sends the caller's original SQL to Doris unchanged.
_DORIS_MATCH_OPERATORS = frozenset(
    {
        "MATCH",
        "MATCH_ANY",
        "MATCH_ALL",
        "MATCH_PHRASE",
        "MATCH_PHRASE_PREFIX",
        "MATCH_PHRASE_EDGE",
        "MATCH_REGEXP",
    }
)


class _DorisMatch(exp.Expression, exp.Binary, exp.Predicate):
    """AST node used only to validate Doris MATCH predicates."""

    arg_types = {
        "this": True,
        "expression": True,
        "operator": True,
        "analyzer": False,
    }


class _DorisMatchParser(Doris.Parser):
    """Doris parser with support for the full-text infix operator family."""

    def _parse_range(self, this: exp.Expression | None = None) -> exp.Expression | None:
        this = super()._parse_range(this)

        while (
            this is not None
            and self._curr is not None
            and self._curr.text.upper() in _DORIS_MATCH_OPERATORS
        ):
            operator = self._curr.text.upper()
            self._advance()
            expression = self._parse_bitwise()
            if expression is None:
                self.raise_error(f"Expected expression after {operator}")
                return this

            analyzer = None
            if self._match_text_seq("USING", "ANALYZER"):
                analyzer = self._parse_id_var()
                if analyzer is None:
                    self.raise_error("Expected analyzer name after USING ANALYZER")
                    return this

            this = self.expression(
                _DorisMatch(
                    this=this,
                    expression=expression,
                    operator=operator,
                    analyzer=analyzer,
                )
            )

        return this


def _parse_doris(sql: str) -> list[exp.Expression | None]:
    dialect = sqlglot.Dialect.get_or_raise("doris")
    return _DorisMatchParser(dialect=dialect).parse(dialect.tokenize(sql), sql)

# Statement types allowed for execute_query (read-only)
_READONLY_TYPES = (
    exp.Select,
    exp.Union,
)

# Doris SHOW/DESCRIBE/EXPLAIN are parsed as Command by sqlglot
_READONLY_COMMAND_PREFIXES = (
    "SHOW",
    "DESCRIBE",
    "DESC",
    "EXPLAIN",
)


def validate_readonly(sql: str) -> tuple[bool, str]:
    """Validate that SQL is read-only (for execute_query).

    Returns (is_valid, error_message).
    """
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        return False, "Empty SQL statement"

    # Check for multiple statements (stacked queries)
    try:
        statements = _parse_doris(stripped)
    except sqlglot.errors.ParseError as e:
        # If sqlglot can't parse, fall back to prefix check for SHOW/DESC/EXPLAIN
        upper = stripped.upper().lstrip()
        for prefix in _READONLY_COMMAND_PREFIXES:
            if upper.startswith(prefix):
                return True, ""
        return False, f"SQL parse error: {e}"

    if len(statements) > 1:
        return False, "Multiple statements not allowed"

    if not statements or statements[0] is None:
        # sqlglot returned empty — try prefix-based check
        upper = stripped.upper().lstrip()
        for prefix in _READONLY_COMMAND_PREFIXES:
            if upper.startswith(prefix):
                return True, ""
        return False, "Unable to parse SQL statement"

    node = statements[0]

    # SELECT statements are allowed
    if isinstance(node, _READONLY_TYPES):
        return True, ""

    # Command nodes: SHOW, DESCRIBE, EXPLAIN etc.
    if isinstance(node, exp.Command):
        cmd = node.this
        if isinstance(cmd, str):
            upper_cmd = cmd.upper()
            for prefix in _READONLY_COMMAND_PREFIXES:
                if upper_cmd.startswith(prefix) or upper_cmd == prefix:
                    return True, ""

    # Also check raw text for SHOW/DESCRIBE/EXPLAIN that sqlglot may parse differently
    upper = stripped.upper().lstrip()
    for prefix in _READONLY_COMMAND_PREFIXES:
        if upper.startswith(prefix):
            return True, ""

    stmt_type = type(node).__name__
    return False, f"Statement type '{stmt_type}' not allowed in read-only mode"
