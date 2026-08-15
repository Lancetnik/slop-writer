"""The discussion group: service-message classification, thread linkage, and
the scan that turns a group into `group_messages` / `group_events` rows.

The classification half is duck-typed over Telethon objects — dispatch is on
`type(action).__name__`, attribute access via getattr — a habit worth keeping
because it is what makes those rules readable next to the wire format. The
module as a whole no longer avoids importing Telethon: since the package
resolved to one flat dependency set (#13) the property bought nothing, and the
scan below is the group's own domain logic, not the caller's. `db` remains the
stdlib-only module, which is the one that matters — a query path must never
drag a Telegram client in.

`group_messages` is also where post comments land (docs/adr/0002), so the post
pipeline imports the row writers here. The dependency runs one way only:
nothing in this module knows about `scrape`.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Connection

from telethon import TelegramClient
from telethon.tl.functions.channels import GetAdminLogRequest, GetFullChannelRequest
from telethon.tl.types import (
    ChannelAdminLogEventsFilter,
    Message,
    MessageService,
    PeerChannel,
)

from .db import db_path_for, open_db
from .errors import SlopWriterError
from .messages import count_reactions, media_type, sender_fields, tme_link
from .tg import channel_session, resolve_peer

log = logging.getLogger(__name__)


@dataclass
class GroupEvent:
    """One membership change. kind: 'join'|'leave'; via: join → 'link'|
    'request'|'added', leave → 'self'|'removed'|'unknown'."""
    id: int
    date: str | None
    kind: str
    via: str
    user_id: int | None


def _sender_user_id(msg) -> int | None:
    return getattr(getattr(msg, "from_id", None), "user_id", None)


def _iso(msg) -> str | None:
    date = getattr(msg, "date", None)
    return date.isoformat() if date else None


def classify_service_message(msg) -> list[GroupEvent]:
    """Map one service message to its membership events.

    One MessageActionChatAddUser can add several users → several events
    (why group_events' PK is (id, user_id)). 'added' covers both "added by
    a member" and the Join button — Telegram encodes a self-join as the
    user adding themselves. Non-membership service messages (pins, title
    changes, ...) yield []."""
    action = getattr(msg, "action", None)
    if action is None:
        return []
    name = type(action).__name__
    date = _iso(msg)
    sender = _sender_user_id(msg)
    if name == "MessageActionChatJoinedByLink":
        return [GroupEvent(msg.id, date, "join", "link", sender)]
    if name == "MessageActionChatJoinedByRequest":
        return [GroupEvent(msg.id, date, "join", "request", sender)]
    if name == "MessageActionChatAddUser":
        users = getattr(action, "users", None) or []
        return [GroupEvent(msg.id, date, "join", "added", uid) for uid in users]
    if name == "MessageActionChatDeleteUser":
        uid = getattr(action, "user_id", None)
        # Live data shows three actor states, not two: confirmed self-leave
        # (sender == user), confirmed kick (sender is someone else), and NO
        # actor at all (sender None — e.g. deleted accounts auto-removed by
        # Telegram). Calling the last one either 'self' or 'removed' would
        # silently skew churn numbers, so it gets its own value.
        if sender is None:
            via = "unknown"
        elif uid is not None and uid == sender:
            via = "self"
        else:
            via = "removed"
        return [GroupEvent(msg.id, date, "leave", via, uid)]
    return []


def classify_admin_log_event(ev) -> list[GroupEvent]:
    """Map one admin-log event to membership events.

    The admin log records joins/leaves that Telegram never wrote (or later
    deleted) as service messages — CTA join bursts get suppressed wholesale,
    so for an admin account this is the authoritative source. Same `via`
    vocabulary as classify_service_message. Admin-log event ids live in a
    separate, much larger id space than message ids, so the (id, user_id)
    PK can't collide with service-message events."""
    action = getattr(ev, "action", None)
    if action is None:
        return []
    name = type(action).__name__
    date = _iso(ev)
    actor = getattr(ev, "user_id", None)
    if name == "ChannelAdminLogEventActionParticipantJoin":
        # Self-join via the public Join button — same bucket the service
        # path uses for AddUser(self).
        return [GroupEvent(ev.id, date, "join", "added", actor)]
    if name == "ChannelAdminLogEventActionParticipantJoinByInvite":
        return [GroupEvent(ev.id, date, "join", "link", actor)]
    if name == "ChannelAdminLogEventActionParticipantJoinByRequest":
        return [GroupEvent(ev.id, date, "join", "request", actor)]
    if name == "ChannelAdminLogEventActionParticipantInvite":
        # The subject is the invited participant, not the acting member.
        uid = getattr(getattr(action, "participant", None), "user_id", None)
        return [GroupEvent(ev.id, date, "join", "added", uid)] if uid else []
    if name == "ChannelAdminLogEventActionParticipantLeave":
        return [GroupEvent(ev.id, date, "leave", "self", actor)]
    if name == "ChannelAdminLogEventActionParticipantToggleBan":
        # Only a ban that ejects the member counts as a leave; rights-only
        # restrictions keep them in the group.
        new = getattr(action, "new_participant", None)
        if type(new).__name__ == "ChannelParticipantBanned" and getattr(
            new, "left", False
        ):
            uid = getattr(getattr(new, "peer", None), "user_id", None)
            if uid is not None:
                return [GroupEvent(ev.id, date, "leave", "removed", uid)]
        return []
    return []


def auto_forward_post_id(msg, channel_id: int | None) -> int | None:
    """Channel post id if `msg` is the auto-forward of a channel post (a
    thread root); None otherwise.

    `channel_id` is the linked channel's id — it guards against manual
    forwards of unrelated channels' posts being mistaken for roots. None
    (standalone group) means no message is ever a root."""
    if channel_id is None:
        return None
    fwd = getattr(msg, "fwd_from", None)
    if fwd is None:
        return None
    post_id = getattr(fwd, "channel_post", None)
    if post_id is None:
        return None
    if getattr(getattr(fwd, "from_id", None), "channel_id", None) != channel_id:
        return None
    return post_id


def _thread_head(msg) -> int | None:
    """Group-side id of the thread head this message replies under.

    Nested replies carry the head in reply_to_top_id; direct comments on
    the root carry it in reply_to_msg_id (top_id absent)."""
    reply = getattr(msg, "reply_to", None)
    if reply is None:
        return None
    top = getattr(reply, "reply_to_top_id", None)
    return top if top is not None else getattr(reply, "reply_to_msg_id", None)


def thread_post_id_for(msg, root_map: dict[int, int]) -> int | None:
    """Which channel post's thread `msg` belongs to (None = top-level
    chatter). `root_map` maps group-side root id → channel post id."""
    head = _thread_head(msg)
    return root_map.get(head) if head is not None else None


def unresolved_root_refs(msgs, root_map: dict[int, int]) -> set[int]:
    """Thread heads referenced by `msgs` but absent from `root_map` —
    roots that fell outside the scanned window and need a targeted fetch.
    May include heads that turn out to be plain chatter (replies to a
    top-level message); the caller fetches and re-checks."""
    return {
        head
        for m in msgs
        if (head := _thread_head(m)) is not None and head not in root_map
    }


# ---------------------------------------------------------------------------
# DB writes — group_messages is also the comment store (docs/adr/0002), so
# `scrape` writes a post's thread through message_row/upsert_group_messages.
# ---------------------------------------------------------------------------


def message_row(msg: Message, thread_post: int | None, is_root: bool) -> tuple:
    """group_messages row tuple — shared by the group scan and the
    scrape-side comment path (which knows its thread_post directly)."""
    uid, name, username = sender_fields(msg)
    reactions, stars = count_reactions(msg)
    return (
        msg.id,
        msg.date.isoformat() if msg.date else None,
        msg.text or "",
        uid,
        name,
        username,
        msg.reply_to_msg_id,
        thread_post,
        1 if is_root else 0,
        reactions + stars,
        media_type(msg),
    )


def upsert_group_messages(conn: Connection, rows: list[tuple]) -> None:
    conn.executemany(
        """
        INSERT INTO group_messages (
            id, date, text, user_id, user_name, user_username,
            reply_to_msg_id, thread_post_id, is_thread_root,
            reactions, media_type
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            date            = excluded.date,
            text            = excluded.text,
            user_id         = excluded.user_id,
            user_name       = excluded.user_name,
            user_username   = excluded.user_username,
            reply_to_msg_id = excluded.reply_to_msg_id,
            thread_post_id  = excluded.thread_post_id,
            is_thread_root  = excluded.is_thread_root,
            reactions       = excluded.reactions,
            media_type      = excluded.media_type
        """,
        rows,
    )


def upsert_group_events(
    conn: Connection,
    events: list[GroupEvent],
    users: dict[int, tuple[str | None, str | None]],
) -> None:
    # SQLite treats NULLs in a composite PK as distinct, so ON CONFLICT
    # can't dedupe the rare events whose user_id is unknown — pre-delete
    # them for the incoming ids to keep re-scans idempotent.
    conn.executemany(
        "DELETE FROM group_events WHERE id = ? AND user_id IS NULL",
        [(e.id,) for e in events if e.user_id is None],
    )
    conn.executemany(
        """
        INSERT INTO group_events (
            id, date, kind, via, user_id, user_name, user_username
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id, user_id) DO UPDATE SET
            date          = excluded.date,
            kind          = excluded.kind,
            via           = excluded.via,
            user_name     = excluded.user_name,
            user_username = excluded.user_username
        """,
        [
            (e.id, e.date, e.kind, e.via, e.user_id,
             *(users.get(e.user_id) or (None, None)))
            for e in events
        ],
    )


def insert_group_metrics(
    conn: Connection, scrape_date: str, target: "GroupTarget"
) -> None:
    conn.execute(
        """
        INSERT INTO group_metrics (scrape_date, group_link, group_title, members)
        VALUES (?, ?, ?, ?)
        """,
        (scrape_date, target.link, target.title, target.members),
    )


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


@dataclass
class GroupTarget:
    entity: object          # the group, resolved
    title: str | None
    link: str
    members: int | None
    channel_id: int | None  # linked channel's id; None = standalone


@dataclass
class GroupScanResult:
    """What one scan produced, in the shapes `render.summarize_group` consumes.

    The scan returns this instead of printing it: the CLI renders it to stdout,
    and a second caller can do something else with the same run."""
    label: str
    overview: dict
    messages: list[dict]
    events: list[dict]
    threads: list[dict]


async def resolve_group_target(
    client: TelegramClient, channel: str | None, group: str | None
) -> GroupTarget:
    """Resolve the group to scan from exactly one of channel/group.

    channel: the channel's linked discussion group (error if none).
    group: the group itself, treated as standalone — if it turns out to
    be linked to a channel, log a notice suggesting the channel path and
    proceed (explicit beats clever: never silently redirect to another DB)."""
    if channel:
        ch_entity = await resolve_peer(client, channel)
        full = await client(GetFullChannelRequest(ch_entity))
        linked = full.full_chat.linked_chat_id
        if not linked:
            raise SlopWriterError(
                f"{channel} has no linked discussion group - nothing to scan",
                hint="Attach a discussion group to the channel, or scan a "
                "standalone group instead.",
            )
        entity = await client.get_entity(PeerChannel(linked))
        channel_id = ch_entity.id
    else:
        entity = await resolve_peer(client, group)
        channel_id = None

    g_full = await client(GetFullChannelRequest(entity))
    if group and g_full.full_chat.linked_chat_id:
        log.warning(
            "%s is the discussion group of a channel (id %d) - to get "
            "thread analytics, re-run against that channel",
            group, g_full.full_chat.linked_chat_id,
        )
    username = getattr(entity, "username", None)
    link = f"https://t.me/{username}" if username else f"https://t.me/c/{entity.id}"
    return GroupTarget(
        entity=entity,
        title=getattr(entity, "title", None),
        link=link,
        members=g_full.full_chat.participants_count,
        channel_id=channel_id,
    )


async def _fetch_admin_log_events(
    client: TelegramClient, entity
) -> tuple[list[GroupEvent], dict[int, tuple[str | None, str | None]]]:
    """Joins/leaves from the group's admin log (~48h retention), plus the
    subjects' (name, username) from the log's own user objects.

    The log records membership changes even when Telegram suppresses or
    deletes the corresponding service messages — which is exactly what
    happens to CTA join bursts — so for an admin account it is the
    authoritative join source. Requires admin; degrades to empty with a
    notice otherwise."""
    events_filter = ChannelAdminLogEventsFilter(
        join=True, leave=True, invite=True, ban=True, unban=False,
        kick=True, unkick=False, promote=False, demote=False, info=False,
        settings=False, pinned=False, edit=False, delete=False,
        group_call=False, invites=False, send=False, forums=False,
    )
    events: list[GroupEvent] = []
    users: dict[int, tuple[str | None, str | None]] = {}
    max_id = 0
    try:
        while True:
            res = await client(
                GetAdminLogRequest(
                    channel=entity, q="", max_id=max_id, min_id=0,
                    limit=100, events_filter=events_filter,
                )
            )
            if not res.events:
                break
            for ev in res.events:
                events.extend(classify_admin_log_event(ev))
            for u in res.users:
                first = getattr(u, "first_name", "") or ""
                last = getattr(u, "last_name", "") or ""
                users[u.id] = (
                    (first + " " + last).strip() or None,
                    getattr(u, "username", None),
                )
            max_id = res.events[-1].id
    except Exception as e:
        log.info(
            "admin log unavailable (admin rights needed) - joins/leaves "
            "rely on service messages only (%s)", e,
        )
        return [], {}
    if events:
        log.info(
            "admin log: %d membership event(s) (~48h retention - run "
            "`group` at least every 2 days to keep the series complete)",
            len(events),
        )
    return events, users


def _dedupe_admin_events(
    conn: Connection,
    admin_events: list[GroupEvent],
    service_events: list[GroupEvent],
) -> list[GroupEvent]:
    """Drop admin-log events already captured as service messages.

    The same join can appear in both sources under unrelated ids, so the
    (id, user_id) PK can't dedupe across them — match on (user_id, kind)
    within a 10-minute window instead, against both this run's service
    events and rows from earlier runs."""

    def near(a: str | None, b: str | None) -> bool:
        if not a or not b:
            return False
        try:
            da, db = datetime.fromisoformat(a), datetime.fromisoformat(b)
        except ValueError:
            return False
        return abs((da - db).total_seconds()) <= 600

    kept = []
    for e in admin_events:
        if any(
            s.user_id == e.user_id and s.kind == e.kind and near(s.date, e.date)
            for s in service_events
        ):
            continue
        row = conn.execute(
            """
            SELECT 1 FROM group_events
            WHERE user_id = ? AND kind = ? AND id != ?
              AND abs(strftime('%s', datetime(date))
                      - strftime('%s', datetime(?))) <= 600
            LIMIT 1
            """,
            (e.user_id, e.kind, e.id, e.date),
        ).fetchone()
        if row is None:
            kept.append(e)
    dropped = len(admin_events) - len(kept)
    if dropped:
        log.debug("admin log: %d duplicate event(s) skipped", dropped)
    return kept


async def _resolve_event_users(
    client: TelegramClient, events: list[GroupEvent]
) -> dict[int, tuple[str | None, str | None]]:
    """user_id -> (name, username) for event subjects. Added-user events
    reference users who never sent a message, so iter_messages' entity
    cache may miss them; resolve individually, tolerating dead accounts."""
    users: dict[int, tuple[str | None, str | None]] = {}
    for uid in {e.user_id for e in events if e.user_id is not None}:
        try:
            entity = await client.get_entity(uid)
            first = getattr(entity, "first_name", "") or ""
            last = getattr(entity, "last_name", "") or ""
            users[uid] = (
                (first + " " + last).strip() or None,
                getattr(entity, "username", None),
            )
        except Exception as e:
            log.debug("event user %d unresolvable (%s)", uid, e)
            users[uid] = (None, None)
    return users


def _group_message_row(msg: Message, root_map: dict[int, int]) -> tuple:
    is_root = msg.id in root_map
    thread_post = (
        root_map[msg.id] if is_root else thread_post_id_for(msg, root_map)
    )
    return message_row(msg, thread_post, is_root)


def _load_thread_stats(
    conn: Connection, lo: int, hi: int, channel: str
) -> list[dict]:
    """Per-thread stats for threads touched in the scanned id window,
    joined to posts for snippet/link and time-to-first-reply.

    The join is a LEFT JOIN: the scan reaches threads on posts that the post
    pipeline has not stored yet, so `p.link` is NULL for them. The link is
    built from the channel handle instead — reachable whether or not the post
    is in the DB — and `scraped` carries the distinction."""
    rows = conn.execute(
        """
        SELECT gm.thread_post_id, p.link, substr(COALESCE(p.text, ''), 1, 80),
               p.date, COUNT(*), COUNT(DISTINCT gm.author), MIN(gm.date)
        FROM group_messages gm
        LEFT JOIN posts p ON p.id = gm.thread_post_id
        WHERE gm.is_thread_root = 0
          AND gm.thread_post_id IS NOT NULL
          AND gm.id BETWEEN ? AND ?
        GROUP BY gm.thread_post_id
        """,
        (lo, hi),
    ).fetchall()
    threads = []
    for post_id, link, snippet, post_date, replies, commenters, first in rows:
        minutes = None
        if post_date and first:
            try:
                delta = (
                    datetime.fromisoformat(first)
                    - datetime.fromisoformat(post_date)
                ).total_seconds()
                minutes = max(delta, 0) / 60
            except ValueError:
                pass
        threads.append(
            {
                "post_id": post_id,
                "post_link": link or tme_link(channel, post_id),
                "scraped": link is not None,
                "snippet": snippet,
                "replies": replies,
                "commenters": commenters,
                "first_reply_minutes": minutes,
            }
        )
    return threads


async def scan_group(
    channel: str | None,
    group: str | None,
    output_dir: Path,
    session_file: str,
    limit: int | None = None,
    offset_id: int = 0,
    offset_date: datetime | None = None,
    latest: int | None = None,
) -> GroupScanResult:
    """Scan the discussion group: messages + membership events + a member
    snapshot. Selection semantics mirror the post scrape (same window flags,
    same `latest` newest-first flip, same inclusive offset_id).

    Exactly one of `channel` / `group` is expected; the caller decides how to
    report a request that names both or neither."""
    scrape_date = datetime.now(UTC).isoformat()
    handle = channel or group

    if latest is not None:
        reverse, iter_limit = False, latest
        iter_offset_id, iter_offset_date = 0, None
    else:
        reverse, iter_limit = True, limit
        iter_offset_id = offset_id - 1 if offset_id else 0
        iter_offset_date = offset_date

    async with channel_session(session_file) as (client, _):
        target = await resolve_group_target(client, channel, group)
        # DB after the target resolved — see the same note in `scrape.ingest`.
        conn = open_db(output_dir, handle)
        try:
            log.info(
                "authenticated, scanning group %s (%s)",
                target.title or target.link, handle,
            )
            raw = [
                m
                async for m in client.iter_messages(
                    target.entity,
                    limit=iter_limit,
                    reverse=reverse,
                    offset_id=iter_offset_id,
                    offset_date=iter_offset_date,
                )
            ]
            log.info("fetched %d group messages", len(raw))

            service = [m for m in raw if isinstance(m, MessageService)]
            ordinary = [m for m in raw if isinstance(m, Message)]
            # Snapshot the window NOW: back-fetched out-of-window roots get
            # appended to `ordinary` below, and their (much older) ids would
            # otherwise drag `lo` down — making the thread-stats query and
            # the reported id_range cover prior scans' rows, not this one's.
            scanned_ids = [m.id for m in raw]

            events = [e for m in service for e in classify_service_message(m)]
            admin_events, users = await _fetch_admin_log_events(
                client, target.entity
            )
            if admin_events:
                events.extend(
                    _dedupe_admin_events(conn, admin_events, events)
                )
            # Admin-log responses carry the subjects' user objects; only
            # resolve the (rare) uids the log didn't cover.
            unresolved = [
                e for e in events
                if e.user_id is not None and e.user_id not in users
            ]
            users.update(await _resolve_event_users(client, unresolved))

            root_map = {
                m.id: pid
                for m in ordinary
                if (pid := auto_forward_post_id(m, target.channel_id))
            }
            # Comments whose thread root fell outside the window: fetch the
            # referenced heads once and keep the ones that are real roots.
            missing = unresolved_root_refs(ordinary, root_map)
            if missing and target.channel_id is not None:
                log.info("resolving %d out-of-window thread heads", len(missing))
                fetched = await client.get_messages(
                    target.entity, ids=sorted(missing)
                )
                for m in fetched:
                    if not isinstance(m, Message):
                        continue
                    pid = auto_forward_post_id(m, target.channel_id)
                    if pid:
                        root_map[m.id] = pid
                        ordinary.append(m)

            rows = [_group_message_row(m, root_map) for m in ordinary]
            upsert_group_messages(conn, rows)
            upsert_group_events(conn, events, users)
            insert_group_metrics(conn, scrape_date, target)
            conn.commit()
            log.info(
                "stored %d message(s), %d event(s) in %s",
                len(rows), len(events), db_path_for(output_dir, handle),
            )

            lo, hi = (
                (min(scanned_ids), max(scanned_ids)) if scanned_ids else (0, 0)
            )
            threads = (
                _load_thread_stats(conn, lo, hi, handle)
                if target.channel_id is not None
                else []
            )
        finally:
            conn.close()

    overview = {
        "title": target.title,
        "link": target.link,
        "members": target.members,
        "standalone": target.channel_id is None,
        "id_range": f"{lo}..{hi}" if scanned_ids else "—",
    }
    messages = [
        {
            "id": r[0], "date": r[1], "text": r[2],
            "author": r[5] or r[4] or (str(r[3]) if r[3] else None),
            "reply_to_msg_id": r[6], "thread_post_id": r[7],
            "is_thread_root": r[8], "reactions": r[9],
        }
        for r in rows
    ]
    event_dicts = [
        {
            "kind": e.kind, "via": e.via, "date": e.date,
            "author": (users.get(e.user_id) or (None, None))[1]
            or (users.get(e.user_id) or (None, None))[0]
            or (str(e.user_id) if e.user_id else None),
        }
        for e in events
    ]
    return GroupScanResult(handle, overview, messages, event_dicts, threads)
