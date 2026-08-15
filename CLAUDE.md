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
- `src/slop_writer/` — the **library, and now the shipped server**, published
  to PyPI as `slop-writer`. It holds the domain logic; the scripts are argument
  parsing and output rendering, nothing else, which is what lets `server.py` be
  a *second caller* of the same functions rather than a rewrite. The scripts
  name it in their PEP-723 headers (`slop-writer>=0.3,<0.4`), so `uv run`
  fetches it from the index; nothing is vendored into the skill directory.
  Modules import each other relatively (`from .db import …`).
  - `db.py` — paths, the `SCHEMA` + `FTS_SCHEMA` constants (**source of truth**
    for the DB layout), and DB open helpers. **Stdlib-only**, so importing it
    doesn't drag Telethon in.
  - `errors.py` — `SlopWriterError` / `UsageError` (exit 2), carrying
    `message` / `hint` / `code`. The domain **raises**; each entrypoint decides
    how to report. Stdlib-only. `ERROR_CODES` is the closed 16-code vocabulary
    the tool contract promises, **enforced at construction** — a code outside
    it raises `ValueError`.
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
    dicts in, **a string out** — no Telethon or SQLite types, and no `print`:
    on a stdio server stdout is the JSON-RPC transport (#16). The CLIs do
    `print(summarize_x(...))`; one renderer serves both callers.
  - `server.py` — the **MCP server**: `build_server(project_root)` registers
    all eleven tools, `assert_text_only` is the startup guard, and
    `WRITE_TOOLS` / `permission_rules()` are the read/write split written down
    for `install` to copy. Argument parsing and rendering only; no Telegram
    logic.
  - `cli.py` — the `slop-writer` console script (argparse, not typer). Decides
    the project root — from cwd, or `--project` — and loads `.env`.
  - `markdown.py` — `publish.py` only: walks mistune's Markdown AST straight
    to Telethon `MessageEntity` objects (no HTML, no sulguk). Tables render as
    monospace `pre`; UTF-16 offset accounting lives here.
- `tests/` — the package suite. **`uv run pytest`** (from the repo root; `uv`
  installs the project editable, so it exercises the working tree). It targets
  `slop_writer.*` functions and their return shapes, **never** a script's
  stdout or exit code — the CLI is scheduled to stop being the exercise
  surface, so a test that asserted on rendered Markdown would be deleted by
  the MCP move. Where the #15 vocabulary names a failure, assert on
  `SlopWriterError.code`, not on the message.
  - `factories.py` holds the faking policy: **messages are real Telethon TL
    objects** (`complete_albums`/`scan_group` gate on `isinstance`, and
    Telethon is already a hard dependency), **the client is hand-faked**.
    Recorded fixtures were rejected — they pin a wire format the `<2` pin
    already expects to move. One quirk the factories hide: `Message.text` is a
    property returning `None` until assigned, and every `msg.text or ""` in
    the package depends on it.
  - `test_server.py` builds a real `FastMCP` over a `tmp_path` root and asks
    it questions — the roster, `outputSchema` absence, the `ask`-rules-vs-tool
    -names comparison, and the write tools' failures up to (never past) the
    missing session. It is the one place the MCP contract is checked without
    a client.
  - `test_server_stdio.py` is the other half: it launches `serve --mcp` as a
    **subprocess** (cwd = a `tmp_path` project root, no `--project`) and
    speaks MCP to it, because `isError`, `structuredContent`, `_meta` and the
    handshake version are produced on the way out and no in-process call runs
    that code. One session per module, on a loop of its own thread — the suite
    still has no async plugin. A completed handshake *is* the assertion that
    `assert_text_only` passed inside the real entrypoint.
  - `test_errors.py` guards the closed vocabulary two ways: at construction,
    and **statically over the package's own source** (`ast`, every `code=`
    literal). The static half exists because most raise sites need a Telegram
    session to reach, so `__init__`'s check otherwise fires only in a live
    run. It also records the two codes no raise site names yet
    (`MESSAGE_TOO_LONG`, `NOT_A_MEMBER`) so a third is a decision.
  - Nothing in the suite touches a Telegram session, `.tg-analytic/`, or the
    network. Live runs stay the acceptance step; they are no longer the only
    one.
  - pytest lives in a PEP 735 `[dependency-groups] dev` — never an extra, so
    it cannot reach a user's install. CI asserts that against the built wheel.
  - `asyncio.run` via `tests/conftest.py:run`, not pytest-asyncio: four async
    functions don't justify a plugin whose `asyncio_mode` silently skips tests.
- `.github/workflows/test.yaml` — push/PR CI: the suite on 3.11 + 3.13, the
  wheel-metadata guard, and `check_schema_doc.py`. Deliberately **not** part of
  `publish.yaml`, whose *filename* is bound to PyPI's trusted publisher.
