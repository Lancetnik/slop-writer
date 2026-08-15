"""Markdown renderers: plain summary dicts in, LLM-oriented Markdown out.

Pure presentation — no Telegram, no SQLite, no Telethon types. The dicts the
domain modules return (`ScrapeResult`, `GroupScanResult`, ...) are the
interface; anything here is exercisable by fabricating them.

Returning a string, rather than printing it, is what the second entrypoint
forced (Lancetnik/slop-writer#16): for a **stdio** MCP server stdout *is* the
JSON-RPC transport, so a `print` inside the server process corrupts the
protocol stream. The CLIs do `print(summarize_x(...))` and their terminal
output is unchanged; the server puts the same string in a text block. One
renderer serves both, which is the point — two would be two things to keep in
sync.

The summaries are **pre-computation**, not decoration: the aggregation, the
top-10 ranking, and above all the direction disambiguation in
`summarize_scrape` ("OTHER channels forwarded YOUR content" vs "YOUR reposts
of OTHER channels") are domain knowledge encoded in the output. Handed the raw
rows instead, a model re-derives that relation on every read and sometimes gets
it backwards.
"""

from collections import Counter
from datetime import UTC, datetime

#: Cap on the two `summarize_scrape` sections that grow with the scrape window.
#: Every other section is bounded by construction (an aggregate or a top-10);
#: these two are per-post, so a 500-post scrape would otherwise render 500
#: blocks. Clipping is always announced with the true total (#16).
MAX_RESHARE_POSTS = 25


def _text_snippet(text: str | None, length: int = 80) -> str:
    return " ".join((text or "").split())[:length]


def _md_cell(text: str | None) -> str:
    """Snippet safe for a Markdown table cell - escape pipes, drop newlines."""
    return _text_snippet(text).replace("|", "\\|") or "—"


def _as_number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _rel_when(iso: str | None, now: datetime) -> str:
    """Coarse, agent-friendly delta from `now`, e.g. 'in ~3h' / 'overdue 10m'."""
    if not iso:
        return "no date"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return "unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    secs = (dt - now).total_seconds()
    overdue = secs < 0
    secs = abs(secs)
    if secs < 3600:
        mag = f"{int(secs // 60)}m"
    elif secs < 86400:
        mag = f"{int(secs // 3600)}h"
    else:
        mag = f"{int(secs // 86400)}d"
    return f"overdue {mag}" if overdue else f"in ~{mag}"


def _query_cell(value, truncate: bool = True) -> str:
    if value is None:
        return ""
    text = str(value).replace("|", "\\|").replace("\n", " ")
    if truncate and len(text) > 200:
        text = text[:197] + "..."
    return text


def summarize_query(
    columns: list[str],
    rows: list[tuple],
    limit: int = 100,
    truncate: bool = True,
) -> str:
    """Render a query result as a Markdown table.

    `limit` caps the rendered rows (0 = all) but never the reported count: a
    caller reading "50 row(s), showing 50" must be able to tell a full answer
    from a clipped one. That separation is a server invariant, not a nicety —
    silently clipping a metrics table is a correctness bug.

    A pipe table, not JSON: the header prints once instead of repeating every
    key on every row, which is 2-3x fewer tokens for the same information."""
    if not columns:
        return "(query returned no columns)"

    truncated = limit and len(rows) > limit
    visible = rows[:limit] if limit else rows

    out = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in visible:
        out.append("| " + " | ".join(_query_cell(v, truncate) for v in row) + " |")

    out.append(f"\n_{len(rows)} row(s)" + (f", showing {limit}_" if truncated else "_"))
    return "\n".join(out)


