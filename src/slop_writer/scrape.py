"""The post pipeline: fetch a channel's posts, persist them, summarize them.

Two entrypoints, one lifecycle. `scrape_posts` walks a selection window;
`refresh_posts` re-fetches known ids. They differ only in the message-source
adapter they hand `ingest_with_client`, and in whether the window can prove an
album whole (see `complete_albums`).

Each entrypoint comes in two forms: a `*_with_client` function that takes an
already-connected client, and a same-named session-owning shell for callers
that have none. The connection belongs to whoever owns it - one session per
run for a CLI, one long-lived client for a server, a fake for a test.

Comments land in `group_messages` (docs/adr/0002), so the thread writer here
goes through `group.upsert_group_messages` — one table, one writer.
"""

import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from telethon import TelegramClient
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.stats import GetMessagePublicForwardsRequest
from telethon.tl.types import (
    Message,
    MessageMediaPhoto,
    PeerChannel,
    PublicForwardMessage,
)

from .db import heal_album_phantoms, open_db
from .group import message_row, upsert_group_messages
from .messages import (
    count_reactions,
    extract_tags,
    group_albums,
    media_type,
    tme_link,
)
from .tg import channel_session

log = logging.getLogger(__name__)

# Per-post progress prints every Nth post at INFO; per-post lines go to DEBUG.
PROGRESS_EVERY = 50

#: What a caller passes to watch a long run: awaited with (done, total) at the
#: same points the INFO progress line is logged. The CLI passes nothing (its
#: progress *is* the log line on stderr); the MCP server passes a callback that
#: sends an MCP progress notification, which is what keeps a multi-minute
#: scrape from tripping the client's idle watchdog.
ProgressHook = Callable[[int, int], Awaitable[None]]

# Telegram caps an album at 10 items, so a window that cuts one can be missing
# at most 9 members on either side of what it returned.
ALBUM_MAX_ITEMS = 10


def _log_progress(done: int, total: int, current: str, id_range: str) -> None:
    log.debug("[%d/%d] processed %s", done, total, current)
    if done == total or done % PROGRESS_EVERY == 0:
        log.info("[%d/%d] processed (ids: %s)", done, total, id_range)


@dataclass
class ForwardInfo:
    msg_link: str
    channel_link: str
    peer: object


@dataclass
class ChannelInfo:
    name: str | None
    description: str | None
    subscribers: int | None


@dataclass
class ChannelRecord:
    peer: object
    post_ids: list[int] = field(default_factory=list)


@dataclass
class ScrapeResult:
    """What one run produced, in the shapes `render.summarize_scrape` consumes.

    Returned rather than printed: the CLI renders it to stdout, and a second
    caller can do something else with the same run.

    `history_exhausted` is what makes a short run readable: True means the walk
    ran out of channel to read, False means it stopped at the requested count
    with history still beyond it. `None` is "this run did not walk a window" —
    a refresh of known ids, which can say nothing either way."""
    channel: str
    posts: list[dict]
    channels: list[dict]
    history_exhausted: bool | None = None


async def take_posts(stream, want: int | None) -> tuple[list[Message], bool]:
    """Read a message stream until `want` *posts* are whole, or it runs dry.

    Telethon's `limit` counts messages, and an album is many messages but one
    post - so a straight `limit=N` over a channel that posts albums stores
    fewer than N posts, and the shortfall is indistinguishable from a channel
    that simply holds no more. Counting posts instead gives a short run exactly
    one meaning: the history ended. That is the second half of the returned
    pair, and `summarize_scrape` is what states it.

    Album members carry contiguous ids, so a key the stream has not shown yet
    closes the previous post: accept every member of the post being read, and
    stop on the first message of the one past the count. The trailing post is
    therefore whole on this side; `complete_albums` covers the other one.
    """
    raw: list[Message] = []
    seen: set[tuple[str, int]] = set()
    async for msg in stream:
        key = ("album", msg.grouped_id) if msg.grouped_id else ("post", msg.id)
        if key not in seen:
            if want is not None and len(seen) >= want:
                return raw, False
            seen.add(key)
        raw.append(msg)
    return raw, True


