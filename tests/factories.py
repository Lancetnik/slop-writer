"""Test doubles for the Telegram side.

The split here is the whole faking policy, and it is not the binary the
ticket posed ("hand-rolled fake vs recorded fixtures"):

* **Messages are real Telethon TL objects.** `complete_albums` and
  `scan_group` gate on `isinstance(m, Message)` / `MessageService`, so a
  hand-rolled stand-in would either have to subclass the real type anyway or
  make the test vacuous. Telethon is already a hard dependency, so building
  the real thing costs nothing. The one quirk the factories hide: `Message.text`
  is a *property* that returns None until a client is attached — the setter
  fills `message`/`entities` directly, which is what `_msg` below uses. Every
  `msg.text or ""` in the package depends on it.
* **The client is faked**, by hand, exposing only the coroutines the unit
  under test actually awaits. This is the network boundary, and it is the one
  place recorded fixtures were the alternative — rejected because a recording
  pins Telethon's wire format, and the dependency pin (`telethon>=1.36,<2`)
  already says a major-version move is expected.

No fixture here needs a session, credentials, or a DB under `.tg-analytic/`.
"""

from datetime import UTC, datetime

from telethon.tl.types import (
    Message,
    MessageFwdHeader,
    MessageMediaPhoto,
    MessageReactions,
    MessageReplies,
    MessageReplyHeader,
    MessageService,
    PeerChannel,
    PeerUser,
    ReactionCount,
    ReactionEmoji,
    ReactionPaid,
)

# Fixed so nothing in the suite depends on wall-clock time.
DATE = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
CHANNEL_ID = 1001
GROUP_ID = 2002


def msg(
    id: int,
    *,
    text: str = "",
    grouped_id: int | None = None,
    date: datetime | None = None,
    media: object | None = None,
    views: int | None = None,
    forwards: int | None = None,
    edit_date: datetime | None = None,
    reply_to_msg_id: int | None = None,
    reply_to_top_id: int | None = None,
    fwd_channel_post: int | None = None,
    fwd_channel_id: int | None = None,
    reactions: int = 0,
    stars: int = 0,
    replies: int | None = None,
    sender_id: int | None = None,
) -> Message:
    """A real `telethon.tl.types.Message` with only the fields the domain reads."""
    reply = None
    if reply_to_msg_id is not None or reply_to_top_id is not None:
        reply = MessageReplyHeader(
            reply_to_msg_id=reply_to_msg_id, reply_to_top_id=reply_to_top_id
        )
    fwd = None
    if fwd_channel_post is not None or fwd_channel_id is not None:
        fwd = MessageFwdHeader(
            date=date or DATE,
            from_id=PeerChannel(fwd_channel_id) if fwd_channel_id else None,
            channel_post=fwd_channel_post,
        )
    counts = []
    if reactions:
        counts.append(ReactionCount(reaction=ReactionEmoji("🔥"), count=reactions))
    if stars:
        counts.append(ReactionCount(reaction=ReactionPaid(), count=stars))

    m = Message(
        id=id,
        peer_id=PeerChannel(CHANNEL_ID),
        date=date or DATE,
        message="",
        grouped_id=grouped_id,
        media=media,
        views=views,
        forwards=forwards,
        edit_date=edit_date,
        reply_to=reply,
        fwd_from=fwd,
        reactions=MessageReactions(results=counts) if counts else None,
        replies=(
            MessageReplies(comments=True, replies=replies, replies_pts=0)
            if replies is not None
            else None
        ),
        from_id=PeerUser(sender_id) if sender_id is not None else None,
    )
    # Property, not a plain field — see the module docstring.
    m.text = text
    return m


def service(
    id: int,
    action: object,
    *,
    sender_id: int | None = None,
    date: datetime | None = None,
) -> MessageService:
    """A real `MessageService`; `sender_id` is the actor, `None` = no actor."""
    return MessageService(
        id=id,
        peer_id=PeerChannel(GROUP_ID),
        date=date or DATE,
        action=action,
        from_id=PeerUser(sender_id) if sender_id is not None else None,
    )


def photo() -> MessageMediaPhoto:
    """Enough of a photo for `messages.media_type` to say "photo"."""
    return MessageMediaPhoto(photo=None)


class AdminLogEvent:
    """Duck-typed admin-log event.

    Faked rather than built for real: `classify_admin_log_event` dispatches on
    `type(action).__name__` and reads everything through `getattr`, so the
    plain object *is* the shape the function was written against. Constructing
    a real `ChannelAdminLogEvent` would add required fields the code never
    looks at.
    """

    def __init__(self, id: int, action: object, *, user_id: int | None = None,
                 date: datetime | None = None) -> None:
        self.id = id
        self.action = action
        self.user_id = user_id
        self.date = date or DATE


class Named:
    """An action object identified only by its class name.

    `Named("ChannelAdminLogEventActionParticipantJoin")` classifies exactly as
    the Telethon type would, because the name is all the dispatch reads.
    """

    def __new__(cls, name: str, **fields):
        obj = super().__new__(type(name, (cls,), {}))
        obj.__dict__.update(fields)
        return obj

    def __init__(self, name: str, **fields) -> None:  # noqa: D107 - see __new__
        pass


class FakeClient:
    """Only `get_messages`, which is all `complete_albums` awaits.

    `responses` maps a requested-id tuple to what Telegram returns; anything
    unmapped comes back as a list of Nones, which is how the real API reports
    ids that do not exist. `calls` records every request so a test can assert
    that no round-trip happened at all.
    """

    def __init__(self, responses: dict[tuple[int, ...], list] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[list[int]] = []

    async def get_messages(self, entity, ids=None):
        self.calls.append(list(ids))
        return self.responses.get(tuple(ids), [None] * len(ids))
