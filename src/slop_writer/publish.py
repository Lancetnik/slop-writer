"""The write surface: queue, move, and rewrite a channel's scheduled posts.

The one module in this package that can post to Telegram, so that "this code
can publish" stays auditable at the file level (docs/adr/0003 — the rule used
to be satisfied by a separate script; the script is now a shell over this).
Nothing here is imported by a read path.

Pipeline: Markdown --(slop_writer.markdown)--> text + Telethon MessageEntity
list --> client.send_message(schedule), or client.send_file(schedule) when
photos are attached (the body becomes the caption; several photos form one
album). Scheduling is an MTProto/user-client feature, so this rides the same
Telethon session as the read paths — the Bot API cannot schedule.

Input validation is split from the network work on purpose: `prepare_schedule`
takes everything the caller supplied and fails before a session is required,
which keeps a bad `at` or a missing photo cheap to report and stops the two
callers (CLI, server) from having to reproduce the same order of checks.

Messages here name **arguments, never CLI flags** — the package-wide rule, now
stated in `errors.py`. This module is where it was found broken (#18) and it
owns the one seam for the case the rule cannot cover: the body's origin
genuinely differs per caller, so the caller supplies it (`body_source`) rather
than the raise site guessing. The CLI says "stdin" or `--file draft.md`, the
server says "the `body` argument", and both are right for their reader.
"""

import logging
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telethon.errors import MediaCaptionTooLongError, MessageTooLongError
from telethon.tl.functions.messages import (
    EditMessageRequest,
    SendMediaRequest,
    SendMultiMediaRequest,
)

from .errors import SlopWriterError, UsageError
from .markdown import render as render_markdown
from .scheduled import get_scheduled_message
from .tg import channel_session

log = logging.getLogger(__name__)

# Hardcoded on purpose, with no flag or env override: the guard exists to stop
# the *agent* driving this from scheduling a post too soon. A configurable
# floor the agent could pass would be the agent holding its own leash. The
# human owner can still edit this constant. See docs/adr/0003.
MIN_LEAD = timedelta(hours=1)

# A Telegram album (grouped media) holds at most 10 items.
MAX_ALBUM = 10
# Extensions Telegram accepts as *photos*. Anything else would silently go out
# as a document attachment, so it's rejected instead.
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass
class ScheduleDraft:
    """A validated post, ready to send. Produced by `prepare_schedule`."""
    text: str
    entities: list
    when: datetime
    photos: list[Path] = field(default_factory=list)
    caption_above: bool = False


@dataclass
class PublishResult:
    """Shapes `render.summarize_schedule` consumes. `action` heads the block:
    "Scheduled" (new), "Rescheduled" (time changed), "Edited" (body changed)."""
    channel: str
    item: dict
    action: str


def parse_schedule_time(at: str) -> datetime:
    """ISO-8601-with-offset -> aware datetime, enforcing the lead-time floor.

    Naive (offset-less) values are rejected: for a *published* post, guessing
    the timezone could place it an hour off and silently defeat the floor."""
    normalized = at.strip()
    if normalized.endswith(("Z", "z")):  # 3.10's fromisoformat rejects 'Z'
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        raise UsageError(
            f"Invalid schedule time {at!r}: use ISO-8601 with an offset, "
            "e.g. 2026-06-27T18:00:00+03:00.",
            code="INVALID_SCHEDULE_TIME",
        ) from None
    if dt.tzinfo is None:
        raise UsageError(
            f"Schedule time {at!r} has no UTC offset. Naive times are "
            "ambiguous for a published post — include one, e.g. "
            "2026-06-27T18:00:00+03:00.",
            code="INVALID_SCHEDULE_TIME",
        )
    earliest = datetime.now(UTC) + MIN_LEAD
    if dt < earliest:
        local_earliest = earliest.astimezone(dt.tzinfo).isoformat(timespec="minutes")
        raise SlopWriterError(
            f"Schedule time {dt.isoformat()} is too soon: posts must be "
            f"scheduled at least 1 hour ahead (earliest {local_earliest}).",
            code="INVALID_SCHEDULE_TIME",
        )
    return dt


