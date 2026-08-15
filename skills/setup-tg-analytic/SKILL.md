---
name: setup-tg-analytic
description: Set up Telegram credentials and the login session that tg-analytic-skill needs. Run once per project, before asking Claude to analyze a channel.
disable-model-invocation: true
license: Apache-2.0
metadata:
  author: Lancetnik
  version: "1.0"
---

# Set up tg-analytic-skill

Two things live in `.tg-analytic/` at the **project root** and gate every
Telegram-facing command: a `.env` with API credentials, and a `session.session`
from a one-time login. This skill produces both. The login itself is the
user's to run — Telethon prompts for an SMS code on stdin, which only works in
their own terminal.

Everything below runs from the project root: the scripts anchor
`.tg-analytic/` on the current working directory.

## 1. Find the skill and take stock

Locate the installed `tg-analytic-skill` directory — `.claude/skills/`,
`.agents/skills/`, `~/.claude/skills/`, or `skills/` in the source repo — and
call it `<skill_dir>`. If it isn't installed, stop and tell the user to install
it first (`npx skills@latest add Lancetnik/tg-analytic-skill --skill
tg-analytic-skill`); this skill configures it and does nothing on its own.

Then check what already exists: `.tg-analytic/.env`, `.tg-analytic/session.session`,
and whether the project is a git repo whose `.gitignore` covers `.tg-analytic/`.
Report the state before changing anything — a populated `.env` is never
overwritten without the user saying so.

## 2. Collect the credentials

Ask the user for three values, pointing them at https://my.telegram.org/apps to
create an application if they have none:

- `TG_API_ID` — the numeric app id
- `TG_API_HASH` — the app hash
- `TG_PHONE` — the account's phone in international format, e.g. `+15551234567`

These are the credentials of a real Telegram **account** (not a bot), and the
account is what the analytics see: it needs to be a member of any group you
want to scan, and an admin of the channel for `subscribers`/`views`.

## 3. Write `.tg-analytic/.env`

Create the directory and the file:

```
TG_API_ID=<id>
TG_API_HASH=<hash>
TG_PHONE=<+phone>
```

The session file that follows is a **credential** — anyone holding it is logged
into that Telegram account. If the project is a git repo and `.gitignore`
doesn't already cover it, add a `.tg-analytic/` line before going further.

## 4. Hand the login to the user

Tell the user to run this **in their own terminal**, from the project root, and
to come back when it finishes:

```
uv run <skill_dir>/scripts/tg_scrape.py login
```

Substitute the real path. Telethon prompts for an SMS code, and for a 2FA
password if the account has one — an interactive TTY, so never attempt this
through a tool call yourself; it deadlocks on the prompt.

## 5. Confirm

When the user reports back, check that `.tg-analytic/session.session` exists.
If it does, setup is done: say so, and tell them the analytics skill is now
usable ("analyze @channel", "who's forwarding @channel", "schedule this post
for Friday"). Re-running this skill is only needed to switch accounts.

If the file is missing, the login didn't complete — ask for the error they saw
and work from that. The usual causes are a wrong `TG_API_ID`/`TG_API_HASH`
pair (Telethon reports an invalid API id), a phone in the wrong format, or the
command being run from a different directory than the project root.
