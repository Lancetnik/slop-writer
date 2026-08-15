# Publishing — `tg_publish.py` (+ `scheduled`)

The only commands that write to Telegram. They need **post rights** on the
channel and the same session as the read commands. Nothing is persisted to the
DB: scheduled ids differ from the ids a post gets once published, and carry no
engagement yet — so the queue lives in Telegram alone, and `scheduled` is the only way to read it. `tg_query.py` never sees a future post.

These commands reach a live channel. Show the user the exact body and the exact `--at`, get their go-ahead, then run the command.

Read [markup.md](markup.md) before writing a post body — it lists the Markdown
that survives the trip to Telegram entities.

## See what is queued — `scheduled`

```
uv run <skill_dir>/scripts/tg_scrape.py scheduled --channel @name
```

Lists the channel's scheduled posts soonest-first: an `## Overview` (count +
UTC window) and a numbered `## Queue`, each entry headed by the scheduled time,
a relative delta (`in ~17h` / `overdue 10m`) and the `sched-msg #` id, then the
full body as a blockquote plus attachments. That `sched-msg` id is the `--id`
for `reschedule` and `edit`; it is stable across an edit. Console output only.

**Every time in that listing is UTC**, including the per-entry headings, while people keep their plans in local time. Convert to the user's timezone when you report the queue, and say which timezone you converted to.

## Queue a post — `schedule`

```
uv run <skill_dir>/scripts/tg_publish.py schedule --channel @name \
  --file post.md --at 2026-06-27T18:00:00+03:00
```

The body comes from `--file PATH` **or** stdin (`--file -`, or omit `--file`).
Drafts usually carry metainfo — a header, trailing notes — that must not be
published, so produce the clean body and pipe it through a quoted heredoc: no
temp file, and backticks, `$`, and quotes pass through verbatim.

```
uv run <skill_dir>/scripts/tg_publish.py schedule --channel @name \
  --at 2026-06-27T18:00:00+03:00 --file - <<'EOF'
**Body** with `code`, a price of 40$, and a ||spoiler||.
EOF
```

`--at` must be **ISO-8601 with a UTC offset** (`…+03:00`); a naive time is
rejected as ambiguous. The post must be at least **1 hour** ahead — an earlier
time exits 1, and there is no override flag.

Photos:

```
uv run <skill_dir>/scripts/tg_publish.py schedule --channel @name \
  --file post.md --photo cover.jpg --photo chart.png \
  --at 2026-06-27T18:00:00+03:00
```

`--photo` repeats up to 10 images, which publish as **one album**. The body
then becomes the **caption** and may be empty (omit `--file` for a caption-less
photo post). The caption renders below the photos as in the Telegram UI;
`--caption-above` flips it on top. Only real photo files are accepted
(`.jpg`/`.jpeg`/`.png`/`.webp`) — anything else would go out as a document, so
convert first.

Length caps are enforced by **Telegram, not the CLI**, and depend on the
account: 1024 characters for captions (2048 with Premium), 4096 for text posts.
If Telegram rejects the body, the command exits 1 with a readable error and
nothing is queued.

## Retime — `reschedule`

```
uv run <skill_dir>/scripts/tg_publish.py reschedule --channel @name \
  --id 182 --at 2026-06-28T19:00:00+03:00
```

Body unchanged, caption position unchanged; the 1-hour floor applies again.

## Rewrite — `edit`

```
uv run <skill_dir>/scripts/tg_publish.py edit --channel @name \
  --id 182 --file revised.md
```

Replaces the body (same `--file`/stdin rules as `schedule`), keeps the publish
time — no floor check. On a photo post it rewrites the caption but cannot add
or replace the photos themselves.

## After any write

Telethon returns `None` for scheduled edits, so the confirmation block
(publish time, `sched-msg` id, body preview) is built from the known inputs
rather than from Telegram's response. Confirm it landed by running `scheduled`.
