# Reading Telegram — `tg_scrape.py`

Every command below runs from the **project root**:

```
uv run <skill_dir>/scripts/tg_scrape.py <command> --channel @name [flags]
```

All of them need the session from `/setup-tg-analytic`. Results land in
`.tg-analytic/<channel>.db` (leading `@` stripped) and each run prints a
Markdown summary — paste that, don't paraphrase it.

`scrape`/`fetch`/`group` **append** a metrics row per run and upsert everything
else, so repeated runs build a time series rather than overwriting one.

## Choose the selection flag first — never default to `--limit`

`scrape` and `group` share four mutually exclusive selection modes. Pick one
from what the user actually said. **Default to `--latest`.**

| User said… | Flag | Why this one |
| --- | --- | --- |
| "latest 10", "newest 10", "last 10", "10 most recent" | `--latest 10` | The only flag that iterates **newest-first**. Use whenever the user counts posts from the present. |
| "posts from this week", "last 7 days", "since 2026-05-01" | `--offset-date DD-MM-YYYY` | Time-window framing. Compute the date locally; the boundary is **exclusive** (strictly after). |
| "posts after #1234", "resume", "incremental refresh" | `--offset-id 1234` | Cursor-based forward walk, **inclusive** of 1234. Read `MAX(id)` from the DB and pass it in. |
| Known ids: "post 226", "refresh 103, 105, 108" | `fetch 103 105 108` | One round-trip, no scan. Cheaper than `scrape --offset-id … --limit 1`. |
| First-ever scrape, "full history", "all posts" | *(no flag)* | Walks oldest→newest from message 1. Slow; run once per channel. |

`--limit N` is **not** a selection flag — it caps one of the above. Alone it
walks oldest-first from message 1 and stops after N, re-scraping ancient
history instead of returning recent posts. Use it only to bound a forward page
after an offset (`--offset-id 299 --limit 1` grabs one specific post).

## `scrape` — posts, comments, media, forwarders

```
# full history (first run on a channel)
uv run <skill_dir>/scripts/tg_scrape.py scrape --channel @name

# fast first look at an unfamiliar channel
uv run <skill_dir>/scripts/tg_scrape.py scrape --channel @name --latest 100 --no-media

# incremental refresh from the newest stored id
uv run <skill_dir>/scripts/tg_scrape.py scrape --channel @name --offset-id 1234
```

Zero posts scraped is a normal result for an incremental run whose window holds
nothing new — report it as "no new posts since #N", not as a failure. A wrong
handle cannot land here: it exits 1 with `Cannot resolve @name` before any DB
is created.

On very large channels Telethon may surface `FloodWaitError` mid-run; the
script logs it and continues per item where it can. If a run aborts, resume
forward with `--offset-id <last-seen-id>` instead of restarting.

An album (grouped media) is **one** post no matter how the window falls: when
the selection cuts through one, the script re-fetches the missing members by id
(`album … was cut by the window: pulled in …`) so the caption-carrying head
still owns the whole album. Any leftover extra post rows from before that fix
are removed on open, logged as `removed N phantom album post row(s)` — expected
maintenance output, not an error.

## `fetch` — refresh specific posts

```
uv run <skill_dir>/scripts/tg_scrape.py fetch 103 105 108 --channel @name
```

Appends a `post_metrics` row per id and replaces those posts' comments,
attachments, and shares. Missing ids are logged and skipped; album members
auto-group by `grouped_id` — passing any member id refreshes the whole album
under its head post, so the row you get back may carry a different id than the
one you asked for. To refresh metrics only — views, forwards,
reactions, comment counts, no comment bodies — the cheapest form is:

```
uv run <skill_dir>/scripts/tg_scrape.py fetch 103 105 108 --channel @name \
    --no-comments --no-media --no-channel-info
```

Pick the ids by querying the DB first (e.g. the recent high performers), then
pass them in.

## `group` — discussion-group analytics

```
# the channel's linked discussion group — threads join to posts,
# rows land in the CHANNEL's DB
uv run <skill_dir>/scripts/tg_scrape.py group --channel @name --latest 500

# a standalone group the account belongs to — own DB, no thread linkage
uv run <skill_dir>/scripts/tg_scrape.py group --group @name --latest 500
```

Pass **exactly one** of `--channel` / `--group`. For a group attached to a
channel you analyze, always use `--channel`: `--group` treats it as standalone
and writes to a separate DB divorced from the channel's posts. The script warns
(`is the discussion group of a channel … re-run with --channel`) when it detects
that — re-run as told. `… has no linked discussion group` means comments are
disabled on the channel, so only `--group` mode is possible.

Needs group membership, not admin rights. Writes three tables:
`group_messages` (every non-service message, comments included — the single
comment store, which `scrape` also writes to), `group_events` (joins/leaves),
and an append-only `group_metrics` member-count snapshot per run. See
[schema.md](schema.md). No media is downloaded from groups (`media_type` is
still recorded). Selection flags are the four above; refresh incrementally with
`--offset-id` from `MAX(id)` over `group_messages`.

Join/leave events come from **two sources**: service messages in the group
history (visible to any member) plus, when the account is an **admin** of the
group, the group's admin log — which records every membership change even when
Telegram suppresses or deletes the service messages (it does so wholesale
during join bursts, e.g. after a CTA post). The two are deduped automatically.
**The admin log only retains ~48 hours**, so run `group` at least every 2 days
to keep the join series complete; without admin rights the command logs a
notice and falls back to service messages alone.

Completeness caveat: without admin rights the event counts are what the scan
*found*, not the truth. Cross-check the `group_metrics.members` trend before
claiming totals — Telegram's own member count can lag a burst by hours.

The summary prints joins/leaves by mechanism and by day, an hour-of-day
activity table (joins / messages / unique authors, in the **machine-local**
timezone — it is labeled; don't re-report those hours as UTC), every thread
touched in the window (replies, unique commenters, time-to-first-reply), and
top contributors. CTA attribution ("did post #X's invite work?") is
deliberately not pre-computed — use the canonical query in [schema.md](schema.md)
with the user's chosen window.

## `subscribers` — audience growth and churn

```
uv run <skill_dir>/scripts/tg_scrape.py subscribers --channel @name
```

Prints the date range, current total, net change, joins/leaves, daily averages,
best/worst day, and new subscribers by source. Upserts into `subscribers`
(date|total|joins|leaves) and `subscriber_sources` (date|source|count), so
repeated runs accumulate history beyond Telegram's own retention window — the
period Telegram returns is already the maximum it offers, so schedule the
command periodically to build a longer series.

## `views` — best hour to post

```
uv run <skill_dir>/scripts/tg_scrape.py views --channel @name
```

Prints views per hour of day (0–23): peak hours, quietest hours, full 24-hour
breakdown. Console only, nothing persisted. Hours are in the **Telegram
account's local timezone** — that is what the stats API returns and there is no
offset to convert from, so report them as e.g. "20:00 local time (channel
admin's tz)".

Both `subscribers` and `views` require the account to be an **admin** of the
channel, and the channel to be stats-eligible (~500+ subscribers). Otherwise
they exit 1 with `you must be an admin of a channel that is large enough` —
no retry helps; fall back to `scrape` plus `post_metrics` for engagement
signals. `no followers graph available` / `no top-hours graph available` means
stats exist but that particular graph is empty; report it as such.

## `scheduled`

Lists not-yet-published posts; documented with the write commands in
[publishing.md](publishing.md), since its `sched-msg` ids are what
`reschedule`/`edit` take.
