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

from contextlib import asynccontextmanager
from datetime import UTC, datetime

from telethon.tl.types import (
    Channel,
    Message,
    MessageFwdHeader,
    MessageMediaPhoto,
    MessageReactions,
    MessageReplies,
    MessageReplyHeader,
    MessageService,
    PeerChannel,
    PeerUser,
    PublicForwardMessage,
    ReactionCount,
    ReactionEmoji,
    ReactionPaid,
    User,
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


def channel(id: int = CHANNEL_ID, *, title: str = "The Channel",
            username: str | None = "chan") -> Channel:
    """A real `Channel`: `resolve_peer` rejects `User` by isinstance, and
    `resolve_group_target` reads `.id` / `.title` / `.username` off it."""
    return Channel(
        id=id, title=title, photo=None, date=DATE, username=username
    )


def user(id: int, *, first: str | None = "Ann", last: str | None = None,
         username: str | None = None) -> User:
    """A real `User` — the shape `_resolve_event_users` and the admin log's
    own `users` list are read through."""
    return User(id=id, first_name=first, last_name=last, username=username)


class FullChannel:
    """The `GetFullChannelRequest` reply, duck-typed.

    Same reasoning as `AdminLogEvent` below: the domain reads exactly four
    attributes off it (`chats[0].title`, `full_chat.about`,
    `.participants_count`, `.linked_chat_id`) through plain attribute access,
    while a real `messages.ChatFull` demands a few dozen fields nothing looks
    at.
    """

    def __init__(self, *, title: str | None = None, about: str | None = None,
                 participants: int | None = None,
                 linked_chat_id: int | None = None) -> None:
        self.chats = [Named("Chat", title=title)] if title is not None else []
        self.full_chat = Named(
            "ChannelFull", about=about, participants_count=participants,
            linked_chat_id=linked_chat_id,
        )


class PublicForwards:
    """One page of `GetMessagePublicForwardsRequest`. An empty `next_offset`
    ends the walk, which is how `get_public_forwards` knows to stop."""

    def __init__(self, forwards: list, next_offset: str = "") -> None:
        self.forwards = forwards
        self.next_offset = next_offset


def public_forward(msg_id: int, channel_id: int) -> PublicForwardMessage:
    """A forward of one of our posts into another channel. Real, because
    `get_public_forwards` filters on `isinstance(fwd, PublicForwardMessage)`."""
    return PublicForwardMessage(
        message=Message(
            id=msg_id, peer_id=PeerChannel(channel_id), date=DATE, message=""
        )
    )


class AdminLogPage:
    """One `GetAdminLogRequest` reply: events plus the subjects' user objects.
    An empty `events` list ends `_fetch_admin_log_events`' walk."""

    def __init__(self, events: list, users: list | None = None) -> None:
        self.events = events
        self.users = users or []


def entity_key(obj):
    """The id a fake lookup is keyed by, whatever wrapper it arrives in.

    The same channel reaches the client as a handle string, a `Channel`, or a
    `PeerChannel` depending on the call site, so lookups normalise first.
    """
    for attr in ("channel_id", "user_id", "chat_id", "id"):
        value = getattr(obj, attr, None)
        if value is not None:
            return value
    return obj


class FakeClient:
    """The Telegram network boundary, hand-faked.

    This is the whole seam `ingest_with_client` / `scan_group_with_client`
    exist for: they take a client rather than opening a session, so a test
    supplies this and no `.tg-analytic/session.session` is ever involved.

    Only what those paths actually await is here:

    * `messages` is the timeline — what `iter_messages` walks (honouring
      `reverse` / `limit` / `offset_id`, because `scrape_posts` computes all
      three and a fake that ignored them would make that translation
      untestable) and what `get_messages(ids=...)` looks ids up in. Ids the
      timeline doesn't hold come back as `None`, which is how the real API
      reports a deleted or out-of-range message.
    * `comments` maps a post id to its thread, for `iter_messages(reply_to=)`.
    * `entities` answers `get_entity`; every value is also registered under
      its own `.id`, so one entry serves a handle *and* a `PeerChannel`. A
      miss raises `ValueError`, which is what `resolve_peer` catches.
    * `full` / `forwards` / `admin_log` answer the three raw TL requests.
      `admin_log_error` makes the log raise instead, which is what a non-admin
      account gets.

    Every call is recorded so a test can assert a round-trip did *not* happen.
    """

    def __init__(
        self,
        messages: list | None = None,
        *,
        comments: dict[int, list] | None = None,
        entities: dict | None = None,
        full: dict | None = None,
        forwards: dict[int, list] | None = None,
        admin_log: list | None = None,
        admin_log_error: Exception | None = None,
    ) -> None:
        self.messages = list(messages or [])
        self.comments = comments or {}
        self.entities: dict = {}
        for handle, entity in (entities or {}).items():
            self.entities[handle] = entity
            own_id = getattr(entity, "id", None)
            if own_id is not None:
                self.entities.setdefault(own_id, entity)
        self.full = full or {}
        self.forwards = forwards or {}
        self._admin_log = list(admin_log or [])
        self.admin_log_error = admin_log_error

        self.calls: list[list[int]] = []          # get_messages id lists
        self.iter_calls: list[dict] = []          # iter_messages kwargs
        self.entity_calls: list = []
        self.downloads: list[int] = []
        self.requests: list = []                  # raw TL requests

    # -- messages -----------------------------------------------------------

    async def get_messages(self, entity, ids=None):
        self.calls.append(list(ids))
        by_id = {m.id: m for m in self.messages}
        return [by_id.get(i) for i in ids]

    async def iter_messages(
        self, entity, limit=None, reverse=False, offset_id=0,
        offset_date=None, reply_to=None,
    ):
        self.iter_calls.append(
            {
                "entity": entity, "limit": limit, "reverse": reverse,
                "offset_id": offset_id, "offset_date": offset_date,
                "reply_to": reply_to,
            }
        )
        if reply_to is not None:
            window = list(self.comments.get(reply_to, []))
        elif reverse:
            # Oldest-first, strictly above offset_id — Telegram's offset is
            # exclusive, which is why the callers pass `offset_id - 1`.
            window = sorted(self.messages, key=lambda m: m.id)
            window = [m for m in window if m.id > offset_id]
            if offset_date is not None:
                window = [m for m in window if m.date >= offset_date]
        else:
            window = sorted(self.messages, key=lambda m: m.id, reverse=True)
            window = [m for m in window if offset_id == 0 or m.id < offset_id]
            if offset_date is not None:
                window = [m for m in window if m.date <= offset_date]
        for m in window[:limit] if limit is not None else window:
            yield m

    # -- entities and media -------------------------------------------------

    async def get_entity(self, peer):
        self.entity_calls.append(peer)
        key = entity_key(peer)
        if key not in self.entities:
            raise ValueError(f"no entity for {key!r}")
        return self.entities[key]

    async def download_media(self, msg, file=None):
        """Writes the file for real: `download_photo` skips the download when
        the destination already exists, and that branch needs a file on disk."""
        self.downloads.append(msg.id)
        with open(file, "wb") as fh:
            fh.write(b"\xff\xd8\xff")
        return file

    # -- raw TL requests ----------------------------------------------------

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name == "GetFullChannelRequest":
            key = entity_key(request.channel)
            if key not in self.full:
                raise ValueError(f"no full-channel entry for {key!r}")
            return self.full[key]
        if name == "GetMessagePublicForwardsRequest":
            return PublicForwards(self.forwards.get(request.msg_id, []))
        if name == "GetAdminLogRequest":
            if self.admin_log_error is not None:
                raise self.admin_log_error
            return self._admin_log.pop(0) if self._admin_log else AdminLogPage([])
        raise AssertionError(f"unexpected request {name}")


def fake_session(client: FakeClient, entity=None, *, error: Exception | None = None):
    """A stand-in for `tg.channel_session`, for the one thing the split leaves
    untestable: that `scrape.ingest` resolves the handle *before* its body
    opens the DB. `error` raises where `resolve_peer` would."""

    @asynccontextmanager
    async def session(session_file, channel=None):
        if error is not None:
            raise error
        yield client, entity

    return session