async def complete_albums(
    client: TelegramClient,
    entity,
    groups: list[list[Message]],
    fetched_ids: set[int],
    window_contiguous: bool,
) -> list[list[Message]]:
    """Re-fetch album members that fell outside the selection window.

    A window whose edge cuts an album used to hand `process_post` a *suffix*
    of it, and the captionless first element of that suffix became a post row
    of its own - a phantom carrying the album's grouped_id, an empty text, its
    own `post_metrics` series (Telegram reports views/forwards on every album
    member, so nothing looked wrong) and a duplicate slice of the album's
    attachments. Every SUM/AVG over posts then counted the album twice.
    Pulling the missing members back in restores "one row per album, owned by
    the caption carrier" no matter where the window cut.

    Probing is skipped when the window already proves the album is whole: over
    a contiguous scrape window, a fetched id below (above) the group means the
    neighbour on that side was seen and is not an album member. Refreshing
    arbitrary ids can prove nothing, so both sides get probed."""
    completed: list[list[Message]] = []
    for group in groups:
        gid = group[0].grouped_id
        if not gid or len(group) >= ALBUM_MAX_ITEMS:
            completed.append(group)
            continue
        lo = min(m.id for m in group)
        hi = max(m.id for m in group)
        room = ALBUM_MAX_ITEMS - len(group)
        wanted: list[int] = []
        if not (window_contiguous and any(i < lo for i in fetched_ids)):
            wanted += [i for i in range(lo - room, lo) if i > 0]
        if not (window_contiguous and any(i > hi for i in fetched_ids)):
            wanted += list(range(hi + 1, hi + 1 + room))
        wanted = [i for i in wanted if i not in fetched_ids]
        if not wanted:
            completed.append(group)
            continue
        # One round-trip per truncated album; missing ids come back as None.
        extra = [
            m
            for m in await client.get_messages(entity, ids=wanted)
            if isinstance(m, Message) and m.grouped_id == gid
        ]
        if extra:
            log.info(
                "album %s was cut by the window: pulled in %s to join %s",
                gid,
                sorted(m.id for m in extra),
                sorted(m.id for m in group),
            )
        completed.append(group + extra)
    return completed


async def get_forward_source(
    client: TelegramClient, msg: Message
) -> ForwardInfo | None:
    """If `msg` is itself a forward of a channel post, resolve the source.

    Returns the source channel as a ForwardInfo so the caller can register it
    in the same channel_map used for outbound forwarders - the source then
    gets persisted to `public_channels` alongside everything else.
    Returns None for non-channel forwards (user/chat) and hidden senders."""
    fwd = msg.fwd_from
    if not fwd or not getattr(fwd, "from_id", None):
        return None
    peer = fwd.from_id
    if not isinstance(peer, PeerChannel):
        return None

    username: str | None = None
    try:
        entity = await client.get_entity(peer)
        username = getattr(entity, "username", None)
    except Exception as e:
        # Private/restricted channels still leave us with channel_id, so we
        # can persist them under the `t.me/c/<id>` form.
        log.error("msg %d: failed to resolve fwd_from entity (%s)", msg.id, e)

    ch_link = (
        f"https://t.me/{username}"
        if username
        else f"https://t.me/c/{peer.channel_id}"
    )
    src_msg_id = getattr(fwd, "channel_post", None)
    return ForwardInfo(
        msg_link=f"{ch_link}/{src_msg_id}" if src_msg_id else "",
        channel_link=ch_link,
        peer=peer,
    )