def summarize_scrape(channel: str, posts: list[dict], channels: list[dict]) -> str:
    """Render an LLM-oriented summary of a scrape run."""
    out = [f"\n# Scrape summary: {channel}\n"]
    if not posts:
        out.append("No posts fetched.")
        return "\n".join(out)

    dates = sorted(p["date"] for p in posts if p.get("date"))
    n = len(posts)
    views = sum(p.get("views") or 0 for p in posts)
    forwards = sum(p.get("forwards") or 0 for p in posts)
    reactions = sum(p.get("reactions") or 0 for p in posts)
    comments = sum(p.get("comments_count") or 0 for p in posts)

    # Outward forwarders only (channels that re-shared our posts). Inward
    # sources we forwarded from are registered into the same channel_map for
    # `public_channels` persistence but carry no `shared_posts`, so filter.
    forwarders = [c for c in channels if c.get("shared_posts")]

    out.append("## Overview\n")
    span = f"  ({dates[0][:10]} → {dates[-1][:10]})" if dates else ""
    out.append(f"- Posts: {n}{span}")
    out.append(f"- Views: {views:,}  (avg {views // n:,}/post)")
    out.append(f"- Reactions: {reactions:,}   Comments: {comments:,}   "
               f"Forwards of your posts: {forwards:,}")
    out.append(f"- Your posts re-shared by: {len(forwarders)} other channels")

    # One combined ranking, sorted by views, with reactions alongside - half
    # the lines of two separate tables and both signals visible at once.
    top = sorted(posts, key=lambda p: p.get("views") or 0, reverse=True)[:10]
    out.append("\n## Top posts\n")
    out.append("| Views | Reactions | Post | Snippet |")
    out.append("|------:|----------:|------|---------|")
    for p in top:
        out.append(
            f"| {p.get('views') or 0:,} | {p.get('reactions') or 0:,} "
            f"| {p['link']} | {_md_cell(p.get('text'))} |"
        )

    if forwarders:
        # Group by post (not by channel): for each of our posts that got
        # re-shared, list the channels that shared it. Inverts the
        # forwarder->posts mapping we already have - no extra API calls.
        post_by_id = {p["id"]: p for p in posts}
        shares_by_post: dict[int, list[dict]] = {}
        for c in forwarders:
            for pid in c["shared_posts"]:
                shares_by_post.setdefault(pid, []).append(c)
        total_shares = sum(len(chs) for chs in shares_by_post.values())
        # Direction matters and gets confused easily: this section is OTHERS
        # re-sharing US. The opposite direction (our channel reposting others)
        # is the "YOUR reposts" section below. Spell it out in the headings.
        out.append(
            f"\n## Who re-shared YOUR posts "
            f"({len(shares_by_post)} of your posts re-shared, "
            f"{total_shares} shares by {len(forwarders)} other channels)\n"
        )
        out.append(
            "Direction: OTHER channels forwarded YOUR content (your reach). "
            "Each of your posts below was re-shared by the listed channels. "
            "`subs` is each channel's size (the audience that share reached).\n"
        )
        # Bounded like every other section: the summary must not grow with the
        # window, or a wide scrape buries its own overview (#16).
        shown = sorted(shares_by_post, reverse=True)
        clipped = shown[MAX_RESHARE_POSTS:]
        for pid in shown[:MAX_RESHARE_POSTS]:
            chans = sorted(
                shares_by_post[pid],
                key=lambda c: c.get("subscribers") or 0,
                reverse=True,
            )
            p = post_by_id.get(pid)
            if p is not None:
                views = p.get("views")
                views_str = f"{views:,} views" if views is not None else "views n/a"
                out.append(f"### #{pid} ({views_str}) — {p['link']}")
                snippet = _text_snippet(p.get("text"))
                if snippet:
                    out.append(f'"{snippet}"')
            else:
                # Re-shared post is outside this scrape's window; id only.
                out.append(f"### #{pid}")
            for c in chans:
                subs = c.get("subscribers")
                subs_str = f"{subs:,} subs" if subs is not None else "subs n/a"
                name = c.get("name") or c["link"]
                out.append(f"- {name} ({subs_str}) — {c['link']}")
            out.append("")
        if clipped:
            out.append(
                f"_{len(shown)} re-shared posts, showing {MAX_RESHARE_POSTS}. "
                "Query `public_shares` for the rest._\n"
            )

    # Our posts that forward/cite another channel — one row per post, newest
    # first. Source channel name/subs joined from `channels`.
    cited_posts = [p for p in posts if p.get("forwarder_from_channel")]
    if cited_posts:
        by_link = {c["link"]: c for c in channels}
        out.append("\n## YOUR reposts of OTHER channels (not your original content)\n")
        out.append(
            "Direction: YOUR channel forwarded SOMEONE ELSE's content — the "
            "opposite of the re-shares section above.\n"
        )
        out.append("| Post | Snippet | Reposted from |")
        out.append("|------|---------|---------------|")
        ranked = sorted(cited_posts, key=lambda p: p["id"], reverse=True)
        for p in ranked[:MAX_RESHARE_POSTS]:
            link = p["forwarder_from_channel"]
            info = by_link.get(link, {})
            name = info.get("name") or link
            subs = info.get("subscribers")
            subs_str = f"{subs:,} subs" if subs else "subs n/a"
            out.append(
                f"| {p['link']} | {_md_cell(p.get('text'))} "
                f"| {name} ({subs_str}) {link} |"
            )
        if len(ranked) > MAX_RESHARE_POSTS:
            out.append(
                f"\n_{len(ranked)} reposts, showing {MAX_RESHARE_POSTS}. "
                "Query `posts WHERE forwarder_from_channel IS NOT NULL` "
                "for the rest._"
            )
    return "\n".join(out)



