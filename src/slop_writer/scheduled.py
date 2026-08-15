"""Reading the channel's scheduled queue.

Read-only, and kept out of `publish` on purpose: docs/adr/0003 wants "this code
can post" auditable at the file level, so the module that lists the queue must
be importable without pulling the module that writes to it. The dependency runs
one way — `publish` reads the queue through `get_scheduled_message` here.
"""

import logging
from dataclasses import dataclass

from telethon.tl.functions.messages import (
    GetScheduledHistoryRequest,
    GetScheduledMessagesRequest,
)
from telethon.tl.types import Message

from .errors import SlopWriterError
from .messages import group_albums, media_desc
from .tg import channel_session

log = logging.getLogger(__name__)


@dataclass
class ScheduledQueue:
    """Shapes `render.summarize_scheduled` consumes."""
    channel: str
    items: list[dict]


async def list_scheduled(channel: str, session_file: str) -> ScheduledQueue:
    """The channel's scheduled (not-yet-published) posts.

    Calls messages.GetScheduledHistory directly rather than
    `iter_messages(..., scheduled=True)`: the iterator assumes the normal
    newest-first (descending-id) order and stops after the first message once
    ids start increasing, but scheduled history comes back oldest-first, so the
    iterator only ever yields one post. The raw request returns the whole queue
    in one round-trip. It only returns rows to an account with post rights on
    the channel. Scheduled posts carry no views/forwards/reactions and their
    ids are *scheduled-message* ids (distinct from the id a post gets once
    published), so we don't persist them — this is a read-only peek."""
    async with channel_session(session_file, channel) as (client, entity):
        log.info("authenticated, listing scheduled posts for %s", channel)
        try:
            result = await client(
                GetScheduledHistoryRequest(peer=entity, hash=0)
            )
        except Exception as e:
            raise SlopWriterError(
                f"failed to list scheduled posts ({e})",
                hint="You need post rights on the channel to see its "
                "scheduled queue.",
                code="NO_POST_RIGHTS",
            ) from None
        raw: list[Message] = [
            m for m in getattr(result, "messages", []) if isinstance(m, Message)
        ]

    items: list[dict] = []
    for group in group_albums(raw):
        group.sort(key=lambda m: m.id)
        # Raw messages from GetScheduledHistory aren't client-bound, so the
        # `.text` property is None; the plain body lives in `.message`.
        parent = next((m for m in group if m.message), group[0])
        attachments = [d for m in group if m.media is not None if (d := media_desc(m))]
        text = parent.message or ""
        items.append(
            {
                "id": parent.id,
                "date": parent.date.isoformat() if parent.date else None,
                "text": text,
                "attachments": attachments,
            }
        )
    items.sort(key=lambda i: (i["date"] or "", i["id"]))

    return ScheduledQueue(channel, items)


async def get_scheduled_message(client, entity, msg_id: int) -> Message:
    """Fetch one post from the channel's scheduled queue by its sched-msg id.

    One round-trip via GetScheduledMessages (no full-history scan). Fails with
    an actionable message if nothing matches — the id is most likely stale
    (the post published, or was already removed).

    The hint names the *operation*, not the surface that performs it: listing
    the queue is a tool for one caller and a command for the other, and this
    function knows which one it is serving no more than `errors.py` does."""
    result = await client(GetScheduledMessagesRequest(peer=entity, id=[msg_id]))
    found = [
        m
        for m in getattr(result, "messages", [])
        if isinstance(m, Message) and m.id == msg_id
    ]
    if not found:
        raise SlopWriterError(
            f"No scheduled post #{msg_id} in the queue.",
            hint="List the channel's scheduled queue again and use a current "
            "id — these are sched-msg ids, and they go stale the moment a post "
            "publishes or is removed.",
            code="NO_SUCH_MESSAGE",
        )
    return found[0]