async def get_public_forwards(
    client: TelegramClient, channel_entity, msg_id: int
) -> list[ForwardInfo]:
    result_list: list[ForwardInfo] = []
    offset = ""
    try:
        # The API pages 100 forwards at a time; an empty next_offset ends
        # the walk. A mid-walk error keeps the pages fetched so far.
        while True:
            result = await client(
                GetMessagePublicForwardsRequest(
                    channel=channel_entity,
                    msg_id=msg_id,
                    offset=offset,
                    limit=100,
                )
            )
            for fwd in result.forwards:
                if not isinstance(fwd, PublicForwardMessage):
                    continue
                peer = fwd.message.peer_id
                try:
                    entity = await client.get_entity(peer)
                    username = getattr(entity, "username", None)
                    ch_link = (
                        f"https://t.me/{username}"
                        if username
                        else f"https://t.me/c/{peer.channel_id}"
                    )
                    result_list.append(
                        ForwardInfo(
                            msg_link=f"{ch_link}/{fwd.message.id}",
                            channel_link=ch_link,
                            peer=peer,
                        )
                    )
                except Exception as e:
                    log.error("msg %d: failed to resolve forward peer (%s)", msg_id, e)
            offset = getattr(result, "next_offset", None) or ""
            if not offset or not result.forwards:
                break
        if result_list:
            log.debug("msg %d: %d public forward(s)", msg_id, len(result_list))
    except Exception as e:
        log.error("msg %d: public forwards request failed (%s)", msg_id, e)
    return result_list


async def get_comments(
    client: TelegramClient, channel_entity, msg: Message
) -> list[Message]:
    """One post's comment thread — group-side Message objects, sorted by id.

    Sender extraction happens later in `messages.sender_fields` (shared with
    the group scan), so this stays a plain fetch."""
    if not (msg.replies and msg.replies.replies):
        return []
    comments: list[Message] = []
    try:
        async for c in client.iter_messages(channel_entity, reply_to=msg.id):
            if isinstance(c, Message):
                comments.append(c)
    except Exception as e:
        log.error("msg %d: failed to fetch comments (%s)", msg.id, e)
    comments.sort(key=lambda c: c.id)
    if comments:
        log.debug("msg %d: %d comment(s)", msg.id, len(comments))
    return comments


async def get_channel_info(client: TelegramClient, peer) -> ChannelInfo:
    try:
        full = await client(GetFullChannelRequest(peer))
        return ChannelInfo(
            name=full.chats[0].title if full.chats else None,
            description=full.full_chat.about or None,
            subscribers=full.full_chat.participants_count,
        )
    except Exception as e:
        log.error("failed to get channel info (%s)", e)
        return ChannelInfo(name=None, description=None, subscribers=None)


async def download_photo(
    client: TelegramClient, msg: Message, media_dir: Path, with_media: bool
) -> str | None:
    if not with_media or not isinstance(msg.media, MessageMediaPhoto):
        return None
    media_dir.mkdir(parents=True, exist_ok=True)
    dest = media_dir / f"{msg.id}.jpg"
    if dest.exists():
        log.debug("msg %d: photo already cached", msg.id)
        return str(dest)
    log.debug("msg %d: downloading photo", msg.id)
    path = await client.download_media(msg, file=str(dest))
    return str(path) if path else None


# ---------------------------------------------------------------------------
# DB writes
# ---------------------------------------------------------------------------