- `tools/check_schema_doc.py` — **dev-only** drift guard, kept *outside* the
  skill so it isn't shipped to users; run after editing `SCHEMA` or
  `references/schema.md`. Its PEP-723 header pins `slop-writer` to *this
  checkout* (`[tool.uv.sources]`, editable), never to the released version —
  a guard reading the published schema would pass while the tree disagrees.
  It stays a standalone script rather than becoming a test: its subject is a
  document under `skills/` that the package doesn't ship, and its editable pin
  is a different resolution from the suite's. CI runs it as its own step.
- `.tg-analytic/` — **runtime state at the project root** (cwd), gitignored:
  `.env`, `session.session`, one `<channel>.db` per channel, `media/`. The
  **entrypoints** anchor this on `Path.cwd()` (`PROJECT_ROOT` at the top of
  each script; `cli.py` for the server, overridable with `--project`) and pass
  the derived paths in; `slop_writer` never reads the cwd itself. Always run
  from the project root — and the MCP client launches `serve` there.

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
- **typer** for the PEP-723 scripts — a *script* dependency only, never the
  library's: the domain raises `SlopWriterError` and the script turns it into
  stderr + an exit code. `slop_writer.cli` (the shipped `slop-writer` command)
  uses **argparse** instead, so an installed server carries no CLI framework.
  **SQLite** for storage (one DB file per channel, leading `@` stripped from
  the filename). `run_query` opens `?mode=ro`.
- **mcp** (`>=1.10,<2`) — the Python MCP SDK, `mcp.server.fastmcp`. Pinned
  below 2 because `structured_output=False` is load-bearing (#16) and v2 moved
  `FastMCP` elsewhere. **python-dotenv** is a package dependency again as of
  0.3.0: `serve` decides the project root, so it is one of the callers that
  populates the environment `tg.py` reads.

## slop-writer commands

The shipped console script. `install` / `init` arrive with #20.

| Command | Does | Needs |
| --- | --- | --- |
| `serve --mcp` | run the MCP server over stdio; `--project PATH` overrides the cwd-derived project root | launched by the MCP client, not by hand |

## MCP tools

Eleven tools, server name `slop-writer`, so a permission rule reads
`mcp__slop-writer__run_query`.

- **Reads** (un-gated): `scrape_posts`, `refresh_posts`, `scan_linked_group`,
  `scan_standalone_group`, `fetch_subscribers`, `fetch_views_by_hour`,
  `list_scheduled`, `run_query`.
- **Telegram writes** (gated): `publish_schedule`, `publish_reschedule`,
  `publish_edit`.

Non-obvious, and enforced in `server.py`:

- **The `publish_` prefix is the read/write split**, not a naming style: Claude
  Code matches permission rules by tool *name*, and `server.py` imports the
  read paths and `publish.py` alike — so adr/0003's file-level auditability
  stops at the module, and the entrypoint's half of it is the name. `install`
  (#20) writes `permission_rules()` — `allow: [mcp__slop-writer]` plus three
  `ask` entries — into `.claude/settings.json`; the roster and the rules live
  in one module because a tool renamed without its rule is silently ungated,
  and `tests/test_server.py` compares the two halves.
- `_meta["anthropic/requiresUserInteraction"]` is **rejected** (#15, restated
  in adr/0003): it survives `bypassPermissions` but makes publishing
  impossible headless, which forecloses autoposting by the channel's own
  owner. So a misfire is unlikely, not impossible — `bypassPermissions` walks
  past an `ask` rule, and nothing in the Claude Code CLI adds permission rules.
- **The write tools validate before they require a session**, mirroring the
  CLI: a malformed `at` must answer with the argument to fix, not with
  "run `slop-writer init`". The empty-body error names `` `body` `` where the
  CLI names stdin or `--file`.

- **Text only, never `structuredContent`** — Claude Code discards the content
  blocks when structure is present (#12), so structure travels *inside* text.
  The SDK's default is the lossy branch, so registration goes through the
  local `_tool` wrapper (`structured_output=False`) and `assert_text_only`
  refuses to start a server whose tools carry an `outputSchema`.
- **Errors are `{code, message, hint}` JSON in the text block** with
  `isError: true`, over `errors.ERROR_CODES`. `_guarded` wraps every tool: it
  translates `SlopWriterError`, names `FLOOD_WAIT` (with `seconds`) at the one
  place a Telethon flood-wait can be caught once, and turns anything else into
  `INTERNAL` rather than a traceback. The SDK prefixes the text with
  `Error executing tool <name>: `; the JSON is the tail.
- **Argument validation failures are the one gap**: pydantic rejects a bad
  `select` arm *before* `_guarded` runs, so those come back as a pydantic
  message rather than the JSON contract.
- `select` is a **nested** tagged union (`LatestSelect` / `WindowSelect`, both
  `extra="forbid"`) — a root-level combinator gets flattened by the client
  (#23), and nothing validates tool input on the way in.
- `run_query` alone carries `_meta["anthropic/alwaysLoad"]`; every other tool
  is deferred behind `ToolSearch`. Its rendered-row default is **50** where the
  CLI's `--limit` is 100 — different layers, different readers, deliberately
  not unified.
- Server `instructions` stay **empty** (#21): they are always in context, so
  domain knowledge belongs in the skill.

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
