# slop-writer

A [Claude Code](https://claude.com/claude-code) skill that analyzes a Telegram
channel:

- Scrape posts (text, media, reactions, views, forwards, tags) and the comment
  threads under each.
- Track **outward forwarders** — outside channels that re-share your posts —
  and **inward citations** — channels you forwarded from.
- Build an append-only time-series of per-post metrics on every re-run, so you can watch engagement evolve.
- Pull subscriber growth/churn broken down by acquisition source and views by hour of day for the "best time to post" question.
- Schedule, retime, and rewrite future posts — the one write path.

Backed by Telethon, with a separate read-only SQL CLI for querying the local
SQLite DB (one file per channel).

> This is my personal skill — I built it to manage my own channel,
> [**@fastnewsdev**](https://t.me/fastnewsdev). It's shared here in case the
> patterns are useful to someone else running a Telegram channel

> **Renamed from `tg-analytic-skill`.** The analytics are being repackaged as a
> PyPI distribution driving an MCP server, and the new name leaves room for
> media beyond Telegram. The setup below is the new one; links to the old
> repository path still redirect.

## Install

Two commands, run from the project where you want the analytics.

```bash
uv tool install slop-writer
slop-writer install     # wires Claude Code: MCP server, permissions, the skill
slop-writer init        # Telegram credentials and the one-time login
```

`install` writes three things and nothing else: the `slop-writer` entry in
`.mcp.json`, a permission block in `.claude/settings.json` (reads allowed,
publishing behind a prompt), and the skill into `.claude/skills/slop-writer/`.
The `.mcp.json` entry holds no machine-specific path, so it is safe to commit —
a teammate clones, runs `slop-writer init`, and is done. **Restart your MCP
client afterwards**: `.mcp.json` is read at session start only.

Verified on Claude Code. For Cursor, Codex or the Copilot coding agent,
`install` prints the entry for you to paste into their config yourself.

`init` asks for your Telegram API credentials
([create them here](https://my.telegram.org/apps)), writes `.tg-analytic/.env`,
and runs the login. Run it **in your own terminal** — Telethon prompts for the
SMS code and 2FA password on stdin, so it cannot go through a tool call. It is
additive and safe to re-run: it prompts only for what is missing and logs in
only when there is no working session. The two commands are independent and
work in either order.

`slop-writer uninstall` removes exactly what `install` wrote. It never touches
`.tg-analytic/`.

The skill alone, without the server, still installs through the
[`skills`](https://dev.to/baltz/sharing-skills-with-npx-2nbc) CLI:

```bash
npx skills@latest add Lancetnik/slop-writer
```

Both channels serve the same directory, so whichever runs last wins on disk.

All runtime state — `.env`, the session, per-channel `*.db` files, downloaded
media — lives in `.tg-analytic/` at your **project root**, never inside the
skill. `init` gitignores it for you: the session file is a live login to your
Telegram account.

## Usage

Inside Claude Code, ask in plain language:

> Analyze @some_channel — show me the most popular posts.
> Who's forwarding @some_channel? Which channels do they cite most often?
> Refresh metrics on the last 20 posts of @some_channel.
> How did the discussion group grow after Tuesday's post?
> Schedule this draft for Friday at 18:00.

The skill picks the command, runs it from the project root, and pastes back the
Markdown summary each command prints (top posts by views and reactions, top
tags, outward forwarders and inward citations with their post ids) — usually
the answer is right there without dropping into SQL.

### Keeping the metric history usable

`post_metrics` is a time series, but only of the moments you actually scraped — it cannot be backfilled. A routine `scrape --latest N` re-measures recent posts only, so an older post can sit on the single snapshot it got on its first day forever, and any "how did this post do over time" question then has nothing to answer from. Every so often, run `fetch` over a spread of older ids as well:

```bash
uv run "$SKILL/scripts/tg_scrape.py" fetch --channel @yourchannel 340 345 350 355
```

Views in particular keep climbing for months, so a post's snapshots are worth collecting long after it stops feeling current.

To drive the CLIs by hand, the full command reference lives in the skill:

- [`references/scraping.md`](./skills/slop-writer/references/scraping.md) — `tg_scrape.py`: `scrape`, `fetch`, `group`, `subscribers`, `views`
- [`references/querying.md`](./skills/slop-writer/references/querying.md) — `tg_query.py` and full-text search, over [`references/schema.md`](./skills/slop-writer/references/schema.md)
- [`references/publishing.md`](./skills/slop-writer/references/publishing.md) — `tg_publish.py`: `schedule`, `reschedule`, `edit`, plus [`references/markup.md`](./skills/slop-writer/references/markup.md)

## Repository layout

```
skills/
  slop-writer/        Shipped both ways: packaged into the wheel (so
                      `slop-writer install` can copy it out) and served to
                      `npx skills add` from here. One directory, one version.
    SKILL.md          Skill instructions: the branch map, not the flags.
    scripts/
      tg_scrape.py           Telethon read CLI (scrape, fetch, group, subscribers, views, scheduled).
      tg_publish.py          Telethon write CLI (schedule, reschedule, edit).
      tg_query.py            Read-only SQL CLI.
    references/
      scraping.md     tg_scrape.py commands and selection flags.
      querying.md     tg_query.py usage and search patterns.
      publishing.md   tg_publish.py commands and scheduling rules.
      schema.md       DB schema reference for writing SQL.
      markup.md       Supported Markdown -> Telegram markup for tg_publish.
src/
  slop_writer/        The library the CLIs import, published to PyPI. It holds
                      the domain logic; the three scripts above are argument
                      parsing and output rendering over it. They declare
                      `slop-writer` in their PEP-723 headers, so `uv run`
                      fetches it — nothing is vendored into the skill directory.
    db.py             DB schema (source of truth), paths, open helpers. Stdlib only.
    errors.py         SlopWriterError/UsageError — what the domain raises. Stdlib only.
    query.py          Read-only SQL guards and execution. Stdlib only.
    tg.py             Telethon session/credential plumbing.
    messages.py       Telethon Message -> plain fields (media, reactions, albums).
    scrape.py         The post pipeline: scrape_posts / refresh_posts.
    group.py          Discussion group: classification, thread linkage, scan_group.
    stats.py          Broadcast stats: subscribers, views by hour.
    scheduled.py      Reading the scheduled queue.
    publish.py        The write surface: schedule / reschedule / edit.
    render.py         Markdown renderers for the per-command summaries.
    markdown.py       Markdown -> Telethon MessageEntity (publish only).
    server.py         The MCP server and its read tools.
    install.py        `install`/`uninstall`: the agent wiring. No Telegram.
    init.py           `init`: credentials, gitignore, the TTY login. No MCP.
    cli.py            The `slop-writer` console script (argparse).
tools/
  check_schema_doc.py  Dev-only: guard SCHEMA <-> references/schema.md drift (not
                       shipped). Pinned to this checkout, not to the released
                       package, so it guards the working tree.
```

Runtime state (`.env`, the Telethon session, per-channel `*.db` files, media)
lives in a gitignored `.tg-analytic/` directory at the root of whatever
project you run the skill from — nothing is written inside the skill itself.
