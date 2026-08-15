---
name: slop-writer
description: >-
  Analyze a Telegram channel with the bundled CLIs: posts, comments, engagement over time and forwarder networks; subscriber growth and churn by source; views by hour of day; the linked discussion group's threads and join/leave events. Also the write path — schedule, reschedule, or edit a future post, optionally with photos. Not for reading one specific message, and not for chats the logged-in account cannot see.
compatibility: >-
  Python >=3.10 with uv (PEP-723 deps install on first run) and network access to Telegram's API. Credentials and the Telegram session come from `slop-writer init`, which the user runs once per project in their own terminal.
license: Apache-2.0
metadata:
  author: Lancetnik
  version: "2.1"
---

# Telegram channel analytics

Three bundled CLIs over one Telegram channel: read from Telegram → store in a per-channel SQLite DB → report. Publishing scheduled posts is the only write path.

Run every command from the **project root**: the scripts anchor `.tg-analytic/` — session, one DB per channel, downloaded media — on the current working directory, never on the skill directory, which stays read-only. `<skill_dir>` in the references means the directory holding this SKILL.md; resolve it once and reuse it.

## Pick the branch

| The user wants | Read first | CLI |
| --- | --- | --- |
| posts, comments, engagement, forwarders, subscriber growth, best hour to post, discussion-group activity | [references/scraping.md](references/scraping.md) | `tg_scrape.py` |
| a number, a ranking, a text search — anything the printed summary didn't answer | [references/querying.md](references/querying.md) | `tg_query.py` |
| to schedule, retime, or rewrite a future post | [references/publishing.md](references/publishing.md) | `tg_publish.py` |

Publishing reaches a live channel, so it runs on an explicit instruction from the user and on their confirmation of the exact body and time.

Read the branch's reference before running its CLI: each one carries a flag
that quietly does the wrong thing when guessed — `scrape --limit` walks history
from message 1, `--at` without a UTC offset is rejected, engagement queries
need `is_thread_root = 0`.

Shape of every invocation:

```
uv run <skill_dir>/scripts/tg_scrape.py scrape --channel @name --latest 100
```

## Reporting back

Every command prints a Markdown summary block to stdout, pre-computing the
most-asked questions. Report it as-is:

1. A one-line headline — channel, window, headline metric.
2. The script's summary, pasted verbatim rather than paraphrased.
3. A `tg_query.py` table underneath, only when the user asked for something the
   summary doesn't cover.

Before calling anything a repost, check the direction against the cheat-sheet
in [references/schema.md](references/schema.md): "other channels re-shared your
post" (the user's reach) and "the channel forwarded someone else's post" (not
the user's own content) live in different tables and are easy to swap. Name the
direction explicitly when you report it.