def summarize_subscribers(channel: str, rows: dict[str, dict]) -> str:
    """Render an LLM-oriented summary of subscriber dynamics."""
    out = [f"\n# Subscriber summary: {channel}\n"]
    dates = sorted(rows)
    if not dates:
        out.append("No subscriber data.")
        return "\n".join(out)

    joins = sum(_as_number(rows[d].get("joins")) for d in dates)
    leaves = sum(_as_number(rows[d].get("leaves")) for d in dates)
    first_total = _as_number(rows[dates[0]].get("total"))
    last_total = _as_number(rows[dates[-1]].get("total"))
    days = len(dates)

    out.append(f"- Date range: {dates[0]} -> {dates[-1]} ({days} days)")
    out.append(f"- Current total subscribers: {int(last_total):,}")
    out.append(
        f"- Net change over period: {int(last_total - first_total):+,} "
        f"(from {int(first_total):,})"
    )
    out.append(
        f"- Total joins: {int(joins):,} | total leaves: {int(leaves):,} "
        f"| net: {int(joins - leaves):+,}"
    )
    out.append(f"- Avg per day: {joins / days:.1f} joins, {leaves / days:.1f} leaves")

    best = max(dates, key=lambda d: _as_number(rows[d].get("joins")))
    worst = max(dates, key=lambda d: _as_number(rows[d].get("leaves")))
    out.append(f"- Best day: {best} (+{int(_as_number(rows[best].get('joins')))} joins)")
    out.append(
        f"- Worst day: {worst} "
        f"(-{int(_as_number(rows[worst].get('leaves')))} leaves)"
    )

    source_totals: Counter = Counter()
    for d in dates:
        for source, count in rows[d].get("sources", {}).items():
            source_totals[source] += _as_number(count)
    if source_totals:
        out.append("\n## New subscribers by source (period total)\n")
        grand = sum(source_totals.values()) or 1
        for source, value in source_totals.most_common():
            out.append(f"- {source}: {int(value):,} ({value / grand * 100:.1f}%)")
    return "\n".join(out)


