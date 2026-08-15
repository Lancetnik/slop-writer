<picture>
  <source media="(prefers-color-scheme: dark)"
          srcset="https://raw.githubusercontent.com/Lancetnik/slop-writer/main/docs/assets/logo-dark.svg">
  <img src="https://raw.githubusercontent.com/Lancetnik/slop-writer/main/docs/assets/logo.svg"
       alt="slop-writer" width="260">
</picture>

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
> media beyond Telegram. Nothing has moved yet — the skill below works exactly
> as before, and links to the old repository path still redirect.

## Install

From any project where you want the skill available to Claude Code, use the
[`skills`](https://dev.to/baltz/sharing-skills-with-npx-2nbc) CLI:

```bash
npx skills@latest add Lancetnik/slop-writer
```

## First-run setup

Run `/setup-tg-analytic` in Claude Code, once per project. It asks for your
Telegram API credentials ([create them here](https://my.telegram.org/apps)),
writes `.tg-analytic/.env`, and walks you through the one-time login — which
you run **in your own terminal**, since Telethon prompts for the SMS code and
2FA password on stdin:

```bash
SKILL=.claude/skills/tg-analytic-skill   # adjust to your install
uv run "$SKILL/scripts/tg_scrape.py" login
```

That writes `.tg-analytic/session.session`; every later command reuses it.

All runtime state — `.env`, the session, per-channel `*.db` files, downloaded
media — lives in `.tg-analytic/` at your **project root** (the directory you
run commands from), never inside the skill. Keep it out of git: the session
file is a live login to your Telegram account.

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

- [`references/scraping.md`](./skills/tg-analytic-skill/references/scraping.md) — `tg_scrape.py`: `scrape`, `fetch`, `group`, `subscribers`, `views`
- [`references/querying.md`](./skills/tg-analytic-skill/references/querying.md) — `tg_query.py` and full-text search, over [`references/schema.md`](./skills/tg-analytic-skill/references/schema.md)
- [`references/publishing.md`](./skills/tg-analytic-skill/references/publishing.md) — `tg_publish.py`: `schedule`, `reschedule`, `edit`, plus [`references/markup.md`](./skills/tg-analytic-skill/references/markup.md)

## Repository layout

```
skills/
  tg-analytic-skill/
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
  setup-tg-analytic/
    SKILL.md          One-time credential + login setup, run by the user.
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
tools/
  check_schema_doc.py  Dev-only: guard SCHEMA <-> references/schema.md drift (not
                       shipped). Pinned to this checkout, not to the released
                       package, so it guards the working tree.
```

Runtime state (`.env`, the Telethon session, per-channel `*.db` files, media)
lives in a gitignored `.tg-analytic/` directory at the root of whatever
project you run the skill from — nothing is written inside the skill itself.