def upsert_post(
    conn: sqlite3.Connection,
    channel: str,
    msg: Message,
    attachments: list[tuple[int, str, str | None, str | None]],
    forwarder_from_channel: str | None = None,
) -> None:
    text = msg.text or ""
    tags = extract_tags(text)
    conn.execute(
        """
        INSERT INTO posts (
            id, link, date, text, edit_date,
            reply_to_msg_id, tags, grouped_id, forwarder_from_channel
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            link                   = excluded.link,
            date                   = excluded.date,
            text                   = excluded.text,
            edit_date              = excluded.edit_date,
            reply_to_msg_id        = excluded.reply_to_msg_id,
            tags                   = excluded.tags,
            grouped_id             = excluded.grouped_id,
            forwarder_from_channel = excluded.forwarder_from_channel
        """,
        (
            msg.id,
            tme_link(channel, msg.id),
            msg.date.isoformat() if msg.date else None,
            text,
            (
                msg.edit_date.isoformat()
                if msg.edit_date and msg.edit_date != msg.date
                else None
            ),
            msg.reply_to_msg_id,
            json.dumps(tags, ensure_ascii=False) if tags else None,
            msg.grouped_id,
            forwarder_from_channel,
        ),
    )

    # Replace attachments wholesale - cheaper than diffing and matches re-scrape
    # semantics. Rows are cleared by attachment_id too, not just post_id: an
    # attachment belongs to exactly one post, so this drops copies a pre-fix run
    # filed under a phantom album member (see `complete_albums`).
    att_ids = [att_id for att_id, _, _, _ in attachments]
    conn.execute(
        "DELETE FROM post_attachments WHERE post_id = ?"
        + (
            f" OR attachment_id IN ({','.join('?' * len(att_ids))})"
            if att_ids
            else ""
        ),
        (msg.id, *att_ids),
    )
    conn.executemany(
        """
        INSERT INTO post_attachments (
            post_id, attachment_id, link, media_type, photo_path
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            (msg.id, att_id, link, mtype, photo)
            for att_id, link, mtype, photo in attachments
        ],
    )


def insert_metrics(
    conn: sqlite3.Connection,
    msg: Message,
    scrape_date: str,
    comments_count: int,
    public_forwards_count: int,
) -> None:
    reactions, stars = count_reactions(msg)
    conn.execute(
        """
        INSERT INTO post_metrics (
            post_id, scrape_date, views, forwards,
            reactions, stars, comments_count, public_forwards_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            msg.id,
            scrape_date,
            msg.views,
            msg.forwards,
            reactions,
            stars,
            comments_count,
            public_forwards_count,
        ),
    )


def replace_thread_comments(
    conn: sqlite3.Connection,
    post_id: int,
    comments: list[Message],
) -> None:
    """Replace one post's comment thread in group_messages.

    The scoped DELETE keeps the old per-post deletion tracking (a comment
    removed on Telegram disappears on re-scrape) without touching thread
    roots or top-level chatter, which the group scan owns."""
    conn.execute(
        "DELETE FROM group_messages"
        " WHERE thread_post_id = ? AND is_thread_root = 0",
        (post_id,),
    )
    upsert_group_messages(
        conn,
        [message_row(m, thread_post=post_id, is_root=False) for m in comments],
    )


def upsert_public_shares(
    conn: sqlite3.Connection,
    post_id: int,
    forwards: list[ForwardInfo],
    seen_at: str,
) -> None:
    conn.executemany(
        """
        INSERT INTO public_shares (
            post_id, forwarder_link, msg_link, first_seen
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(post_id, forwarder_link, msg_link) DO NOTHING
        """,
        [(post_id, f.channel_link, f.msg_link, seen_at) for f in forwards],
    )


def upsert_public_channel(
    conn: sqlite3.Connection, link: str, info: ChannelInfo, seen_at: str
) -> None:
    conn.execute(
        """
        INSERT INTO public_channels (link, name, description, subscribers, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(link) DO UPDATE SET
            name        = COALESCE(excluded.name, public_channels.name),
            description = COALESCE(excluded.description, public_channels.description),
            subscribers = COALESCE(excluded.subscribers, public_channels.subscribers),
            last_seen   = excluded.last_seen
        """,
        (link, info.name, info.description, info.subscribers, seen_at),
    )


# ---------------------------------------------------------------------------
# Ingestion pipeline (shared by the window scrape and the id refresh)
# ---------------------------------------------------------------------------


