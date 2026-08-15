# tg-scraper / slop-writer

A Claude Code **skill plus an MCP server** that analyze a Telegram channel and
can queue a post to it. The eleven MCP tools are the whole agent-facing
surface; the skill under `skills/slop-writer/` says which tool answers which
question and what the numbers mean. Published to PyPI as `slop-writer`; two
install channels (`slop-writer install`, `npx skills@latest add …`) serve that
same skill directory, and one version covers package and skill.

Rationale for a decision lives in `docs/adr/`; vocabulary in `CONTEXT.md`;
structure (what calls what, where a symbol is) in codegraph.

## Commands

- `uv run pytest` — the suite, from the repo root. `uv` installs the project
  editable, so it exercises the working tree.
- `uv run tools/check_schema_doc.py` — dev-only drift guard; run after editing
  `SCHEMA` or `references/schema.md`. CI runs it as its own step.
- `uv run --with-editable . tools/tg_*.py` — the dev CLIs against the tree.
- `slop-writer` (the shipped console script): `install` wires this project's
  Claude Code config, `uninstall` removes exactly what it wrote, `serve --mcp`
  runs the server over stdio, `init` writes Telegram credentials and logs in.
  **`init` needs a TTY for the SMS code — the user runs it, not the agent.**

## Library rules (`src/slop_writer/`)

- **The domain raises, the entrypoint reports.** Nothing under
  `src/slop_writer/` prints an error or exits: it raises `SlopWriterError`
  (`UsageError` for exit 2) and `cli.py` or `server.py` decides how to report.
  That is what lets the server call the same function and build JSON instead.
- **`errors.ERROR_CODES` is a closed vocabulary**, enforced at construction.
  Reuse a code; adding one is a deliberate edit with a raise site.
- **A message or a hint names an argument or an operation, never a surface** —
  the same string reaches a human at stderr and a model at a tool result, and
  only one of them has a flag or a script. `tests/test_errors.py` scans every
  literal statically over the modules `server.py` can reach. When the surface
  genuinely differs, the caller supplies the noun (`prepare_schedule(
  body_source=…)`) or the boundary swaps the hint (`server._SETUP_HINT`).
- **The domain uses a client, the entrypoint owns it.** Scrape and scan paths
  come in twins: `X_with_client(client, …)` does the work, `X(…, session_file)`
  wraps it in one `channel_session`. A **new command adds both forms.**
- **`resolve_peer` runs before any `open_db`** — `open_db` lives inside the
  `channel_session` body (for the group scan, inside `scan_group_with_client`
  after `resolve_group_target`). A typo then fails on the handle instead of
  leaving a stray empty `.tg-analytic/<typo>.db`, and zero posts always means
  an empty window.
- **Renderers return a string.** `render.py` takes plain dicts and returns
  Markdown — no Telethon or SQLite types, and no `print`: on a stdio server
  stdout is the JSON-RPC transport. The domain returns the result shape
  (`ScrapeResult`, `GroupScanResult`, …); the entrypoint calls `summarize_*`.
- `db.py`, `errors.py` and `query.py` stay **stdlib-only** — a query stays
  answerable without a Telegram client.
- `publish.py` is the **write surface** (adr/0003); nothing on a read path
  imports it, which is why `scheduled.py` is separate.
- **`MESSAGE_TOO_LONG` comes from the network, by design** — the cap depends on
  the account, so `_too_long` translates Telethon's error rather than measuring
  the body.
- **A refusal is not an absence.** `CANNOT_RESOLVE` means the handle names
  nothing, `NOT_A_MEMBER` means Telegram confirmed the peer and refused this
  account — one is fixed by correcting the handle, the other by joining. Every
  new Telethon call that can refuse classifies the two.
- `cli.py` is **argparse**, so an installed server carries no CLI framework;
  typer stays a `tools/` script dependency.
- `.tg-analytic/` is runtime state at the project root (gitignored): `.env`,
  `session.session`, one `<channel>.db` per channel, `media/`. **Entrypoints**
  anchor it on the cwd (`--project` overrides); the library never reads the cwd.

## MCP server (`server.py`)

- **The `publish_` prefix is the read/write split**, not a naming style: Claude
  Code matches permission rules by tool *name*. Roster and `permission_rules()`
  live in one module because a renamed tool without its rule is silently
  ungated; `tests/test_server.py` compares the halves.
- **Text only, never `structuredContent`** — Claude Code drops the content
  blocks when structure is present, so structure travels *inside* text. Register
  through the local `_tool` wrapper (`structured_output=False`);
  `assert_text_only` refuses to start a server whose tools carry an
  `outputSchema`.
- **Errors are `{code, message, hint}` JSON in the text block** with
  `isError: true`. `_guarded` wraps every tool, names `FLOOD_WAIT` (with
  `seconds`), and labels anything unanticipated `INTERNAL`.
- **Two hints belong to the entrypoint**, which is why `_guarded` takes the
  project's `output_dir`: the setup pair answers with `slop-writer init`, and
  `CANNOT_RESOLVE` answers with the channels this project holds
  (`db.scraped_channels()`) — an agent that never had a handle cannot fix a typo.
