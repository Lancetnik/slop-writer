# tg-analytic-skill

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

## Install

From any project where you want the skill available to Claude Code, use the
[`skills`](https://dev.to/baltz/sharing-skills-with-npx-2nbc) CLI:

```bash
npx skills@latest add Lancetnik/tg-analytic-skill
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
      tg_query.py            Stdlib-only read-only SQL CLI.
      utils/                 Support package imported by the CLIs as `utils.*`:
        _common.py             Shared paths, DB schema (source of truth), open helpers.
        _tg.py                 Telethon session/credential plumbing.
        _render.py             Markdown renderers for the per-command summaries.
        _md2entities.py        Markdown -> Telethon MessageEntity (tg_publish).
        _group.py              Discussion-group classification helpers.
    references/
      scraping.md     tg_scrape.py commands and selection flags.
      querying.md     tg_query.py usage and search patterns.
      publishing.md   tg_publish.py commands and scheduling rules.
      schema.md       DB schema reference for writing SQL.
      markup.md       Supported Markdown -> Telegram markup for tg_publish.
  setup-tg-analytic/
    SKILL.md          One-time credential + login setup, run by the user.
tools/
  check_schema_doc.py  Dev-only: guard SCHEMA <-> references/schema.md drift (not shipped).
```

Runtime state (`.env`, the Telethon session, per-channel `*.db` files, media)
lives in a gitignored `.tg-analytic/` directory at the root of whatever
project you run the skill from — nothing is written inside the skill itself.