def check_photos(photos: list[str]) -> list[Path]:
    """Validate photo paths before touching Telegram: files exist, look like
    photos, and fit a single album."""
    if len(photos) > MAX_ALBUM:
        raise UsageError(
            f"Got {len(photos)} photos; a Telegram album holds at most "
            f"{MAX_ALBUM}.",
            code="INVALID_ARGUMENT",
        )
    paths = []
    for raw in photos:
        path = Path(raw)
        if not path.is_file():
            raise UsageError(
                f"Photo {raw!r}: file not found.", code="INVALID_ARGUMENT"
            )
        if path.suffix.lower() not in PHOTO_EXTS:
            raise UsageError(
                f"Photo {raw!r}: extension {path.suffix!r} is not a Telegram "
                f"photo type ({', '.join(sorted(PHOTO_EXTS))}). Other files "
                "would go out as document attachments — convert the image "
                "first.",
                code="INVALID_ARGUMENT",
            )
        paths.append(path)
    return paths


def render_body(
    body: str, *, allow_empty: bool = False, source: str = "the body"
) -> tuple[str, list]:
    """Markdown -> (plain text, Telethon entities).

    `allow_empty` (photo posts): a missing/empty body is a caption-less post,
    not an error. `source` names where the body came from, because only the
    caller knows — a CLI says "stdin" or "--file 'draft.md'"."""
    text, entities = render_markdown(body)
    if not text.strip():
        if allow_empty:
            return "", []
        raise UsageError(
            f"{source} renders to an empty post.", code="INVALID_ARGUMENT"
        )
    return text, entities


def prepare_schedule(
    body: str,
    at: str,
    *,
    photo_paths: list[str] | None = None,
    caption_above: bool = False,
    body_source: str = "the body",
) -> ScheduleDraft:
    """Validate everything a new scheduled post needs, before any network call.

    Check order is load-bearing for the message a caller gets back: the time
    first (cheapest and most often wrong), then the attachments, then the body
    — so a request with three problems reports the one nearest the front."""
    when = parse_schedule_time(at)
    photos = check_photos(photo_paths) if photo_paths else []
    if caption_above and not photos:
        raise UsageError(
            "Placing the caption above needs at least one photo attached.",
            code="INVALID_ARGUMENT",
        )
    text, entities = render_body(
        body, allow_empty=bool(photos), source=body_source
    )
    if caption_above and not text.strip():
        raise UsageError(
            "Placing the caption above needs a non-empty body to place.",
            code="INVALID_ARGUMENT",
        )
    return ScheduleDraft(text, entities, when, photos, caption_above)


def _utf16_len(text: str) -> int:
    """Length in UTF-16 code units — the unit Telegram's limits count in."""
    return len(text.encode("utf-16-le")) // 2


def _too_long(text: str, *, media: bool) -> SlopWriterError:
    """Readable report when Telegram rejects the body for length.

    Length is deliberately NOT checked client-side: the cap depends on the
    account (photo captions: 1024 UTF-16 units, 2048 with Premium; text
    posts: 4096) and Telegram is the authority — a hardcoded check would
    wrongly block Premium accounts. Rejection leaves nothing queued/changed.

    That division is also why this is the only place `MESSAGE_TOO_LONG` can be
    named: the verdict arrives over the network, so the code attaches where
    Telethon's error is translated rather than at a length check that would
    have to guess the cap it is enforcing."""
    n = _utf16_len(text)
    if media:
        cap = "photo-caption cap (1024 UTF-16 units, 2048 with Telegram Premium)"
    else:
        cap = "text-post cap (4096 UTF-16 units)"
    return SlopWriterError(
        f"Telegram rejected the body: {n} UTF-16 units is over this account's "
        f"{cap}. Shorten the body — nothing was queued or changed.",
        hint="Shorten the body and retry. A photo caption is capped far below "
        "a text post, so sending the same body without photos also raises the "
        "ceiling.",
        code="MESSAGE_TOO_LONG",
    )


@contextmanager
def _invert_media_patch(client, invert: bool):
    """Make the friendly Telethon calls carry `invert_media` (caption above).

    Telethon v1's send_file/edit_message don't expose the flag and never will
    (feature-frozen, LonamiWebs/Telethon#4410 — the maintainer's suggested
    route is exactly this monkey patch). Wrap the client's request dispatch
    and stamp the flag onto the raw send/edit requests the friendly API
    builds; everything else (uploads, album grouping) passes through
    untouched."""
    original = client._call

    async def patched(sender, request, ordered=False, flood_sleep_threshold=None):
        if isinstance(
            request, (SendMediaRequest, SendMultiMediaRequest, EditMessageRequest)
        ):
            request.invert_media = invert
        return await original(
            sender, request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold
        )

    client._call = patched
    try:
        yield
    finally:
        del client._call  # drop the shadow, the class method takes over again


