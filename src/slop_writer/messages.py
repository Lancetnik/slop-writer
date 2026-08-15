"""Telethon `Message` -> plain Python fields.

The primitives every ingest path shares: what kind of media a message carries,
its reactions, its sender, the album it belongs to. Nothing here touches the
DB or the network, so both the post pipeline (`scrape`) and the discussion-group
scan (`group`) can import it without either importing the other.
"""

import re

from telethon.tl.types import (
    Message,
    MessageMediaDocument,
    MessageMediaPhoto,
    ReactionPaid,
)


def media_type(msg: Message) -> str | None:
    if not msg.media:
        return None
    if isinstance(msg.media, MessageMediaPhoto):
        return "photo"
    if isinstance(msg.media, MessageMediaDocument):
        return "document"
    return type(msg.media).__name__


def tme_link(channel: str, msg_id: int) -> str:
    return f"https://t.me/{channel.lstrip('@')}/{msg_id}"


def extract_tags(text: str) -> list[str]:
    return re.findall(r"(?<!\S)#(\w+)", text)


def count_reactions(msg: Message) -> tuple[int, int]:
    reactions = stars = 0
    if msg.reactions:
        for r in msg.reactions.results:
            if isinstance(r.reaction, ReactionPaid):
                stars += r.count
            else:
                reactions += r.count
    return reactions, stars


def sender_fields(msg: Message) -> tuple[int | None, str | None, str | None]:
    """(user_id, display name, username) for a message's sender — shared by
    the group scan and the scrape-side comment path."""
    sender = msg.sender
    if sender is None:
        peer = getattr(msg, "from_id", None)
        return getattr(peer, "user_id", None), None, None
    first = getattr(sender, "first_name", "") or ""
    last = getattr(sender, "last_name", "") or ""
    name = (first + " " + last).strip() or getattr(sender, "title", None)
    return sender.id, name or None, getattr(sender, "username", None)


def group_albums(messages: list[Message]) -> list[list[Message]]:
    """Group album members by grouped_id; standalone posts become singletons.

    Shared by the persist pipeline and the scheduled-queue listing so the album
    invariant (one logical post per grouped_id) lives in one place."""
    groups: dict[int, list[Message]] = {}
    standalone: list[Message] = []
    for msg in messages:
        if msg.grouped_id:
            groups.setdefault(msg.grouped_id, []).append(msg)
        else:
            standalone.append(msg)
    return [[m] for m in standalone] + list(groups.values())


def media_desc(msg: Message) -> str | None:
    """Human-readable one-liner for a message's attachment."""
    mt = media_type(msg)
    if mt is None:
        return None
    if isinstance(msg.media, MessageMediaDocument):
        doc = msg.media.document
        name = next(
            (
                fn
                for attr in getattr(doc, "attributes", [])
                if (fn := getattr(attr, "file_name", None))
            ),
            None,
        )
        size = getattr(doc, "size", None)
        mime = getattr(doc, "mime_type", None)
        parts = [name or mime or "document"]
        if size:
            parts.append(f"({size:,} bytes)")
        return " ".join(parts)
    return mt
