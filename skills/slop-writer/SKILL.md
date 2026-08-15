---
name: slop-writer
description: >-
  Telegram channel analytics and scheduled publishing, through the `slop-writer` MCP server's tools. Covers posts, comments, engagement over time and forwarder networks; subscriber growth and churn by source; views by hour of day; a discussion group's threads and join/leave events; and the write path — queue, retime or rewrite a future post. Read this before calling any `slop-writer` tool: the tools say how to call them, this says which one answers the question and what its numbers mean. Not for reading one specific message, and not for chats the logged-in account cannot see.
compatibility: >-
  The `slop-writer` MCP server, connected to this project. It holds the Telegram session and the per-channel databases; the user sets both up once with `slop-writer init` in their own terminal.
license: Apache-2.0
metadata:
  author: Lancetnik
  version: "0.4.1"
---

# Telegram channel analytics

Every operation is a `slop-writer` tool call. The tools read Telegram, store
what they read in a per-channel SQLite database, and answer questions out of
that database. Each tool description carries its own arguments; this file
carries the two things a description cannot — **which tool** answers the
question, and **what the answer means**.

## Pick the tool

| The user wants | Tool | Read first |
| --- | --- | --- |
| the first look at a channel, or fresh data across a range of posts | `scrape_posts` | — |
| fresher numbers for posts whose ids you already have | `refresh_posts` | — |
| the discussion under a channel's posts — comments, threads, joins and leaves | `scan_linked_group` | [analysis.md](references/analysis.md) |
| a group that is nobody's comment section | `scan_standalone_group` | [analysis.md](references/analysis.md) |
| where subscribers came from, and how many left | `fetch_subscribers` | [analysis.md](references/analysis.md) |
| the best hour of day to publish | `fetch_views_by_hour` | [analysis.md](references/analysis.md) |
| a number, a ranking, a comparison, a text search | `run_query` | [analysis.md](references/analysis.md), then [schema.md](references/schema.md) |
| to see what is already queued to publish | `list_scheduled` | [publishing.md](references/publishing.md) |
| to queue, retime or rewrite a future post | `publish_schedule`, `publish_reschedule`, `publish_edit` | [publishing.md](references/publishing.md), then [markup.md](references/markup.md) |

Three pairs are easy to swap, and picking wrong is quiet rather than loud:

- **`scrape_posts` vs `refresh_posts`.** Scraping walks the channel's history;
  refreshing takes ids and makes one round trip. If a query or a group scan
  already handed you the ids, refresh — walking history to reach four known
  posts costs minutes and pulls hundreds of rows nobody asked for.
- **`scan_linked_group` vs `scan_standalone_group`.** The comments under a
  channel's posts **are** that channel's linked group. Use
  `scan_linked_group`, naming the channel, whenever the group belongs to a
  channel you analyse. The standalone tool puts the same messages in a
  separate database with no link back to the posts — every per-post comment
  metric silently becomes unanswerable, and nothing errors.
- **`fetch_subscribers` vs `fetch_views_by_hour`.** "How many, and from where"
  against "when". Both need admin rights on the channel; neither is a
  substitute for the other, and neither is about posts.

Reach for `run_query` for anything a tool's own summary did not already
answer. It is the general instrument — the scans exist to feed it.

## Four invariants that break an answer silently

Each one still returns a plausible number when you get it wrong.

**Scrape before you query.** `run_query` reads the local database and never
Telegram. A channel nobody has scraped answers "no such table" or zero rows —
that is a missing scrape, never an empty channel. Scan first, then query, and
re-scan when the user asks about anything newer than the last run.

**Newest-first is not the default of history, only of the usual case.** Asking
for the N most recent posts is what "latest 10", "this week's posts" and
"how are we doing lately" all mean, and it is what to reach for unless the
user is deliberately paging through old history. A window walk runs the other
way — oldest-first from its offset — so a bounded window over an unscraped
channel returns its oldest posts, which look like data and are the wrong
posts.

**One album is one post.** Telegram reports views and forwards on every member
of a grouped-media post, but only the caption-carrying member is a post here.
A scan whose window cuts through an album re-fetches the missing members so
the album stays whole — expect log lines about pulled-in members and about
removed phantom rows, and read both as maintenance, not failure. Never sum a
metric per album member.

**Metrics are append-only snapshots.** Every scan appends a row per post
rather than overwriting one, which is what makes change over time answerable.
The latest snapshot is the one with the highest row id — *not* the latest
timestamp, because two runs in the same second tie. `references/schema.md`
carries the canonical query; use it rather than writing the join fresh.

## When setup is missing

A tool that fails with a setup error tells you what the user must run. It is
an interactive login on a terminal — Telegram prompts for an SMS code — so you
cannot complete it from a tool call. Relay the instruction, stop, and wait.

## When no channel is named

Ask the user which channel they mean, and record the answer wherever your
client keeps notes across sessions, so it is asked once rather than every
session. `CANNOT_RESOLVE` names the channels this project has data for —
retry with one of those.

## The write gate

`publish_*` reaches a live channel. Run one only on an explicit instruction
from the user. Their client prompts on every call with the exact body and
time, and that prompt is the agreement; report what you queued once it lands.

## Reporting back

Every tool returns a Markdown summary that already pre-computes the
most-asked questions. Report it:

1. A one-line headline — channel, window, headline metric.
2. The tool's summary, pasted rather than paraphrased.
3. A `run_query` table underneath, only where the user asked for something the
   summary does not cover.

Before calling anything a repost, check the direction against the cheat-sheet
in [schema.md](references/schema.md): "other channels re-shared your post"
(the channel's reach) and "this channel forwarded someone else's post" (not
the channel's own content) live in different tables and read alike. Name the
direction explicitly when you report it.