async def schedule_post(
    channel: str, draft: ScheduleDraft, session_file: str
) -> PublishResult:
    async with channel_session(session_file, channel) as (client, entity):
        log.info(
            "authenticated, scheduling post to %s for %s",
            channel, draft.when.isoformat(),
        )
        try:
            if draft.photos:
                # parse_mode=None matters: with an *empty* entity list send_file
                # falls back to Telethon's own Markdown parser, which would
                # mangle literal */_/` in the caption. None disables that
                # fallback; non-empty entities are used either way. Several
                # photos become one album, with the caption on its first item.
                patch = (
                    _invert_media_patch(client, True)
                    if draft.caption_above
                    else nullcontext()
                )
                with patch:
                    sent = await client.send_file(
                        entity,
                        (
                            [str(p) for p in draft.photos]
                            if len(draft.photos) > 1
                            else str(draft.photos[0])
                        ),
                        caption=draft.text,
                        formatting_entities=draft.entities,
                        parse_mode=None,
                        schedule=draft.when,
                    )
                msg = sent[0] if isinstance(sent, list) else sent
            else:
                msg = await client.send_message(
                    entity,
                    draft.text,
                    formatting_entities=draft.entities,
                    schedule=draft.when,
                )
        except MediaCaptionTooLongError:
            raise _too_long(draft.text, media=True) from None
        except MessageTooLongError:
            raise _too_long(draft.text, media=False) from None
    return PublishResult(
        channel,
        {
            "id": msg.id,
            "date": msg.date.astimezone(UTC).isoformat() if msg.date else None,
            "requested": draft.when.isoformat(),
            "text": draft.text,
            "entities": len(draft.entities),
            "photos": len(draft.photos),
            "caption_above": draft.caption_above,
        },
        "Scheduled",
    )


async def reschedule_post(
    channel: str, msg_id: int, when: datetime, session_file: str
) -> PublishResult:
    async with channel_session(session_file, channel) as (client, entity):
        existing = await get_scheduled_message(client, entity, msg_id)
        log.info("rescheduling post #%d in %s to %s", msg_id, channel, when.isoformat())
        # text=None -> Telegram keeps the body and entities, only moves the time.
        # edit_message returns None for scheduled edits (Telethon can't map the
        # UpdateNewScheduledMessage response to a Message), so build the summary
        # from known inputs: the id is stable and the new time is `when`.
        # The patch re-sends the post's invert_media (caption-above) state: the
        # raw edit would otherwise silently reset it to caption-below.
        with _invert_media_patch(client, bool(existing.invert_media)):
            await client.edit_message(entity, msg_id, schedule=when)
    return PublishResult(
        channel,
        {
            "id": msg_id,
            "date": when.astimezone(UTC).isoformat(),
            "requested": when.isoformat(),
            "text": existing.message or "",
            "entities": None,
        },
        "Rescheduled",
    )


async def edit_post(
    channel: str, msg_id: int, text: str, entities: list, session_file: str
) -> PublishResult:
    async with channel_session(session_file, channel) as (client, entity):
        existing = await get_scheduled_message(client, entity, msg_id)
        # Re-send the existing schedule date: it both keeps the post in the
        # scheduled queue and is the flag that tells Telegram this edit targets
        # the scheduled message (not a published one with the same id).
        when = existing.date
        log.info("editing scheduled post #%d in %s (time unchanged)", msg_id, channel)
        # Like reschedule, edit_message returns None for scheduled edits; the id
        # and time are unchanged, so report from known values. The patch keeps
        # the post's invert_media (caption-above) state across the edit.
        try:
            with _invert_media_patch(client, bool(existing.invert_media)):
                await client.edit_message(
                    entity, msg_id, text, formatting_entities=entities, schedule=when
                )
        except MediaCaptionTooLongError:
            raise _too_long(text, media=True) from None
        except MessageTooLongError:
            raise _too_long(text, media=False) from None
    return PublishResult(
        channel,
        {
            "id": msg_id,
            "date": when.astimezone(UTC).isoformat() if when else None,
            "requested": None,
            "text": text,
            "entities": len(entities),
        },
        "Edited",
    )