def _register_forwards(
    fwd_data: list[ForwardInfo],
    post_id: int,
    channel_map: dict[str, ChannelRecord],
) -> None:
    for fwd in fwd_data:
        if fwd.channel_link not in channel_map:
            channel_map[fwd.channel_link] = ChannelRecord(peer=fwd.peer)
        record = channel_map[fwd.channel_link]
        if post_id not in record.post_ids:
            record.post_ids.append(post_id)


def _post_summary(
    msg: Message,
    channel: str,
    comments_count: int,
    forwarder_from_channel: str | None,
) -> dict:
    """Compact per-post record used by the stdout summary."""
    text = msg.text or ""
    reactions, stars = count_reactions(msg)
    return {
        "id": msg.id,
        "link": tme_link(channel, msg.id),
        "date": msg.date.isoformat() if msg.date else None,
        "text": text,
        "views": msg.views,
        "forwards": msg.forwards,
        "reactions": reactions,
        "stars": stars,
        "tags": extract_tags(text),
        "comments_count": comments_count,
        "forwarder_from_channel": forwarder_from_channel,
    }


async def process_post(
    client: TelegramClient,
    channel_entity,
    channel: str,
    group: list[Message],
    conn: sqlite3.Connection,
    channel_map: dict[str, ChannelRecord],
    media_dir: Path,
    scrape_date: str,
    with_comments: bool,
    with_media: bool,
) -> dict:
    # One row per album, owned by its caption carrier (the lowest id when the
    # album has no caption at all). `complete_albums` guarantees `group` holds
    # every member, so this pick is the same whatever the window was - no
    # non-head member ever reaches `posts`.
    group.sort(key=lambda m: m.id)
    parent = next((m for m in group if m.text), group[0])

    attachments: list[tuple[int, str, str | None, str | None]] = []
    for m in group:
        if m.media is None:
            continue
        photo = await download_photo(client, m, media_dir, with_media)
        attachments.append((m.id, tme_link(channel, m.id), media_type(m), photo))

    fwd_data = (
        await get_public_forwards(client, channel_entity, parent.id)
        if parent.forwards
        else []
    )
    _register_forwards(fwd_data, parent.id, channel_map)

    # If this post is itself a forward of another channel, capture the source
    # channel link and make sure that channel exists in `public_channels`.
    fwd_source = await get_forward_source(client, parent)
    forwarder_from_channel: str | None = None
    if fwd_source is not None:
        forwarder_from_channel = fwd_source.channel_link
        if fwd_source.channel_link not in channel_map:
            channel_map[fwd_source.channel_link] = ChannelRecord(peer=fwd_source.peer)

    comments = (
        await get_comments(client, channel_entity, parent) if with_comments else []
    )
    # Without comments we still know the count from the post itself; writing
    # 0 would poison the post_metrics time-series.
    comments_count = (
        len(comments)
        if with_comments
        else (parent.replies.replies if parent.replies else 0)
    )

    upsert_post(conn, channel, parent, attachments, forwarder_from_channel)
    insert_metrics(conn, parent, scrape_date, comments_count, len(fwd_data))
    if with_comments:
        replace_thread_comments(conn, parent.id, comments)
    upsert_public_shares(conn, parent.id, fwd_data, scrape_date)
    conn.commit()

    return _post_summary(parent, channel, comments_count, forwarder_from_channel)


