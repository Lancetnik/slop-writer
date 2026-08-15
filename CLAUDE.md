# tg-scraper / tg-analytic-skill

A Claude Code **skill** that analyzes a Telegram channel (the author's own
[@fastnewsdev](https://t.me/fastnewsdev)). Not an app — a bundled CLI the skill
drives. Distributed via the `skills` npm CLI (`npx skills@latest add ...`).

## Layout

- `skills/tg-analytic-skill/` — the skill itself (read-only when installed).
  - `SKILL.md` — a **router**, not a manual (docs/adr/0005): the invariants every
    branch needs plus a table pointing at the per-CLI reference. Command flags
    belong in `references/`, so a new command usually edits a reference and
    leaves `SKILL.md` alone.
  - `scripts/tg_scrape.py` — the Telegram-facing **read** CLI (Telethon).
  - `scripts/tg_publish.py` — the Telegram-facing **write** CLI: publish paths
    (`schedule`/`reschedule`/`edit`). Isolated from the read scripts on purpose
    (docs/adr/0003).
  - `scripts/tg_query.py` — read-only SQL CLI over the per-channel SQLite DB.
  - `references/scraping.md` / `querying.md` / `publishing.md` — one per CLI,
    each the target of one row in `SKILL.md`'s branch table: flags, worked
    examples, and that CLI's failure modes co-located with the command they
    hit.
  - `references/schema.md` — restates `SCHEMA` for the SQL-writing agent; read
    before writing SQL. Reached via `querying.md`.
  - `references/markup.md` — supported Markdown→Telegram markup for
    `tg_publish.py`; read before writing a post body. Reached via
    `publishing.md`.
- `skills/setup-tg-analytic/` — second, **user-invoked** skill
  (`disable-model-invocation: true`): collects credentials, writes
  `.tg-analytic/.env`, hands the TTY login to the user. The CLIs name it in
  their missing-credentials/session errors, so the agent knows to stop and ask
  rather than attempt an interactive login.
- `src/slop_writer/` — the **library the CLIs import**, published to PyPI as
  `slop-writer`. It holds the domain logic: the scripts are argument parsing
  and output rendering, nothing else, so the MCP server can be a *second
  caller* of the same functions rather than a rewrite. The scripts name it in
  their PEP-723 headers (`slop-writer>=0.2,<0.3`), so `uv run` fetches it from
  the index; nothing is vendored into the skill directory. Modules import each
  other relatively (`from .db import …`).
  - `db.py` — paths, the `SCHEMA` + `FTS_SCHEMA` constants (**source of truth**
    for the DB layout), and DB open helpers. **Stdlib-only**, so importing it
    doesn't drag Telethon in.
  - `errors.py` — `SlopWriterError` / `UsageError` (exit 2), carrying
    `message` / `hint` / `code`. The domain **raises**; each entrypoint decides
    how to report. Stdlib-only.
  - `query.py` — the read-only SQL guards (`validate_read_only`) and execution
    (`run_query`, `schema_listing`). **Stdlib-only** with `db.py`, which is
    what keeps a query answerable without a Telegram client.
  - `tg.py` — Telethon session/credential plumbing (`_credentials`,
    `make_client`, `channel_session`, `require_session`, `resolve_peer`,
    `session_path`). Telethon-dependent, so kept out of stdlib-only `db.py`.
  - `messages.py` — Telethon `Message` → plain fields (`media_type`,
    `count_reactions`, `sender_fields`, `group_albums`, `tme_link`). No DB, no
    network, so `scrape` and `group` share it without importing each other.
  - `scrape.py` — the post pipeline: `scrape_posts` / `refresh_posts` over one
    `ingest` lifecycle, album completion, and the `posts`/`post_metrics` writes.
  - `group.py` — the discussion group: service/admin-log classification, thread
    linkage, the `group_messages`/`group_events` writers, and `scan_group`.
    Imports Telethon (the stdlib-only property went with 0.2.0 — one flat
    dependency set made it free of value, and `db`/`query` are the modules
    where it earns its keep). `scrape` imports the row writers here, never the
    reverse — comments *are* group messages (adr/0002).
  - `stats.py` — the broadcast-stats API: `fetch_subscribers`,
    `fetch_views_by_hour`, and the graph decoding both need.
  - `scheduled.py` — reading the scheduled queue (`list_scheduled`,
    `get_scheduled_message`). Read-only, and kept out of `publish.py` so the
    read CLI never imports the module that can post.
  - `publish.py` — **the write surface** (adr/0003): `prepare_schedule`
    validates before any session is required, then `schedule_post` /
    `reschedule_post` / `edit_post`. Nothing on a read path imports it.
  - `render.py` — pure-presentation Markdown renderers (`summarize_*`); plain
    dicts in, stdout out — no Telethon or SQLite types.
  - `markdown.py` — `publish.py` only: walks mistune's Markdown AST straight
    to Telethon `MessageEntity` objects (no HTML, no sulguk). Tables render as
    monospace `pre`; UTF-16 offset accounting lives here.
- `tools/check_schema_doc.py` — **dev-only** drift guard, kept *outside* the
  skill so it isn't shipped to users; run after editing `SCHEMA` or
  `references/schema.md`. Its PEP-723 header pins `slop-writer` to *this
  checkout* (`[tool.uv.sources]`, editable), never to the released version —
  a guard reading the published schema would pass while the tree disagrees.
- `.tg-analytic/` — **runtime state at the project root** (cwd), gitignored:
  `.env`, `session.session`, one `<channel>.db` per channel, `media/`. The
  **CLIs** anchor this on `Path.cwd()` (`PROJECT_ROOT` at the top of each
  script) and pass the derived paths in; `slop_writer` never reads the cwd
  itself. Always run from the project root.

## Stack

- Python ≥3.11, run via `uv run` (PEP-723 inline deps in each script header).
  Shared helpers live in the published `slop-writer` package (`src/`); the CLIs
  import them as `from slop_writer.x import …`. Each script also declares what
  it imports *directly* — the package dependency is not a licence to drop them.
  For local development, pass `--with-editable .` to run against the working
  tree (it layers over the resolved release; the version pin still has to be
  satisfiable from the index).
- **mistune** — `slop_writer.markdown` only: parses the Markdown post body to an AST,
  which `slop_writer/markdown.py` walks straight to Telethon `MessageEntity` objects (no
  HTML hop, no sulguk). mistune (CommonMark-ish) over Python-Markdown on
  purpose: it keeps `#hashtag` lines literal instead of parsing them as `<h1>`,
  and lets a list interrupt a paragraph without a blank line. Pure-Python, zero
  transitive deps. Not needed by the read/query scripts. (RichText was a
  dead-end: it's Instant-View-only — messages carry only text + MessageEntity.)
- **Telethon** (`>=1.36,<2`) — Telegram client API (not the bot API). Auth = a
  `session.session` file from a one-time interactive `login` (needs a TTY for
  the SMS code, so it can't run via the Bash tool — tell the user to run it).
- **typer** for the CLI — a *script* dependency only, never the library's: the
  domain raises `SlopWriterError` and the script turns it into stderr + an exit
  code. **SQLite** for storage (one DB file per channel, leading `@` stripped
  from the filename). `run_query` opens `?mode=ro`.

## tg_scrape.py commands

| Command | Does | Needs |
| --- | --- | --- |
| `login` | one-time interactive auth → writes session | TTY (user runs it) |
| `scrape` | posts + comments + media + forwarders → DB; appends a `post_metrics` row per run | session |
| `fetch <ids>` | refresh specific post ids (one round-trip, no scan) | session |
| `group` | discussion-group messages + threads + join/leave events → DB; appends a `group_metrics` row per run | membership in the group (`--channel @chan` for the linked group, or `--group @grp` standalone) |
| `subscribers` | growth/churn by source from stats API | **admin** + ~500+ subs |
| `views` | views per hour of day | **admin** + stats-eligible |
| `scheduled` | list not-yet-published posts (console-only, no DB) | **post rights** |

## tg_publish.py commands

| Command | Does | Needs |
| --- | --- | --- |
| `schedule` | queue a Markdown post (body from `--file` or stdin) to publish at `--at`; Markdown→Telethon entities via `slop_writer.markdown`; `--photo` (repeatable, ≤10 → album) attaches images, body becomes the caption (may be empty; length caps enforced by Telegram — 1024/2048 Premium — not the CLI); `--caption-above` puts it on top via an `invert_media` monkey patch (Telethon v1 won't expose it, see #4410); prints confirmation, no DB write | **post rights** + session |
| `reschedule` | move scheduled post `--id` to a new `--at` (body unchanged); re-applies the 1h floor | **post rights** + session |
| `edit` | replace scheduled post `--id`'s body (from `--file` or stdin, time unchanged); **no** floor check | **post rights** + session |

`--id` is the `sched-msg` id from `tg_scrape.py scheduled`. `--at` is ISO-8601
**with offset** (naive rejected); the now+1h floor is a hardcoded `MIN_LEAD`
constant with no CLI/env override — the guard exists so the agent can't
schedule too soon (docs/adr/0003). `reschedule`/`edit` are `editMessage` with
`schedule_date`; Telethon returns `None` for scheduled edits, so the commands
report from known inputs, not the call result. The body (`schedule`/`edit`)
comes from `--file PATH` or stdin (`--file -`, or omit it); pipe a quoted
heredoc to publish a draft's clean body without writing a temp file (the CLI
strips no metainfo — pass only the body).

Scrape selection flags are mutually exclusive; default to `--latest N`
(newest-first), never bare `--limit N` (walks oldest-first from msg 1).

## Key architecture facts (non-obvious)

- **The domain raises, the entrypoint reports.** No module under
  `src/slop_writer/` prints an error or exits: it raises `SlopWriterError`
  (`UsageError` for exit 2) and the CLI turns that into stderr + an exit code.
  This is what lets the MCP server call the same function and build a JSON
  payload instead. `code` is the #15 vocabulary, and is `None` at the raise
  sites that vocabulary doesn't name yet.
- Handles resolve through `resolve_peer` **before** any `open_db` call, so a
  typo exits 1 with `Cannot resolve @x` instead of a raw Telethon traceback
  plus a stray empty `.tg-analytic/<typo>.db`. Keep that order when adding a
  command; zero scraped posts then means an empty window, never a bad handle.
- `post_metrics` is **append-only** — use `MAX(id)` for "latest snapshot", not
  `MAX(scrape_date)`. See the canonical CTE in `references/schema.md`.
- **One album = one `posts` row.** A selection window can start inside an album,
  handing the pipeline a *suffix* of it; `complete_albums` re-fetches the
  missing members by id (≤10 per album, one round-trip) before `process_post`
  picks the head, so a captionless member never becomes a post of its own. That
  phantom row was silent — Telegram reports views/forwards on every album
  member — and doubled the album in every `SUM`/`AVG`. `refresh_posts` passes
  `window_contiguous=False` because arbitrary ids can't prove an album whole.
  `heal_album_phantoms` (in `slop_writer/db.py`, run from `open_db` and again after a
  scrape) deletes rows left by pre-fix runs, and `upsert_post` clears
  `post_attachments` by `attachment_id` as well as `post_id` so an attachment
  belongs to exactly one post.
- Telethon TL types are dynamically generated, so Pyright flags `.sender`,
  `.chats`, `.full_chat`, `.forwards` etc. as unknown attributes throughout —
  these warnings are expected noise, not real errors.
- Every command prints a Markdown summary to stdout designed to be pasted to the
  user as-is. The domain function **returns** the summary shape (`ScrapeResult`,
  `GroupScanResult`, …) and the CLI hands it to `summarize_*`; a new command
  follows that split rather than printing from the library.
- Full-text search (docs/adr/0004): `posts_fts`/`gm_fts` are FTS5
  external-content indexes synced by `FTS_SCHEMA` triggers; `open_db` applies
  `FTS_SCHEMA` best-effort (no FTS5 module → warning, search lost, scraping
  fine) and backfills via `'rebuild'` only when a table was just created.
  `MATCH 'stem*'` is the canonical text search (unicode61, no stemming);
  `schema_listing` hides the `*_fts_*` shadow tables from its error listing.
- `group_messages` is the **only** comment store (docs/adr/0002 superseded
  the separate `post_comments` table): `scrape`/`fetch` replace each post's
  thread (`thread_post_id` = post id), `group` upserts its scan window.
  Engagement queries always filter `is_thread_root = 0`. `group_events` PK
  is `(id, user_id)` — one add-user service message can carry several users.
