"""The read-only guard and the query path.

`validate_read_only` is a guard the agent's SQL runs through on every call, so
it gets tests. The threat model is not malicious SQL (`?mode=ro` handles that)
but a clear early error — which means the *shape* of the rejection matters as
much as the fact of it, and `code` is what the MCP server will key off.
"""

import pytest

from slop_writer.db import SCHEMA, db_path_for
from slop_writer.errors import SlopWriterError
from slop_writer.query import run_query, schema_listing, validate_read_only

ACCEPTED = [
    "SELECT 1",
    "select id from posts",
    "  \n SELECT id FROM posts \n ",
    "WITH latest AS (SELECT MAX(id) FROM post_metrics) SELECT * FROM latest",
    "with x as (select 1) select * from x",
    "SELECT 1;",            # a single trailing semicolon is fine
    "SELECT 1  ;  ",
    "-- a leading comment\nSELECT 1",
    "-- one\n-- two\nSELECT 1",
    "/* block */ SELECT 1",
    "/* multi\n   line */\nSELECT 1",
    "-- mixed\n/* both kinds */ -- again\nSELECT 1",
]

REJECTED = [
    "DELETE FROM posts",
    "INSERT INTO posts (id) VALUES (1)",
    "UPDATE posts SET text = ''",
    "DROP TABLE posts",
    "ATTACH DATABASE 'x.db' AS x",
    "PRAGMA table_info(posts)",
    "CREATE TABLE t (a)",
    "VACUUM",
    "EXPLAIN SELECT 1",     # not a SELECT/WITH first word
    "",
    "   ",
    "-- only a comment\n",
    "SELECT 1; DROP TABLE posts",
    "SELECT 1; SELECT 2",
    "-- sneaky\nSELECT 1; DELETE FROM posts;",
]


@pytest.mark.parametrize("sql", ACCEPTED)
def test_accepted(sql):
    validate_read_only(sql)


@pytest.mark.parametrize("sql", REJECTED)
def test_rejected(sql):
    with pytest.raises(SlopWriterError) as exc:
        validate_read_only(sql)
    assert exc.value.code == "QUERY_REJECTED"


def test_empty_query_names_itself_in_the_message():
    with pytest.raises(SlopWriterError) as exc:
        validate_read_only("")
    assert "<empty>" in exc.value.message


def test_multi_statement_says_so():
    with pytest.raises(SlopWriterError) as exc:
        validate_read_only("SELECT 1; DROP TABLE posts")
    assert "multi-statement" in exc.value.message


# --------------------------------------------------------------------------
# run_query
# --------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    """A real on-disk DB for `run_query`, which opens by path in `?mode=ro`."""
    import sqlite3

    path = db_path_for(tmp_path, "@fastnewsdev")
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO posts (id, link, date, text) VALUES (1, 'l', 'd', 'hello')"
    )
    conn.execute(
        "INSERT INTO posts (id, link, date, text) VALUES (2, 'l', 'd', 'world')"
    )
    conn.commit()
    conn.close()
    return tmp_path


def test_returns_columns_and_every_row(db):
    result = run_query("SELECT id, text FROM posts ORDER BY id", "@fastnewsdev", db)
    assert result.columns == ["id", "text"]
    assert result.rows == [(1, "hello"), (2, "world")]


def test_channel_handle_resolves_with_or_without_the_at(db):
    assert run_query("SELECT COUNT(*) FROM posts", "fastnewsdev", db).rows == [(2,)]


def test_missing_database_is_no_data_not_a_crash(tmp_path):
    with pytest.raises(SlopWriterError) as exc:
        run_query("SELECT 1", "@never-scraped", tmp_path)
    assert exc.value.code == "NO_DATA"
    assert "Scrape @never-scraped first" in (exc.value.hint or "")


def test_a_bad_column_carries_the_schema_back_as_the_hint(db):
    """The listing rides on the error so the caller can fix its SQL in one
    retry instead of guessing column names."""
    with pytest.raises(SlopWriterError) as exc:
        run_query("SELECT no_such_col FROM posts", "@fastnewsdev", db)
    assert exc.value.code == "QUERY_REJECTED"
    assert "Available tables/columns:" in (exc.value.hint or "")
    assert "posts(" in exc.value.hint


def test_a_syntax_error_gets_no_schema_dump(db):
    """Only 'no such column/table' is worth a listing; anything else would
    just bloat the error."""
    with pytest.raises(SlopWriterError) as exc:
        run_query("SELECT FROM WHERE", "@fastnewsdev", db)
    assert exc.value.hint is None


def test_writes_are_stopped_before_the_engine_sees_them(db):
    with pytest.raises(SlopWriterError) as exc:
        run_query("DELETE FROM posts", "@fastnewsdev", db)
    assert exc.value.code == "QUERY_REJECTED"


# --------------------------------------------------------------------------
# schema_listing
# --------------------------------------------------------------------------


def test_listing_hides_fts_shadow_tables_but_keeps_the_queryable_ones(tmp_path):
    from slop_writer.db import open_db

    conn = open_db(tmp_path, "@chan")
    listing = schema_listing(conn)
    conn.close()

    assert "posts_fts(" in listing
    assert "gm_fts(" in listing
    assert "posts_fts_data" not in listing
    assert "posts_fts_idx" not in listing
    assert "sqlite_" not in listing


def test_listing_includes_generated_columns(conn):
    """`author` is a generated column — invisible to PRAGMA table_info, and
    exactly what a retrying query should be told about."""
    listing = schema_listing(conn)
    assert "author" in listing
    assert "Full docs: references/schema.md" in listing
