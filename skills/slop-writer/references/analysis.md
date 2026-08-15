# Reading the numbers

What the scraped data means, and the ways it misleads. Read this before
answering an analytics question; read [schema.md](schema.md) before writing
the SQL that answers it — that file has every table, every primary key, and
the canonical joins for the patterns below.

## Four clauses that decide whether a number is true

Each is a filter you have to write. Omit it and the query still runs, and
still returns a number.

- **Comments are not every row in the group.** `group_messages` holds the
  discussion group's messages *and* the thread roots Telegram creates when a
  post is forwarded into it. Engagement figures filter `is_thread_root = 0`,
  or every thread's root counts as a comment.
- **A forwarded post carries someone else's pull.** `posts` holds the
  channel's forwards of other channels' content next to its own posts. Every
  average, rate and ranking about *this channel's* performance filters
  `forwarder_from_channel IS NULL`; the counters on a forward measure the
  source.
- **Count people by id.** `COUNT(DISTINCT user_id)`. `author` is the display
  name — good for `GROUP BY` output and headings, wrong as an identity: people
  rename themselves and two accounts can share a name.
- **The latest snapshot is the highest row id.** `post_metrics` appends rather
  than overwrites, and `MAX(scrape_date)` ties between runs in the same
  second. schema.md has the CTE.

## Every metric is a cumulative snapshot

`post_metrics` holds what one scan saw, and its counters are running totals:
`views` is what a post had reached at that moment, not a property of the post.
Five consequences.

**Views grow with no freeze point.** A post keeps collecting views for as long
as readers scroll past it on the way to newer posts — posts over a year old
still gain them. Compare raw views only between posts of a similar age; across
ages compare rates (forwards per view, reactions per view) instead. A rate
belongs to the snapshot it came from, so quote it with that date.

**Posting cadence inflates views on its own.** That same scroll traffic is
what a post lends the ones before it, so a denser run of posts lifts their
view counts with no extra readers involved. Compare cadences on the metrics
scroll traffic leaves alone: total forwards, total reactions, unique
commenters, net subscriber growth.

**Metrics settle at different speeds.** Reactions and comments reach their
final value within about two days. Forwards keep arriving well past that.
Views never settle at all. A "final" figure is only meaningful with the post's
age attached, and a young post's forwards are a floor, not a result.

**The series holds exactly what was scanned.** A snapshot missed is a snapshot
gone; nothing can be filled in retroactively. A post with one metrics row
supports a total, never a trend — check the row count per post before
reporting change over time.

**A ranking compares snapshots, not posts.** Each row was measured when its
post was last scraped, and the staler row is the *smaller* one: views kept
arriving after it was taken. Two posts of identical age therefore rank by
scrape recency rather than by performance — a separate effect from age. Run
`refresh_posts` over a window before ranking it by a rate, or two runs of one
question return different winners.

## Who is who in the group

Identities in `group_messages` are independent of the channel handle: the
channel's author comments from a personal account, and bots and service
accounts read as ordinary members. Inventory them before aggregating —
`user_id`, `author` and a message count, thread roots excluded, ordered by
volume. Then ask the user which ids are the author, staff or bots, and exclude
those ids from "unique commenters" and "top contributors" rather than
guessing.

## What a group scan can and cannot see

Joins and leaves arrive from two sources: service messages in the group's
history, which any member sees, and — only when the account is an **admin** of
the group — the group's admin log. The admin log matters because Telegram
suppresses service messages wholesale during a join burst, which is exactly
when a CTA post is working. The two sources are deduped for you.

**The admin log retains about 48 hours.** A group scan run less often than
every two days has permanent holes in its join series, and nothing later can
fill them. Without admin rights there is no admin log at all, and the event
counts are what the scan *found*, not what happened — cross-check the
`group_metrics` member trend before claiming a total, remembering that
Telegram's own member count can lag a burst by hours.

A thread the scan reports with no post behind it is normal: the post is newer
than the last channel scrape. `refresh_posts` on that id fills in its date and
snippet.

Two questions the scan summary deliberately leaves to a query, because both
need a window one run does not have: whether a specific post's call to action
brought people in, and the group's hour-of-day activity profile. Both come out
of the accumulated database; schema.md carries a query for each.

## Audience and timing

`fetch_subscribers` accumulates. The period Telegram returns is already the
maximum it retains, so each run extends a series that outlives Telegram's own
window — which makes a regular cadence worth more than any single run, and
makes "we only have N days" a fact about when scanning started.

`fetch_views_by_hour` reports hours in the **Telegram account's local
timezone**. That is what the stats API returns and it comes with no offset to
convert from, so report it as such — "20:00 in the channel admin's timezone",
never as UTC and never silently converted to the user's own.

## Finding text

For "where did I write about X" or "what did commenters say about Y", use the
full-text indexes rather than a `LIKE` scan; schema.md has the queries. Search
by **prefix stem**. The tokenizer does no stemming, so every inflected form is
a different token — a search for the nominative singular of a Russian word
misses every other case it appears in.

An index that does not exist means the database predates it, or this SQLite
build has no FTS5. Any scan migrates the database; until then, fall back to a
substring match and say that you did.

## When a query fails

A query naming a table or column that does not exist comes back with the
actual schema attached. Rewrite from that listing rather than guessing a
second time.