async def _persist_messages(
    client: TelegramClient,
    channel_entity,
    channel: str,
    raw: list[Message],
    conn: sqlite3.Connection,
    media_dir: Path,
    scrape_date: str,
    with_comments: bool,
    with_media: bool,
    with_channel_info: bool,
    window_contiguous: bool,
    on_progress: ProgressHook | None = None,
) -> tuple[list[dict], list[dict]]:
    """Group, persist and summarize a batch of fetched messages."""
    post_groups = await complete_albums(
        client,
        channel_entity,
        group_albums(raw),
        {m.id for m in raw},
        window_contiguous,
    )

    post_summaries: list[dict] = []
    channel_map: dict[str, ChannelRecord] = {}
    total = len(post_groups)
    done = 0
    all_ids = [m.id for g in post_groups for m in g]
    id_range = f"{min(all_ids)}..{max(all_ids)}" if all_ids else "—"

    for group in post_groups:
        summary = await process_post(
            client, channel_entity, channel, group, conn, channel_map,
            media_dir, scrape_date, with_comments, with_media,
        )
        post_summaries.append(summary)
        done += 1
        label = (
            f"msg {group[0].id}"
            if len(group) == 1
            else f"group {sorted(m.id for m in group)}"
        )
        _log_progress(done, total, label, id_range)
        if on_progress is not None:
            await on_progress(done, total)

    log.debug("resolving %d forwarding channels", len(channel_map))
    channel_summaries: list[dict] = []
    for ch_link, record in channel_map.items():
        info = (
            await get_channel_info(client, record.peer)
            if with_channel_info
            else ChannelInfo(name=None, description=None, subscribers=None)
        )
        upsert_public_channel(conn, ch_link, info, scrape_date)
        channel_summaries.append(
            {
                "link": ch_link,
                "name": info.name,
                "subscribers": info.subscribers,
                "shared_posts": sorted(record.post_ids),
            }
        )
    conn.commit()
    # open_db healed what the DB already held; this catches the album whose
    # real head only arrived in *this* run, next to an older phantom.
    heal_album_phantoms(conn)
    channel_summaries.sort(key=lambda c: c["link"])
    post_summaries.sort(key=lambda p: p["id"])
    return post_summaries, channel_summaries


async def ingest_with_client(
    client: TelegramClient,
    entity,
    channel: str,
    output_dir: Path,
    source,
    with_comments: bool,
    with_media: bool,
    with_channel_info: bool,
    window_contiguous: bool = True,
    on_progress: ProgressHook | None = None,
) -> ScrapeResult:
    """One run over an already-connected client.

    Opens the DB, pulls messages via `source(client, entity)`, persists them,
    and returns the summary. `scrape_posts` and `refresh_posts` differ only in
    their message-source adapter - and in `window_contiguous`, which tells
    `complete_albums` whether the ids around a group can be trusted to prove
    the album is whole (true for a scrape window, false for arbitrary ids).

    A source answers with `(messages, history_exhausted)`: whether the walk ran
    out of channel is known only to whoever did the walking, and a source that
    walks no window (a refresh of known ids) says `None` rather than guessing.

    The client is a parameter rather than something this function opens, so
    whoever owns the connection decides its lifetime - see the module
    docstring. Callers must have resolved the handle already: `open_db` below
    is what would otherwise leave a stray `.tg-analytic/<typo>.db` behind."""
    scrape_date = datetime.now(UTC).isoformat()
    conn = open_db(output_dir, channel)
    try:
        raw, history_exhausted = await source(client, entity)
        post_summaries, channel_summaries = await _persist_messages(
            client, entity, channel, raw, conn, output_dir / "media",
            scrape_date, with_comments, with_media, with_channel_info,
            window_contiguous, on_progress,
        )
    finally:
        conn.close()

    return ScrapeResult(
        channel, post_summaries, channel_summaries, history_exhausted
    )


