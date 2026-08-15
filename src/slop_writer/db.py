"""Runtime paths, the SQLite schema, and DB open helpers.

Stdlib-only, and deliberately so: importing this module must not drag Telethon
in. That keeps `tg_query.py` free of Telegram machinery and lets a future MCP
server touch the DB without a client.

Nothing here reads the current working directory. Every path is derived from a
project root the caller passes in — the CLIs default that to `Path.cwd()`,
a server would take it from its configuration.

The SCHEMA and FTS_SCHEMA constants below are the single source of truth for
the DB layout. references/schema.md restates them for the SQL-writing agent —
run tools/check_schema_doc.py (dev-only, in the source repo) after editing
either side to catch drift.
"""

import logging
import sqlite3
from pathlib import Path

# Runtime state lives under the *project* root the caller names, never under
# the skill's install location.
DATA_DIR_NAME = ".tg-analytic"


def data_dir(project_root: Path) -> Path:
    """The runtime state directory (DB files, media, session, .env)."""
    return project_root / DATA_DIR_NAME


def env_path(project_root: Path) -> Path:
    """Where the Telegram credentials live for this project."""
    return data_dir(project_root) / ".env"

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


def scraped_channels(output_dir: Path) -> list[str]:
    """The channels this project holds data for, one per DB file.

    The inverse of `db_path_for`, and beside it so the
    `.tg-analytic/<channel>.db` layout stays the knowledge of one module.
    Touches no database and needs no session: a project can say which
    channels it knows before Telegram is reachable at all, which is what
    lets a resolve failure answer with them (#43).

    Sorted, so the string a caller builds out of this is stable run to run."""
    if not output_dir.is_dir():
        return []
    return sorted(path.stem for path in output_dir.glob("*.db"))


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


def heal_album_phantoms(conn: sqlite3.Connection) -> None:
    """Collapse each album back to a single `posts` row.

    A scrape window that started inside an album used to take the first member
    it saw for a post of its own: an extra row sharing the album's grouped_id,
    with empty text, its own `post_metrics` series and a duplicate slice of the
    album's attachments. Nothing failed - Telegram reports views/forwards on
    every album member, so the phantom's numbers looked plausible - but every
    SUM/AVG over posts counted the album twice or three times.

    The scraper no longer creates them (`complete_albums` in tg_scrape.py); this
    clears the ones already in the DB. The surviving row is the caption carrier,
    or the lowest id when the album has no caption; the phantoms' metrics,
    attachments and share rows go with them, while comment threads that got
    re-pointed at a phantom move back onto the real post. An album with two
    text-carrying rows is not a phantom pattern - it is logged and left alone."""
    log = logging.getLogger(__name__)
    rows = conn.execute(
        """
        SELECT grouped_id, id, length(COALESCE(text, ''))
        FROM posts
        WHERE grouped_id IS NOT NULL AND grouped_id <> ''
          AND grouped_id IN (
              SELECT grouped_id FROM posts
              WHERE grouped_id IS NOT NULL AND grouped_id <> ''
              GROUP BY grouped_id HAVING COUNT(*) > 1
          )
        ORDER BY grouped_id, id
        """
    ).fetchall()
    if not rows:
        return

    albums: dict[int, list[tuple[int, int]]] = {}
    for grouped_id, post_id, text_len in rows:
        albums.setdefault(grouped_id, []).append((post_id, text_len))

    pairs: list[tuple[int, int]] = []  # (phantom id, head id)
    for grouped_id, members in albums.items():
        head = next((pid for pid, n in members if n), members[0][0])
        texted = [pid for pid, n in members if n]
        if len(texted) > 1:
            log.warning(
                "album %s has %d post rows with text (%s) - not the phantom "
                "pattern, left untouched",
                grouped_id, len(texted), texted,
            )
            continue
        pairs += [(pid, head) for pid, _ in members if pid != head]

    if not pairs:
        return
    phantoms = [pid for pid, _ in pairs]
    marks = ",".join("?" * len(phantoms))
    conn.executemany(
        "UPDATE group_messages SET thread_post_id = ? WHERE thread_post_id = ?",
        [(head, pid) for pid, head in pairs],
    )
    conn.execute(f"DELETE FROM post_attachments WHERE post_id IN ({marks})", phantoms)
    conn.execute(f"DELETE FROM post_metrics WHERE post_id IN ({marks})", phantoms)
    conn.execute(f"DELETE FROM public_shares WHERE post_id IN ({marks})", phantoms)
    conn.execute(f"DELETE FROM posts WHERE id IN ({marks})", phantoms)
    conn.commit()
    log.warning(
        "removed %d phantom album post row(s) and their metrics: %s",
        len(phantoms),
        ", ".join(f"{pid} -> {head}" for pid, head in sorted(pairs)),
    )


def open_db(output_dir: Path, channel: str) -> sqlite3.Connection:
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path_for(output_dir, channel))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _ensure_fts(conn)
    _drop_legacy_tables(conn)
    # After _ensure_fts so the posts delete trigger keeps the index in sync.
    heal_album_phantoms(conn)
    return conn