def summarize_scheduled(channel: str, items: list[dict]) -> str:
    """Render the scheduled-post queue, one block per post."""
    out = [f"\n# Scheduled posts: {channel}\n"]
    if not items:
        out.append("No scheduled posts in the queue.")
        return "\n".join(out)

    now = datetime.now(UTC)
    dates = [i["date"] for i in items if i.get("date")]
    out.append("## Overview\n")
    out.append(f"- Queued posts: {len(items)}")
    if dates:
        lo = dates[0][:16].replace("T", " ")
        hi = dates[-1][:16].replace("T", " ")
        out.append(f"- Window: {lo} → {hi} UTC")
    out.append("- Times are UTC. Scheduled posts have no engagement metrics yet.")
    out.append(
        "- `sched-msg #` is the scheduled-message id, distinct from the id the "
        "post gets once published.\n"
    )

    out.append("## Queue\n")
    for n, i in enumerate(items, 1):
        when = (i.get("date") or "")[:16].replace("T", " ") or "no date"
        rel = _rel_when(i.get("date"), now)
        # The id heads its own block rather than sitting in a table cell: it is
        # the *input* to reschedule/edit, so it has to be lexically unmissable.
        out.append(f"### {n}. {when} UTC ({rel}) — sched-msg #{i['id']}\n")
        body = (i.get("text") or "").strip()
        if body:
            out.append("Text:")
            for line in body.splitlines():
                out.append(f"> {line}")
        else:
            out.append("Text: (none)")
        attachments = i.get("attachments") or []
        if attachments:
            out.append("\nAttachments:")
            for a in attachments:
                out.append(f"- {a}")
        else:
            out.append("\nAttachments: (none)")
        out.append("")
    return "\n".join(out)


def summarize_schedule(channel: str, item: dict, action: str = "Scheduled") -> str:
    """Confirm one queued/changed post.

    `action` heads the block — "Scheduled" (new), "Rescheduled" (time changed),
    or "Edited" (body changed). Nothing is persisted: a scheduled-message id
    differs from the id the post gets once published and carries no engagement
    (same rationale as the read-only `scheduled` command)."""
    now = datetime.now(UTC)
    when = (item.get("date") or "")[:16].replace("T", " ") or "no date"
    rel = _rel_when(item.get("date"), now)
    out = [f"\n# {action} post: {channel}\n"]
    out.append(f"- Publishes: {when} UTC ({rel})")
    if item.get("requested"):
        out.append(f"- Requested: {item['requested']}")
    out.append(
        f"- sched-msg #{item['id']} — distinct from the id the post gets once "
        "published; not stored in the DB."
    )
    if item.get("entities") is not None:
        out.append(f"- Formatting entities: {item['entities']}")
    if item.get("photos"):
        n = item["photos"]
        kind = "album of " if n > 1 else ""
        pos = "above" if item.get("caption_above") else "below"
        out.append(
            f"- Photos: {kind}{n} — the body below is the caption, "
            f"shown {pos} the photos"
        )
    out.append("\n## Body preview\n")
    body = (item.get("text") or "").strip()
    if body:
        for line in body.splitlines():
            out.append(f"> {line}")
    else:
        out.append("> (empty)")
    out.append("")
    return "\n".join(out)


def summarize_views(
    channel: str, hours: list, views: list, period_start: str, period_end: str
) -> str:
    """Render an LLM-oriented summary of views-per-hour."""
    out = [f"\n# Views by hour of day: {channel}\n"]
    pairs = [(int(h), _as_number(v)) for h, v in zip(hours, views)]
    if not pairs:
        out.append("No views-by-hour data.")
        return "\n".join(out)

    total = sum(v for _, v in pairs) or 1
    ranked = sorted(pairs, key=lambda hv: hv[1], reverse=True)

    out.append(f"- Analyzed period: {period_start} -> {period_end}")
    out.append(f"- Total views in sample: {int(total):,}")
    out.append(
        "- Hour is hour-of-day, 0-23, in the Telegram account's local "
        "timezone (NOT UTC)."
    )

    out.append("\n## Peak hours\n")
    for hour, value in ranked[:3]:
        out.append(f"- {hour:02d}:00 | {int(value):,} views ({value / total * 100:.1f}%)")

    out.append("\n## Quietest hours\n")
    for hour, value in sorted(ranked[-3:]):
        out.append(f"- {hour:02d}:00 | {int(value):,} views ({value / total * 100:.1f}%)")

    out.append("\n## All hours\n")
    for hour, value in sorted(pairs):
        out.append(f"- {hour:02d}:00 | {int(value):,} views ({value / total * 100:.1f}%)")
    return "\n".join(out)


def _via_breakdown(events: list[dict], kind: str) -> str:
    counts = Counter(e.get("via") or "?" for e in events if e["kind"] == kind)
    total = sum(counts.values())
    if not total:
        return f"{total}"
    detail = ", ".join(f"{via} {n}" for via, n in counts.most_common())
    return f"{total} ({detail})"