async def scrape_posts_with_client(
    client: TelegramClient,
    entity,
    channel: str,
    output_dir: Path,
    limit: int | None = None,
    offset_id: int = 0,
    offset_date: datetime | None = None,
    latest: int | None = None,
    with_comments: bool = True,
    with_media: bool = True,
    with_channel_info: bool = True,
    on_progress: ProgressHook | None = None,
) -> ScrapeResult:
    # `latest=N` flips iteration to newest-first to actually return the
    # most recent N posts. `limit=N` alone keeps the chronological
    # (oldest-first) walk, which is what you want when paging forward from
    # an offset.
    #
    # Both counts are *posts*, which is why neither reaches Telethon's `limit`:
    # `take_posts` walks the unbounded stream and stops on the post past the
    # count. It reads at most one extra batch, and it is what keeps "fewer than
    # asked for" meaning "the channel ended" over a channel that posts albums.
    if latest is not None:
        reverse = False
        want = latest
        iter_offset_id = 0
        iter_offset_date = None
    else:
        reverse = True
        want = limit
        # Telethon's offset_id is exclusive; -1 makes offset_id inclusive.
        iter_offset_id = offset_id - 1 if offset_id else 0
        iter_offset_date = offset_date

    async def source(client: TelegramClient, entity) -> tuple[list[Message], bool]:
        log.info("authenticated, scraping %s", channel)
        raw, history_exhausted = await take_posts(
            client.iter_messages(
                channel,
                limit=None,
                reverse=reverse,
                offset_id=iter_offset_id,
                offset_date=iter_offset_date,
            ),
            want,
        )
        log.info(
            "fetched %d messages (history %s)",
            len(raw),
            "exhausted" if history_exhausted else "continues past this window",
        )
        return raw, history_exhausted

    return await ingest_with_client(
        client, entity, channel, output_dir, source,
        with_comments, with_media, with_channel_info,
        window_contiguous=True, on_progress=on_progress,
    )


async def refresh_posts_with_client(
    client: TelegramClient,
    entity,
    channel: str,
    post_ids: list[int],
    output_dir: Path,
    with_comments: bool = True,
    with_media: bool = True,
    with_channel_info: bool = True,
    on_progress: ProgressHook | None = None,
) -> ScrapeResult:
    async def source(
        client: TelegramClient, entity
    ) -> tuple[list[Message], None]:
        log.info("authenticated, fetching %d post(s) from %s", len(post_ids), channel)
        # get_messages returns a parallel list; entries are None for missing ids.
        fetched = await client.get_messages(entity, ids=post_ids)
        raw: list[Message] = []
        missing: list[int] = []
        for req_id, msg in zip(post_ids, fetched):
            if isinstance(msg, Message):
                raw.append(msg)
            else:
                missing.append(req_id)
        if missing:
            log.warning("not found in channel: %s", missing)
        log.info("resolved %d/%d post(s)", len(raw), len(post_ids))
        # No window was walked, so this run knows nothing about what remains.
        return raw, None

    return await ingest_with_client(
        client, entity, channel, output_dir, source,
        with_comments, with_media, with_channel_info,
        window_contiguous=False, on_progress=on_progress,
    )


# The session-owning entrypoints. Each is its `*_with_client` twin with one
# `channel_session` wrapped around it, and the nesting is the invariant:
# `channel_session` resolves the handle *before* the body runs, and the body
# is where `open_db` lives - so a typo fails with `Cannot resolve @x` instead
# of leaving an empty `.tg-analytic/<typo>.db` behind. Keep that order when
# adding a command.


async def scrape_posts(
    channel: str,
    output_dir: Path,
    session_file: str,
    limit: int | None = None,
    offset_id: int = 0,
    offset_date: datetime | None = None,
    latest: int | None = None,
    with_comments: bool = True,
    with_media: bool = True,
    with_channel_info: bool = True,
    on_progress: ProgressHook | None = None,
) -> ScrapeResult:
    async with channel_session(session_file, channel) as (client, entity):
        return await scrape_posts_with_client(
            client, entity, channel, output_dir,
            limit, offset_id, offset_date, latest,
            with_comments, with_media, with_channel_info, on_progress,
        )


async def refresh_posts(
    channel: str,
    post_ids: list[int],
    output_dir: Path,
    session_file: str,
    with_comments: bool = True,
    with_media: bool = True,
    with_channel_info: bool = True,
    on_progress: ProgressHook | None = None,
) -> ScrapeResult:
    async with channel_session(session_file, channel) as (client, entity):
        return await refresh_posts_with_client(
            client, entity, channel, post_ids, output_dir,
            with_comments, with_media, with_channel_info, on_progress,
        )
