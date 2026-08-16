"""Read-only SQL over a channel's SQLite DB.

Stdlib-only, like `db`: the query path must stay importable without a Telegram
client, which is what lets a caller answer an analytics question without
touching the network.

Two guards, both kept: `?mode=ro` (SQLite itself rejects writes) and
`validate_read_only` (a clear error before the engine sees a destructive verb).
The threat model is not malicious SQL but expensive SQL — the row cap the
caller applies when rendering answers that.
"""

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .db import db_path_for
from .errors import SlopWriterError

# Strip leading `-- line comments` and `/* block comments */` so we can inspect
# the first real keyword. We don't try to parse strings - any leading SELECT
# inside a string literal would still satisfy the check, but that's harmless
# (the engine layer is read-only too; this guard is a clearer early error).
_LINE_COMMENT = re.compile(r"^\s*--[^\n]*\n?")
_BLOCK_COMMENT = re.compile(r"^\s*/\*.*?\*/", re.DOTALL)


@dataclass
class QueryResult:
    """Shapes `render.summarize_query` consumes. `rows` is everything the
    query returned — capping is a presentation decision, and the true count
    must survive it."""
    columns: list[str]
    rows: list[tuple]


@dataclass
class QueryFailure:
    """One query's refusal, carried instead of raised.

    A batch is a series of *independent* questions, so one bad SQL must not
    discard the answers to the others — the caller would have to re-ask all of
    them, which is exactly the round-trip the batch existed to avoid. Batch-wide
    conditions (no DB at all) still raise: there is no partial answer there."""
    message: str
    hint: str | None
    code: str


def _strip_leading_comments(sql: str) -> str:
    prev = None
    cur = sql.lstrip()
    while prev != cur:
        prev = cur
        cur = _LINE_COMMENT.sub("", cur, count=1).lstrip()
        cur = _BLOCK_COMMENT.sub("", cur, count=1).lstrip()
    return cur


def validate_read_only(sql: str) -> None:
    """Reject anything that isn't a single SELECT/WITH query.

    Belt-and-braces alongside `?mode=ro`: gives a clear error before the
    engine sees a destructive verb (INSERT/UPDATE/DELETE/DROP/ATTACH/PRAGMA
    etc.), and catches multi-statement payloads even though sqlite3.execute
    only runs the first one."""
    body = _strip_leading_comments(sql)
    first = body.split(None, 1)[0].upper() if body else ""
    if first not in ("SELECT", "WITH"):
        raise SlopWriterError(
            "rejected query: only SELECT or WITH queries are allowed "
            f"(got '{first or '<empty>'}')",
            code="QUERY_REJECTED",
        )
    # Reject obvious multi-statement payloads like `SELECT 1; DROP TABLE posts`.
    # A trailing single `;` is fine. A semicolon followed by more SQL is not.
    trimmed = body.rstrip().rstrip(";").rstrip()
    if ";" in trimmed:
        raise SlopWriterError(
            "rejected query: multi-statement queries are not allowed",
            code="QUERY_REJECTED",
        )


def schema_listing(conn: sqlite3.Connection) -> str:
    """Compact one-line-per-table schema dump, attached to 'no such
    column/table' errors so a caller (typically an LLM) can fix its query in
    one retry instead of guessing names."""
    lines = ["Available tables/columns:"]
    # The GLOB hides FTS5 shadow tables (posts_fts_data, posts_fts_idx, ...)
    # while keeping the queryable posts_fts/gm_fts virtual tables listed.
    tables = conn.execute(
        "SELECT name FROM sqlite_master"
        " WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        " AND name NOT GLOB '*_fts_*' ORDER BY name"
    ).fetchall()
    for (table,) in tables:
        # table_xinfo, not table_info: only the former lists generated columns
        # (group_messages.author), which are exactly what a retry should use.
        cols = [row[1] for row in conn.execute(f"PRAGMA table_xinfo({table})")]
        lines.append(f"  {table}({', '.join(cols)})")
    lines.append("Full docs: references/schema.md")
    return "\n".join(lines)


def _open_ro(channel: str, output_dir: Path) -> sqlite3.Connection:
    """Read-only connection, or the one error that is about the *database*
    rather than about any single query."""
    db_path = db_path_for(output_dir, channel)
    if not db_path.exists():
        raise SlopWriterError(
            f"database not found at {db_path}",
            hint=f"Scrape {channel} first — the DB is created by the first run.",
            code="NO_DATA",
        )
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _run_one(conn: sqlite3.Connection, sql: str) -> QueryResult | QueryFailure:
    try:
        validate_read_only(sql)
        cursor = conn.execute(sql)
    except SlopWriterError as e:
        return QueryFailure(e.message, e.hint, e.code or "QUERY_REJECTED")
    except sqlite3.DatabaseError as e:
        # The schema dump exists precisely for the one-shot retry, so it
        # rides along with the error rather than going to a channel the
        # caller may not be reading.
        listing = (
            schema_listing(conn)
            if "no such column" in str(e) or "no such table" in str(e)
            else None
        )
        return QueryFailure(f"query failed: {e}", listing, "QUERY_REJECTED")
    return QueryResult([d[0] for d in cursor.description or []], cursor.fetchall())


def run_queries(
    sqls: list[str], channel: str, output_dir: Path
) -> list[QueryResult | QueryFailure]:
    """Run several read-only queries against one channel's DB, in order.

    One connection serves the whole batch — the DB is opened in read-only mode,
    so writes and schema changes are rejected by SQLite itself, safe to expose
    to LLM-generated SQL. Results are positional: item *i* answers `sqls[i]`,
    successfully or not, so a caller can always line an answer up with its
    question."""
    with closing(_open_ro(channel, output_dir)) as conn:
        return [_run_one(conn, sql) for sql in sqls]


def run_query(sql: str, channel: str, output_dir: Path) -> QueryResult:
    """Run a read-only SQL query against the channel's SQLite DB.

    The single-query form of `run_queries`, and the one that *raises*. It exists
    for the CLI, whose report of a failure is a non-zero exit and a line on
    stderr — there is no section for a refusal to travel in. The server takes
    the batch form even for one question, because there a verdict is content."""
    (item,) = run_queries([sql], channel, output_dir)
    if isinstance(item, QueryFailure):
        raise SlopWriterError(item.message, hint=item.hint, code=item.code)
    return item
