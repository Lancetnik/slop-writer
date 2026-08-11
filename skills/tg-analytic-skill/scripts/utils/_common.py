"""Shared data home: runtime paths, the SQLite schema, and DB open helpers.

Part of the `utils` package; the CLIs import it as `from utils._common import …`
(`scripts/` is on sys.path), and siblings here import it relatively.
Must stay stdlib-only so tg_query.py keeps its empty-dependencies property.

The SCHEMA and FTS_SCHEMA constants below are the single source of truth for
the DB layout. references/schema.md restates them for the SQL-writing agent —
run tools/check_schema_doc.py (dev-only, at the repo root) after editing
either side to catch drift.
"""

import logging
import sqlite3
from pathlib import Path

# Runtime state anchors on the *current working directory* (the project root
# the user launches from), never on the skill's install location.
DATA_DIR = Path.cwd() / ".tg-analytic"
DEFAULT_OUTPUT_DIR = DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id                     INTEGER PRIMARY KEY,
    link                   TEXT,
    date                   TEXT,
    text                   TEXT,
    edit_date              TEXT,
    reply_to_msg_id        INTEGER,
    tags                   TEXT,
    grouped_id             INTEGER,
    forwarder_from_channel TEXT
);

CREATE TABLE IF NOT EXISTS post_attachments (
    post_id        INTEGER NOT NULL,
    attachment_id  INTEGER NOT NULL,
    link           TEXT,
    media_type     TEXT,
    photo_path     TEXT,
    PRIMARY KEY (post_id, attachment_id)
);

CREATE TABLE IF NOT EXISTS post_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id         INTEGER NOT NULL,
    scrape_date     TEXT    NOT NULL,
    views           INTEGER,
    forwards        INTEGER,
    reactions       INTEGER,
    stars           INTEGER,
    comments_count  INTEGER,
    public_forwards_count INTEGER
);

CREATE INDEX IF NOT EXISTS idx_post_metrics_post
    ON post_metrics(post_id);

CREATE TABLE IF NOT EXISTS public_channels (
    link         TEXT PRIMARY KEY,
    name         TEXT,
    description  TEXT,
    subscribers  INTEGER,
    last_seen    TEXT
);

CREATE TABLE IF NOT EXISTS public_shares (
    post_id         INTEGER NOT NULL,
    forwarder_link  TEXT    NOT NULL,
    msg_link        TEXT    NOT NULL,
    first_seen      TEXT,
    PRIMARY KEY (post_id, forwarder_link, msg_link)
);

CREATE TABLE IF NOT EXISTS subscribers (
    date     TEXT PRIMARY KEY,
    total    INTEGER,
    joins    INTEGER,
    leaves   INTEGER
);

CREATE TABLE IF NOT EXISTS subscriber_sources (
    date     TEXT    NOT NULL,
    source   TEXT    NOT NULL,
    joins    INTEGER,
    PRIMARY KEY (date, source)
);

CREATE TABLE IF NOT EXISTS group_messages (
    id               INTEGER PRIMARY KEY,
    date             TEXT,
    text             TEXT,
    user_id          INTEGER,
    user_name        TEXT,
    user_username    TEXT,
    author           TEXT GENERATED ALWAYS AS (
        COALESCE(user_username, user_name, CAST(user_id AS TEXT))
    ) VIRTUAL,
    reply_to_msg_id  INTEGER,
    thread_post_id   INTEGER,
    is_thread_root   INTEGER NOT NULL DEFAULT 0,
    reactions        INTEGER,
    media_type       TEXT
);

CREATE INDEX IF NOT EXISTS idx_group_messages_thread
    ON group_messages(thread_post_id);

CREATE TABLE IF NOT EXISTS group_events (
    id             INTEGER NOT NULL,
    date           TEXT,
    kind           TEXT,
    via            TEXT,
    user_id        INTEGER,
    user_name      TEXT,
    user_username  TEXT,
    author         TEXT GENERATED ALWAYS AS (
        COALESCE(user_username, user_name, CAST(user_id AS TEXT))
    ) VIRTUAL,
    PRIMARY KEY (id, user_id)
);

CREATE TABLE IF NOT EXISTS group_metrics (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scrape_date  TEXT NOT NULL,
    group_link   TEXT,
    group_title  TEXT,
    members      INTEGER
);
"""

# Full-text search over posts.text / group_messages.text (docs/adr/0004).
# Kept out of SCHEMA so open_db can apply it best-effort: a SQLite build
# without the FTS5 module loses search but keeps scraping. External-content
# tables (no text duplication) stay in sync via triggers — safe because no
# write path uses INSERT OR REPLACE, which would skip the delete trigger.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
    text,
    content='posts',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS posts_fts_ai AFTER INSERT ON posts BEGIN
    INSERT INTO posts_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS posts_fts_ad AFTER DELETE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, text)
    VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS posts_fts_au AFTER UPDATE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, text)
    VALUES ('delete', old.id, old.text);
    INSERT INTO posts_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS gm_fts USING fts5(
    text,
    content='group_messages',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS gm_fts_ai AFTER INSERT ON group_messages BEGIN
    INSERT INTO gm_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS gm_fts_ad AFTER DELETE ON group_messages BEGIN
    INSERT INTO gm_fts(gm_fts, rowid, text)
    VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS gm_fts_au AFTER UPDATE ON group_messages BEGIN
    INSERT INTO gm_fts(gm_fts, rowid, text)
    VALUES ('delete', old.id, old.text);
    INSERT INTO gm_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


def db_path_for(output_dir: Path, channel: str) -> Path:
    """One DB file per channel, e.g. .tg-analytic/fastnewsdev.db."""
    safe = channel.lstrip("@").replace("/", "_") or "channel"
    return output_dir / f"{safe}.db"


def _drop_legacy_tables(conn: sqlite3.Connection) -> None:
    """Self-heal DB files created before the post_comments merge.

    Comments live in group_messages since ADR 0002 — scrape writes
    full-fidelity rows there. The old table is dropped rather than
    migrated; comment data reappears on the next scrape run."""
    conn.execute("DROP TABLE IF EXISTS post_comments")
    conn.commit()


def _ensure_fts(conn: sqlite3.Connection) -> None:
    """Create the FTS5 search indexes; backfill them on first creation.

    A freshly created external-content index over an already-populated table
    starts empty and MATCH would silently return zero rows, so tables that
    didn't exist before this call get a one-time 'rebuild'; the triggers keep
    them current afterwards. On a SQLite build without the FTS5 module this
    logs a warning and returns — search is lost, scraping still works."""
    missing = [
        t
        for t in ("posts_fts", "gm_fts")
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ?", (t,)
        ).fetchone() is None
    ]
    try:
        conn.executescript(FTS_SCHEMA)
    except sqlite3.OperationalError as exc:
        logging.getLogger(__name__).warning(
            "full-text search disabled (%s) - this SQLite build has no FTS5",
            exc,
        )
        return
    for t in missing:
        conn.execute(f"INSERT INTO {t}({t}) VALUES('rebuild')")


def open_db(output_dir: Path, channel: str) -> sqlite3.Connection:
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path_for(output_dir, channel))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _ensure_fts(conn)
    _drop_legacy_tables(conn)
    return conn
