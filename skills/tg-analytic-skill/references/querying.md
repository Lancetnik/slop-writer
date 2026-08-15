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

## The two invariants that break queries silently

- `post_metrics` is **append-only** — one row per post per scrape run. "Latest
  snapshot" is `MAX(id)`, never `MAX(scrape_date)` (runs within the same second
  tie). schema.md carries the canonical CTE.
- `group_messages` holds comments **and** thread roots. Engagement queries must
  filter `is_thread_root = 0`, or every thread's root message inflates the
  comment counts.

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
