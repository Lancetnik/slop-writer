# Querying the DB — `tg_query.py`

Read [schema.md](schema.md) before writing SQL: it documents every table and
primary key, the repost-direction cheat-sheet, the full-text-search patterns,
and the canonical joins (latest metric per post, re-shares of your posts,
repost sources, album items).

```
uv run <skill_dir>/scripts/tg_query.py --channel @name \
  "SELECT p.id, p.link, m.views FROM posts p JOIN post_metrics m ON p.id = m.post_id ORDER BY m.views DESC LIMIT 10"
```

Read-only (SQLite `mode=ro`, writes rejected by the engine) and
dependency-free: no credentials, no session, no network. Output is a Markdown
table, ready to paste. `--limit N` caps rows (default 100, `0` = unlimited);
`--no-truncate` shows full cell content for post bodies and long comments.

Reach for it whenever the user asks for something the command summaries don't
already answer.

## The invariants that break queries silently

Each one is a clause you have to write. Omit it and the query still runs, and still returns a number.

- `post_metrics` is **append-only** — one row per post per scrape run. The
  latest snapshot is `MAX(id)`, never `MAX(scrape_date)` (runs within the same second tie). schema.md carries the canonical CTE.
- `group_messages` holds comments **and** thread roots. Engagement queries
  filter `is_thread_root = 0`, or every thread's root inflates the comment counts.
- `posts` holds the channel's forwards of other channels' content alongside 
  its own posts. Channel performance — every average, rate and ranking — filters `p.forwarder_from_channel IS NULL`: a forwarded post carries the *source* channel's pull in its counters.
- Count people with `COUNT(DISTINCT user_id)`. `author` is the readable name, for display and `GROUP BY`; `user_id` is the identity.

## Every metric is a snapshot

`post_metrics` holds what a scrape run saw, and its counters are cumulative:
`views` is the total a post had reached at that moment, not a property of the post. Four consequences.

**Views grow without a freeze point.** A post keeps collecting views for as
long as readers scroll past it on their way to newer posts — posts over a year old still gain them. Compare raw `views` between posts of a similar age; across ages compare rates (`forwards/views`, `reactions/views`) instead. Every rate is tied to the snapshot it came from, so quote it with that date.

**Posting cadence inflates views on its own.** That same scroll traffic is what one post lends the ones before it, so a denser run of posts lifts their view counts with no extra readers involved. Compare cadences on the metrics scroll traffic leaves alone: total forwards, total reactions, unique commenters, net subscriber growth.

**Metrics settle at different speeds.** Reactions and comments reach their
final value within about two days. Forwards keep arriving well past that.
Views never settle at all. So a "final" figure is only meaningful with the
post's age attached, and a young post's forwards are a floor, not a result.

**The series holds exactly what was scraped.** A snapshot missed is a snapshot gone — `post_metrics` cannot be filled in retroactively, and a post with a single row supports a total, never a trend. Check the row count per post before reporting change over time.

## Who is who in the group

Identities in `group_messages` are independent of the channel handle: the
channel's author comments under a personal account, and bots and service
accounts read as ordinary members. Inventory them before aggregating:

```sql
SELECT user_id, author, COUNT(*) AS n FROM group_messages
WHERE is_thread_root = 0 GROUP BY user_id ORDER BY n DESC LIMIT 20;
```

Ask the user which ids are the author, staff or bots, then exclude those ids
from "unique commenters" and "top contributors".

## Text search

For "where did I write about X" / "what did commenters say about Y", use the
FTS5 indexes — `posts_fts` and `gm_fts` with `MATCH` and `bm25()` ranking —
rather than `LIKE` over `text`. Always search by `stem*` prefix: the tokenizer
is `unicode61` with no stemmer, so `MATCH 'релиз'` misses `релизе`. The
*Full-text search* section of schema.md has the canonical queries.

`no such table: posts_fts` / `gm_fts` means the DB predates the search index,
or this SQLite build lacks FTS5. Run any `scrape`/`fetch`/`group` command to
migrate the DB; until then fall back to `LIKE '%…%'`.

## When a query fails

On `no such column` / `no such table`, the error output lists every table with
its actual columns. Rewrite the query from that listing instead of guessing
again.