def summarize_group(
    label: str,
    overview: dict,
    messages: list[dict],
    events: list[dict],
    threads: list[dict],
) -> str:
    """Render an LLM-oriented summary of a discussion-group scan.

    `messages` includes thread roots (is_thread_root=1); every engagement
    figure below excludes them — roots carry the channel post's reactions.
    """
    out = [f"\n# Group summary: {label}\n"]
    if not messages and not events:
        out.append("No group messages or events in the scanned window.")
        return "\n".join(out)

    own = [m for m in messages if not m.get("is_thread_root")]
    in_threads = [m for m in own if m.get("thread_post_id") is not None]
    chatter = [m for m in own if m.get("thread_post_id") is None]
    joins = [e for e in events if e["kind"] == "join"]
    leaves = [e for e in events if e["kind"] == "leave"]

    out.append("## Overview\n")
    members = overview.get("members")
    members_str = f" — {members:,} members" if members is not None else ""
    out.append(
        f"- Group: {overview.get('title') or label} ({overview.get('link')}){members_str}"
    )
    dates = sorted(d for m in own for d in [m.get("date")] if d)
    if dates:
        out.append(f"- Window: {dates[0][:10]} → {dates[-1][:10]}"
                   f"  (group-msg ids {overview.get('id_range')})")
    out.append(f"- Messages: {len(own)} ({len(in_threads)} in threads, "
               f"{len(chatter)} top-level chatter)")
    out.append(f"- Joins: {_via_breakdown(events, 'join')}  |  "
               f"Leaves: {_via_breakdown(events, 'leave')}  |  "
               f"net {len(joins) - len(leaves):+d}")
    if overview.get("standalone"):
        out.append("- Standalone mode: thread linkage skipped.")

    by_day: dict[str, Counter] = {}
    for e in events:
        day = (e.get("date") or "")[:10]
        if day:
            by_day.setdefault(day, Counter())[e["kind"]] += 1
    if by_day:
        out.append("\n## Joins & leaves by day\n")
        out.append("| Day | Joins | Leaves |")
        out.append("|-----|------:|-------:|")
        for day in sorted(by_day):
            c = by_day[day]
            out.append(f"| {day} | {c['join']} | {c['leave']} |")

    if threads and not overview.get("standalone"):
        out.append(f"\n## Threads in window ({len(threads)})\n")
        out.append("| Post | Replies | Commenters | First reply | Snippet |")
        out.append("|------|--------:|-----------:|-------------|---------|")
        unscraped = False
        for t in sorted(threads, key=lambda t: t["replies"], reverse=True):
            first = t.get("first_reply_minutes")
            first_str = f"{first:.0f}m" if first is not None else "—"
            # `post_link` is always set; `scraped` says whether a `posts` row
            # backs it. Missing rows are the norm for posts newer than the
            # last `scrape`, so the marker points at the fix instead of
            # guessing the post is gone.
            post = t["post_link"]
            if not t.get("scraped"):
                post = f"{post} ⚠"
                unscraped = True
            out.append(f"| {post} | {t['replies']} "
                       f"| {t['commenters']} | {first_str} | {_md_cell(t.get('snippet'))} |")
        if unscraped:
            out.append("\n⚠ — no `posts` row yet, so date and snippet are blank. "
                       "Run `fetch` on those ids to fill them in.")

    if own:
        out.append("\n## Engagement\n")
        per_author: Counter = Counter(m.get("author") for m in own)
        reacts: Counter = Counter()
        for m in own:
            reacts[m.get("author")] += m.get("reactions") or 0
        out.append("| Author | Messages | Reactions received |")
        out.append("|--------|---------:|-------------------:|")
        for author, n in per_author.most_common(10):
            # `author` is None for anonymous senders — don't print "None".
            out.append(f"| {author or 'anonymous'} | {n} | {reacts[author]} |")
        days = len({(m.get("date") or "")[:10] for m in own if m.get("date")}) or 1
        out.append(f"\n- Avg messages/day: {len(own) / days:.1f}")
        top = max(own, key=lambda m: m.get("reactions") or 0)
        if top.get("reactions"):
            out.append(f"- Most-reacted message: {top['reactions']} reactions — "
                       f"{top.get('author') or 'anonymous'}: \"{_md_cell(top.get('text'))}\"")
    return "\n".join(out)