- **Write tools validate before they require a session** — a malformed `at`
  answers with the argument to fix, not with "run `slop-writer init`".
- `select` is a **nested** tagged union (`extra="forbid"` on both arms); a
  root-level combinator gets flattened by the client. Pydantic rejects a bad arm
  *before* `_guarded`, so those alone escape the JSON contract.
- `run_query` alone carries `_meta["anthropic/alwaysLoad"]`; the rest are
  deferred behind `ToolSearch`.
- `_meta["anthropic/requiresUserInteraction"]` stays **rejected** and server
  `instructions` stay **empty** (adr/0003): the first forecloses headless
  autoposting by the channel's owner, the second is context spent every turn on
  what belongs in the skill.

## The skill (`skills/slop-writer/`)

Five files, no `scripts/`. The seam is *tool description = how to call, skill =
what it means*, checkable both ways: **no metric fact in a tool description, no
argument name in the skill.** A new tool therefore edits `server.py` and usually
nothing here; a new *invariant* edits here and not `server.py`. `SKILL.md` is a
router (adr/0005) to `references/{analysis,schema,publishing,markup}.md`.

## Data invariants

- `post_metrics` is **append-only** — `MAX(id)` for "latest snapshot", never
  `MAX(scrape_date)`. Canonical CTE in `references/schema.md`.
- **One album = one `posts` row.** `complete_albums` re-fetches missing members
  before `process_post` picks the head, so a captionless member never becomes a
  post of its own (a phantom row doubles the album in every `SUM`/`AVG`).
  `heal_album_phantoms` cleans up rows from pre-fix runs.
- `group_messages` is the **only** comment store (adr/0002): engagement queries
  filter `is_thread_root = 0`. `group_events` PK is `(id, user_id)` — one
  service message can carry several users.
- **A join count carries its source, or it is unreadable.** Without the admin
  log the counts are a *floor*, so `scan_group` records whether it read the log
  in `overview["admin_log"]` and `summarize_group` states it on the line
  carrying the numbers. **Absent is not `False`** there: a hand-built overview
  does not know.
- FTS5 external-content indexes (adr/0004) sync via `FTS_SCHEMA` triggers;
  `open_db` applies them best-effort (no FTS5 → search lost, scraping fine).
  `MATCH 'stem*'` is the canonical text search.

## Tests

`slop_writer.*` functions and their return shapes — never a script's stdout or
exit code. Assert on `SlopWriterError.code`, not on the message. Messages are
real Telethon TL objects, the client is hand-faked (`factories.FakeClient` is
the whole network boundary). Nothing in the suite touches a session,
`.tg-analytic/`, or the network; live runs stay the acceptance step. pytest
lives in a PEP 735 `dev` group, never an extra.

## Releases

**A release tag is `vMAJOR.MINOR.PATCH` — nothing else**, equal to `v` + the
`version` in `pyproject.toml`. **A bump is a two-file edit**: `pyproject.toml`
and `SKILL.md`'s `metadata.version`, byte-identical, three components — the
`npx skills add` channel never sees `pyproject.toml`, and
`tests/test_packaging.py` pins the copy to the source. `publish.yaml` gates on
tag-vs-`pyproject` agreement but strips the `v`, so the naming rule is a
convention the gate does not enforce. PyPI versions are immutable: check the tag
before publishing, not after.

## Dev CLIs (`tools/tg_*.py`)

`tg_scrape.py`, `tg_publish.py`, `tg_query.py` are PEP-723 typer scripts kept as
**dev tools only** — `login` needs a TTY, and they are the only way to drive
Telethon without an MCP client. They stay **undocumented**: nothing under
`skills/` names a script or a flag, and a `grep` is the acceptance check. Flags
are `--help`; keep it that way rather than restoring a reference file.

`--at` is ISO-8601 **with offset** (naive rejected); the now+1h floor is
`MIN_LEAD` in `publish.py` with no override, so the agent cannot schedule too
soon (adr/0003). `reschedule`/`edit` are `editMessage` with `schedule_date`, and
Telethon returns `None` for scheduled edits — both callers report from known
inputs. `--caption-above` rides an `invert_media` monkey patch.

## Gotchas

- Telethon TL types are generated dynamically, so Pyright flags `.sender`,
  `.chats`, `.full_chat`, `.forwards` as unknown attributes throughout —
  expected noise, not real errors.
- **mistune**, not Python-Markdown, for a post body: it keeps `#hashtag` lines
  literal instead of parsing them as `<h1>`. `markdown.py` walks its AST straight
  to Telethon `MessageEntity` objects (no HTML hop) and owns UTF-16 offsets.
- Wheel data ships `skills/` *beside* the package (`data = { purelib = "skills" }`)
  so `install` can copy it out; it cannot live under `src/slop_writer/`, since
  `npx skills add` serves the same directory from the repo root.
  `install.skill_source()` tries the source checkout **first** — an editable
  install materialises the data directory once and never refreshes it.
- Pins that carry a reason: `mcp>=1.10,<2` (`structured_output=False` is
  load-bearing and v2 moved `FastMCP`), `telethon>=1.36,<2` (client API, not the
  bot API).
