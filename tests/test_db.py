"""Paths, the open path, and full-text search.

`open_db` is the one function every command runs through, and it does more
than open: it applies the schema, wires FTS best-effort, drops the legacy
comment table and heals album phantoms. Each of those is a silent operation,
which is exactly why they are asserted here.
"""

import sqlite3

import pytest

from slop_writer.db import SCHEMA, data_dir, db_path_for, env_path, open_db


def test_paths_hang_off_the_project_root_the_caller_names(tmp_path):
    assert data_dir(tmp_path).name == ".tg-analytic"
    assert env_path(tmp_path) == tmp_path / ".tg-analytic" / ".env"


@pytest.mark.parametrize(
    "channel, filename",
    [
        ("@fastnewsdev", "fastnewsdev.db"),
        ("fastnewsdev", "fastnewsdev.db"),
        ("some/handle", "some_handle.db"),
        ("@", "channel.db"),
    ],
)
def test_one_db_file_per_channel(tmp_path, channel, filename):
    assert db_path_for(tmp_path, channel).name == filename


def test_open_creates_the_directory_and_every_table(tmp_path):
    root = tmp_path / "nested" / "state"
    conn = open_db(root, "@chan")
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()

    assert root.is_dir()
    assert {
        "posts", "post_attachments", "post_metrics", "public_channels",
        "public_shares", "subscribers", "subscriber_sources",
        "group_messages", "group_events", "group_metrics",
    } <= tables


def test_opening_twice_is_safe(tmp_path):
    open_db(tmp_path, "@chan").close()
    conn = open_db(tmp_path, "@chan")
    conn.execute("SELECT COUNT(*) FROM posts")
    conn.close()


def test_the_legacy_comment_table_is_dropped(tmp_path):
    """Comments live in `group_messages` since docs/adr/0002; a DB from before
    the merge self-heals rather than keeping two comment stores."""
    path = db_path_for(tmp_path, "@chan")
    tmp_path.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(path)
    legacy.executescript(SCHEMA)
    legacy.execute("CREATE TABLE post_comments (id INTEGER PRIMARY KEY)")
    legacy.commit()
    legacy.close()

    conn = open_db(tmp_path, "@chan")
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'post_comments'"
    ).fetchone()
    conn.close()
    assert found is None


def test_open_heals_phantoms_left_by_an_older_run(tmp_path):
    path = db_path_for(tmp_path, "@chan")
    tmp_path.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(path)
    old.executescript(SCHEMA)
    old.executemany(
        "INSERT INTO posts (id, text, grouped_id) VALUES (?, ?, ?)",
        [(10, "", 555), (11, "caption", 555)],
    )
    old.commit()
    old.close()

    conn = open_db(tmp_path, "@chan")
    ids = [r[0] for r in conn.execute("SELECT id FROM posts")]
    conn.close()
    assert ids == [11]


# --------------------------------------------------------------------------
# Full-text search (docs/adr/0004)
# --------------------------------------------------------------------------

def _has_fts5() -> bool:
    """Ask the way `_ensure_fts` does — by trying, not by reading a pragma."""
    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.OperationalError:
        return False
    return True


fts5 = pytest.mark.skipif(
    not _has_fts5(),
    reason="this SQLite build has no FTS5 — search is lost, scraping still works",
)


@fts5
def test_a_new_post_becomes_searchable(tmp_path):
    conn = open_db(tmp_path, "@chan")
    conn.execute("INSERT INTO posts (id, text) VALUES (1, 'about telemetry')")
    conn.commit()
    hits = conn.execute(
        "SELECT rowid FROM posts_fts WHERE posts_fts MATCH 'telem*'"
    ).fetchall()
    conn.close()
    assert hits == [(1,)]


@fts5
def test_editing_and_deleting_keep_the_index_in_step(tmp_path):
    conn = open_db(tmp_path, "@chan")
    conn.execute("INSERT INTO posts (id, text) VALUES (1, 'original wording')")
    conn.execute("UPDATE posts SET text = 'replacement wording' WHERE id = 1")
    conn.commit()

    def matches(term):
        return conn.execute(
            "SELECT rowid FROM posts_fts WHERE posts_fts MATCH ?", (term,)
        ).fetchall()

    assert matches("original") == []
    assert matches("replacement") == [(1,)]

    conn.execute("DELETE FROM posts WHERE id = 1")
    conn.commit()
    assert matches("replacement") == []
    conn.close()


@fts5
def test_group_messages_are_searchable_too(tmp_path):
    conn = open_db(tmp_path, "@chan")
    conn.execute("INSERT INTO group_messages (id, text) VALUES (1, 'комментарий')")
    conn.commit()
    hits = conn.execute(
        "SELECT rowid FROM gm_fts WHERE gm_fts MATCH 'комментар*'"
    ).fetchall()
    conn.close()
    assert hits == [(1,)]


@fts5
def test_an_index_created_over_existing_rows_is_backfilled(tmp_path):
    """A fresh external-content index starts empty and MATCH would silently
    return nothing — the one-time 'rebuild' in `_ensure_fts` is what stops a
    pre-FTS DB from looking like it has no posts."""
    path = db_path_for(tmp_path, "@chan")
    tmp_path.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(path)
    old.executescript(SCHEMA)  # no FTS_SCHEMA: this is a pre-adr/0004 DB
    old.execute("INSERT INTO posts (id, text) VALUES (1, 'written before search')")
    old.commit()
    old.close()

    conn = open_db(tmp_path, "@chan")
    hits = conn.execute(
        "SELECT rowid FROM posts_fts WHERE posts_fts MATCH 'written'"
    ).fetchall()
    conn.close()
    assert hits == [(1,)]


@fts5
def test_reopening_does_not_double_count_an_already_backfilled_index(tmp_path):
    conn = open_db(tmp_path, "@chan")
    conn.execute("INSERT INTO posts (id, text) VALUES (1, 'once')")
    conn.commit()
    conn.close()

    conn = open_db(tmp_path, "@chan")
    hits = conn.execute(
        "SELECT rowid FROM posts_fts WHERE posts_fts MATCH 'once'"
    ).fetchall()
    conn.close()
    assert hits == [(1,)]
