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
- Build an append-only time-series of per-post metrics on every re-run, so you
  can watch engagement evolve.
- Pull subscriber growth/churn by acquisition source, and views by hour of day
  for the "best time to post" question.
- Schedule, retime, and rewrite future posts — the one write path.

Backed by Telethon. Everything reaches the agent as **MCP tools** — eleven of
them, over a local SQLite DB (one file per channel) that a read-only SQL tool
answers questions from.

> This is my personal skill — I built it to manage my own channel,
> [**@fastnewsdev**](https://t.me/fastnewsdev). It's shared here in case the
> patterns are useful to someone else running a Telegram channel.

## Install

Two commands, run from the project where you want the analytics.

```bash
uv tool install slop-writer
slop-writer install     # wires Claude Code: MCP server, permissions, the skill
slop-writer init        # Telegram credentials and the one-time login
```

`install` writes the `slop-writer` entry in `.mcp.json`, a permission block in
`.claude/settings.json` (reads allowed, publishing behind a prompt), and the
skill into `.claude/skills/slop-writer/`; everything it writes is printed. The
`.mcp.json` entry holds no machine-specific path, so it is safe to commit — a
teammate clones, runs `slop-writer init`, and is done. **Restart your MCP client
afterwards**: `.mcp.json` is read at session start only.

Verified on Claude Code. For Cursor, Codex or the Copilot coding agent,
`install` prints the entry for you to paste into their config yourself.

`init` asks for your Telegram API credentials
([create them here](https://my.telegram.org/apps)), writes `.tg-analytic/.env`,
and runs the login. Run it **in your own terminal** — Telethon prompts for the
SMS code and 2FA password on stdin, so it cannot go through a tool call. It is
additive and safe to re-run: it prompts only for what is missing and logs in
only when there is no working session. The two commands are independent and work
in either order.

`slop-writer uninstall` removes exactly what `install` wrote, and never touches
`.tg-analytic/`.

The skill alone, without the server, still installs through the
[`skills`](https://dev.to/baltz/sharing-skills-with-npx-2nbc) CLI:

```bash
npx skills@latest add Lancetnik/slop-writer
```

Both channels serve the same directory, so whichever runs last wins on disk.

All runtime state — `.env`, the Telethon session, per-channel `*.db` files,
downloaded media — lives in a gitignored `.tg-analytic/` at your **project
root**, never inside the skill. `init` gitignores it for you: the session file
is a live login to your Telegram account.

## Usage

Inside Claude Code, ask in plain language:

> Analyze @some_channel — show me the most popular posts.
> Who's forwarding @some_channel? Which channels do they cite most often?
> Refresh metrics on the last 20 posts of @some_channel.
> How did the discussion group grow after Tuesday's post?
> Schedule this draft for Friday at 18:00.

The skill picks the tool, and the tool returns a Markdown summary the agent
pastes back (top posts by views and reactions, top tags, outward forwarders and
inward citations with their post ids) — usually the answer is right there
without dropping into SQL.

### Keeping the metric history usable

`post_metrics` is a time series, but only of the moments you actually scraped —
it cannot be backfilled. A routine scrape of the latest N posts re-measures
recent posts only, so an older post can sit forever on the single snapshot it
got on its first day, and "how did this post do over time" then has nothing to
answer from. Every so often, ask for a refresh over a spread of older ids too:

> Refresh metrics on posts 340, 345, 350 and 355 of @yourchannel.

Views in particular keep climbing for months, so a post's snapshots are worth
collecting long after it stops feeling current.

## What the agent reads

- [`SKILL.md`](./skills/slop-writer/SKILL.md) — which tool answers which question, and the four invariants that break an answer silently
- [`references/analysis.md`](./skills/slop-writer/references/analysis.md) — what the numbers mean and how they mislead, over [`references/schema.md`](./skills/slop-writer/references/schema.md)
- [`references/publishing.md`](./skills/slop-writer/references/publishing.md) — the write discipline and queue semantics, plus [`references/markup.md`](./skills/slop-writer/references/markup.md)

## Contributing

`skills/slop-writer/` is the shipped skill (packaged into the wheel *and* served
to `npx skills add` from here — one directory, one version). `src/slop_writer/`
is the library and the MCP server it drives; `tools/` holds dev-only scripts
that are never shipped. [`CLAUDE.md`](./CLAUDE.md) has the working rules,
[`CONTEXT.md`](./CONTEXT.md) the vocabulary, and [`docs/adr/`](./docs/adr/) the
decisions behind both.
