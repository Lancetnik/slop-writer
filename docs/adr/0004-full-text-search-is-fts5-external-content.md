# Full-text search is FTS5 external-content indexes, not vectors

The skill targets channels of any size; past a few thousand messages the
SQL-writing agent can no longer read the whole corpus, and `LIKE` returns
unranked hundreds. We add two FTS5 virtual tables — `posts_fts` over
`posts.text` and `gm_fts` over `group_messages.text` — in **external
content** mode (no text duplication), kept in sync by `AFTER
INSERT/UPDATE/DELETE` triggers in `SCHEMA`. `MATCH` is the canonical
text-search pattern in references/schema.md; ranking via `bm25()`,
excerpts via `snippet()`.

Vector search (sqlite-vec + embeddings) was rejected: it needs a loadable
C extension (breaking `tg_query.py`'s stdlib-only property) plus an
embedding pipeline with a model dependency and per-scrape cost — and the
DB's consumer is itself an LLM, which only needs candidate retrieval, not
semantic ranking.

## Considered Options

- **Tokenizer: `unicode61` + prefix queries — chosen.** No stemmer exists
  for most languages and channels are multilingual, so word forms are
  matched with `MATCH 'stem*'`, a pattern the SQL-writing agent follows
  from schema.md. `trigram` (substring search, no morphology concerns) was
  rejected: ~3× index size, SQLite ≥3.34, no useful bm25 ranking.
  `porter` mangles non-English tokens.
- **External content over regular/contentless — chosen.** Regular
  duplicates every message text; contentless breaks `snippet()` and needs
  `contentless_delete` (SQLite ≥3.43) for the thread-replace delete
  pattern. External content works on any FTS5-enabled SQLite. Triggers
  are safe here because no write path uses `INSERT OR REPLACE` (which
  skips delete triggers without `recursive_triggers`) — they are all
  `ON CONFLICT DO UPDATE` or explicit `DELETE` + `INSERT`.
- **Sync via triggers, not Python — chosen.** Triggers live next to the
  DDL and cover any future write path automatically; per-writer Python
  sync desynchronizes silently the first time someone forgets it.

## Consequences

- Existing DBs self-heal: `open_db` detects the FTS tables' absence
  before applying `SCHEMA` and runs a one-time
  `INSERT INTO x_fts(x_fts) VALUES('rebuild')` after creating them —
  same pattern as `_drop_legacy_tables`. Without this the index would be
  silently empty and `MATCH` would return zero rows without error.
- FTS DDL lives in a separate `FTS_SCHEMA` constant applied in
  try/except: on a rare SQLite build without the FTS5 module, scraping
  still works and only search is lost (a warning is logged). The agent's
  `MATCH` then fails with "no such table", which already triggers
  `tg_query.py`'s schema-listing retry hint — it falls back to `LIKE`.
- `tools/check_schema_doc.py` checks `SCHEMA` + `FTS_SCHEMA` against
  references/schema.md.
- FTS5 shadow tables (`*_fts_data`, `*_fts_idx`, `*_fts_docsize`,
  `*_fts_config`) are filtered out of `tg_query.py`'s error-time schema
  listing; the virtual tables themselves stay listed.