def summarize_install(result) -> str:
    """What `install` did, for a human at a terminal.

    Lives here for the same reason the others do — nothing in `slop_writer`
    prints — even though this one output never reaches a model. `install` is a
    CLI-only command, so this renders plain lines, not the LLM-oriented
    Markdown above."""
    from .install import SERVER_NAME, other_client_entry

    root = result.project_root
    out = [f"Wired {SERVER_NAME} {result.version} into {root}", ""]
    out.append(f"  {result.mcp_config.relative_to(root)} — server entry "
               f"(overwritten every install; that is the upgrade path)")
    skill_rel = result.skill_target.relative_to(root)
    out.append(f"  {skill_rel} — skill, "
               f"{'replaced' if result.skill_existed else 'installed'}")
    if result.legacy_skill_removed is not None:
        # Deleting something the user did not ask us to delete is exactly the
        # kind of thing that must not be silent (#34).
        out.append(f"  {result.legacy_skill_removed.relative_to(root)} — "
                   f"removed: this skill's pre-0.4 directory, which would "
                   f"otherwise load alongside the current one")
    if result.permissions_seeded:
        out.append(f"  {result.settings.relative_to(root)} — permissions: "
                   f"reads allowed, publishing behind a prompt")
        out.append(f"  {result.memory_file.relative_to(root)} — address block "
                   f"so subagents find the skill")
    else:
        # Seeding on every run would silently restore an `ask` rule the human
        # deliberately removed — headless autoposting is a supported choice.
        out.append("  (permissions and CLAUDE.md left alone — seeded on first "
                   "install only, so your edits survive an upgrade)")
        out.append("  publishing tools this version expects under `ask`: "
                   + ", ".join(result.ask_tools))

    if result.skills_lock_names:
        from .install import LEGACY_SKILL_DIR_NAME

        out.append("")
        out.append("! skills-lock.json tracks "
                   + ", ".join(result.skills_lock_names)
                   + ". Both channels serve the same directory, so the lock's "
                     "hash will no longer match — expected, not a failure.")
        if LEGACY_SKILL_DIR_NAME in result.skills_lock_names:
            # We do not write another tool's state file, so this one is the
            # user's to clear — but it has to be said, or the entry silently
            # outlives the directory this run just deleted.
            out.append(f"  Its `{LEGACY_SKILL_DIR_NAME}` entry is the pre-0.4 "
                       f"name and now points at nothing. Remove it yourself — "
                       f"the lock belongs to npx, not to this command.")

    if not result.on_path:
        out.append("")
        out.append("! `slop-writer` is not on PATH, so the client will fail to "
                   "start the server. Add your tool bin directory to PATH "
                   "(`uv tool update-shell`), then check with "
                   "`which slop-writer`.")

    out.append("")
    out.append("Verified on Claude Code only. For Cursor, Codex or the Copilot "
               "coding agent, paste this into their MCP config yourself:")
    out.append("")
    out.append(other_client_entry())
    out.append("")
    out.append("Next: restart your MCP client (.mcp.json is read at session "
               "start only), and run `slop-writer init` to log in to Telegram.")
    return "\n".join(out)


def summarize_uninstall(result) -> str:
    """What `uninstall` removed, and — more usefully — what it did not."""
    out = [f"Removed slop-writer's wiring from {result.project_root}", ""]
    if result.removed:
        out.extend(f"  {item}" for item in result.removed)
    else:
        out.append("  (nothing to remove — was it installed in this project?)")
    if result.kept:
        out.append("")
        out.append("Kept:")
        for item in result.kept:
            out.append(f"  {item}")
    out.append("")
    out.append("Restart your MCP client to drop the server from the session.")
    return "\n".join(out)
