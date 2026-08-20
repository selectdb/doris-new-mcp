"""SQL validation using sqlglot AST parsing for Doris dialect."""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.dialects.doris import Doris
from sqlglot.tokens import Token, TokenType


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

# Defense-in-depth for the tokenizer fallback.  The fallback is used only when
# sqlglot cannot model a Doris-specific query shape, and only SELECT queries
# are eligible.  These tokens must never be accepted in a CTE or trailing
# fragment even if Doris adds new grammar before sqlglot catches up.
_WRITE_KEYWORDS = frozenset(
    {
        "ALTER",
        "ANALYZE",
        "BACKUP",
        "CALL",
        "COPY",
        "CREATE",
        "DELETE",
        "DROP",
        "EXPORT",
        "GRANT",
        "INSERT",
        "INSTALL",
        "LOAD",
        "MERGE",
        "RECOVER",
        "RENAME",
        "REPAIR",
        "REPLACE",
        "RESTORE",
        "REVOKE",
        "SET",
        "TRUNCATE",
        "UNINSTALL",
        "UPDATE",
        "UPSERT",
        "USE",
    }
)

_QUOTED_OR_LITERAL_TOKENS = frozenset(
    {
        TokenType.BIT_STRING,
        TokenType.BYTE_STRING,
        TokenType.HEX_STRING,
        TokenType.IDENTIFIER,
        TokenType.NATIONAL_STRING,
        TokenType.RAW_STRING,
        TokenType.STRING,
        TokenType.UNICODE_STRING,
    }
)

# Statement types allowed for execute_query (read-only)
_READONLY_TYPES = (
    exp.Select,
    exp.Union,
)

# Exact Doris metadata/query-plan commands allowed in read-only mode.
_READONLY_COMMANDS = (
    "SHOW",
    "DESCRIBE",
    "DESC",
    "EXPLAIN",
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


def _token_word(token: Token) -> str:
    return token.text.upper()


def _top_level_statement(tokens: list[Token], start: int) -> str | None:
    """Find the main statement after WITH/EXPLAIN, ignoring nested CTEs."""
    depth = 0
    for token in tokens[start:]:
        if token.token_type == TokenType.L_PAREN:
            depth += 1
            continue
        if token.token_type == TokenType.R_PAREN:
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            word = _token_word(token)
            if word in _WRITE_KEYWORDS or word in {"SELECT", "WITH"}:
                return word
    return None


def _validate_tokenized_readonly(sql: str) -> tuple[bool, str]:
    """Safely admit Doris read-only syntax that sqlglot does not yet model.

    Tokenization still understands comments, quoted identifiers, string
    literals, and statement separators even when the expression grammar is
    newer than sqlglot's Doris dialect.  This lets execute_query support Doris
    SELECT extensions without falling back to an unsafe raw prefix check.
    """
    dialect = sqlglot.Dialect.get_or_raise("doris")
    try:
        tokens = dialect.tokenize(sql)
    except sqlglot.errors.TokenError as exc:
        return False, f"SQL tokenization error: {exc}"

    if not tokens:
        return False, "Unable to tokenize SQL statement"

    semicolons = [
        i for i, token in enumerate(tokens) if token.token_type == TokenType.SEMICOLON
    ]
    if semicolons:
        if len(semicolons) != 1 or semicolons[0] != len(tokens) - 1:
            return False, "Multiple statements not allowed"
        tokens = tokens[:-1]
    if not tokens:
        return False, "Empty SQL statement"

    first = _token_word(tokens[0])
    if first in _READONLY_COMMANDS:
        return True, ""

    if _token_word(tokens[-1]) in _DORIS_MATCH_OPERATORS:
        return False, f"Expected expression after {_token_word(tokens[-1])}"
    if len(tokens) >= 2 and [_token_word(token) for token in tokens[-2:]] == [
        "USING",
        "ANALYZER",
    ]:
        return False, "Expected analyzer name after USING ANALYZER"

    if first == "SELECT":
        main_statement = "SELECT"
    elif first in {"WITH", "EXPLAIN"}:
        main_statement = _top_level_statement(tokens, 1)
        if main_statement == "WITH":
            with_index = next(
                i for i, token in enumerate(tokens[1:], start=1) if _token_word(token) == "WITH"
            )
            main_statement = _top_level_statement(tokens, with_index + 1)
    else:
        return False, f"Statement prefix '{tokens[0].text}' not allowed in read-only mode"

    if main_statement != "SELECT":
        return False, f"Statement type '{main_statement or first}' not allowed in read-only mode"

    words = [
        _token_word(token)
        for token in tokens
        if token.token_type not in _QUOTED_OR_LITERAL_TOKENS
    ]
    for word in words:
        if word in _WRITE_KEYWORDS:
            return False, f"Keyword '{word}' not allowed in read-only mode"

    # Doris SELECT ... INTO OUTFILE writes query results outside the server.
    # It is not a read-only operation even though the statement starts SELECT.
    for left, right in zip(words, words[1:]):
        if left == "INTO" and right in {"OUTFILE", "DUMPFILE"}:
            return False, "SELECT INTO OUTFILE not allowed in read-only mode"

    return True, ""

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
        allowed, fallback_error = _validate_tokenized_readonly(stripped)
        if allowed:
            return True, ""
        return False, fallback_error or f"SQL parse error: {e}"

    if len(statements) > 1:
        return False, "Multiple statements not allowed"

    if not statements or statements[0] is None:
        return _validate_tokenized_readonly(stripped)

    node = statements[0]

    # SELECT statements are allowed
    if isinstance(node, _READONLY_TYPES):
        return True, ""

    # Command nodes: SHOW, DESCRIBE, EXPLAIN etc.
    if isinstance(node, exp.Command):
        cmd = node.this
        if isinstance(cmd, str):
            command = cmd.upper().split(maxsplit=1)[0]
            if command in _READONLY_COMMANDS:
                return True, ""

    allowed, fallback_error = _validate_tokenized_readonly(stripped)
    if allowed:
        return True, ""

    stmt_type = type(node).__name__
    return False, fallback_error or f"Statement type '{stmt_type}' not allowed in read-only mode"
