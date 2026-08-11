# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "telethon>=1.36,<2",
#     "python-dotenv>=1.0",
#     "typer>=0.12,<1",
#     "mistune>=3.0",
# ]
# ///
"""Publish-side CLI: queue a future channel post.

The skill's one *write* path, kept in its own script so "this code can post"
is auditable at the file level (the read/scrape/query scripts never publish).
See docs/adr/0003.

Pipeline: Markdown --(_md2entities)--> plain text + Telethon MessageEntity list
--> client.send_message(schedule), or client.send_file(schedule) when --photo
attaches images (the body becomes the caption; several photos form one album).
Scheduling is an MTProto/user-client feature, so this rides the same Telethon
session as the scrapers — the Bot API cannot schedule.
"""
import asyncio
import logging
import sys
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from telethon.errors import MediaCaptionTooLongError, MessageTooLongError
from telethon.tl.functions.messages import (
    EditMessageRequest,
    GetScheduledMessagesRequest,
    SendMediaRequest,
    SendMultiMediaRequest,
)
from telethon.tl.types import Message

from utils._common import DATA_DIR
from utils._md2entities import render as render_markdown
from utils._render import summarize_schedule
from utils._tg import DEFAULT_SESSION, _require_session, channel_session

load_dotenv(DATA_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("telethon").setLevel(logging.WARNING)
# Scheduled-message edits (reschedule/edit) trigger a benign Telethon WARNING
# ("No random_id in EditMessageRequest ... to map to") that dumps the whole
# Updates object — the edit still applies. Mute that one logger; real failures
# raise exceptions, not warnings.
logging.getLogger("telethon.client.messageparse").setLevel(logging.ERROR)
log = logging.getLogger(__name__)

UTC = timezone.utc

# Hardcoded on purpose, with no CLI flag or env override: the guard exists to
# stop the *agent* driving this CLI from scheduling a post too soon. A
# configurable floor the agent could pass would be the agent holding its own
# leash. The human owner can still edit this constant. See docs/adr/0003.
MIN_LEAD = timedelta(hours=1)

# A Telegram album (grouped media) holds at most 10 items.
MAX_ALBUM = 10
# Extensions Telegram accepts as *photos*. Anything else would silently go out
# as a document attachment, so it's rejected instead.
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

app = typer.Typer(help="Publish to a Telegram channel: schedule / reschedule / edit posts.")


@app.callback()
def _main() -> None:
    """Keep the subcommand name required even with a single command, so the
    CLI reads `tg_publish.py schedule ...` and stays open to future verbs."""


def _read_body(path: str | None, *, optional: bool = False) -> str:
    """Read the Markdown body from a file, or from stdin when `path` is None/`-`.

    stdin keeps the agent from writing a temp file just to strip a draft's
    metainfo: it produces the clean body and pipes it via a quoted heredoc,
    which passes backticks/`$`/quotes verbatim (no shell escaping). The TTY
    guard turns a bare interactive run into a clear message, not a silent hang
    — except when the body is `optional` (a photo post may have no caption)."""
    if path in (None, "-"):
        if sys.stdin.isatty():
            if optional:
                return ""
            typer.echo(
                "No --file given and stdin is a terminal. Pass --file PATH, or "
                "pipe the body, e.g. `... --file - <<'EOF'`.",
                err=True,
            )
            raise typer.Exit(code=2)
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        typer.echo(f"Cannot read --file {path!r}: {exc}", err=True)
        raise typer.Exit(code=2) from None


def _render_markdown(path: str | None, *, allow_empty: bool = False) -> tuple[str, list]:
    """Markdown from --file or stdin -> (plain text, Telethon entities).

    `allow_empty` (photo posts): a missing/empty body is a caption-less post,
    not an error."""
    text, entities = render_markdown(_read_body(path, optional=allow_empty))
    if not text.strip():
        if allow_empty:
            return "", []
        src = "stdin" if path in (None, "-") else f"--file {path!r}"
        typer.echo(f"{src} renders to an empty post.", err=True)
        raise typer.Exit(code=2)
    return text, entities


def _utf16_len(text: str) -> int:
    """Length in UTF-16 code units — the unit Telegram's limits count in."""
    return len(text.encode("utf-16-le")) // 2


def _check_photos(photos: list[str]) -> list[Path]:
    """Validate --photo paths before touching Telegram: files exist, look like
    photos, and fit a single album."""
    if len(photos) > MAX_ALBUM:
        typer.echo(
            f"Got {len(photos)} photos; a Telegram album holds at most "
            f"{MAX_ALBUM}.",
            err=True,
        )
        raise typer.Exit(code=2)
    paths = []
    for raw in photos:
        path = Path(raw)
        if not path.is_file():
            typer.echo(f"--photo {raw!r}: file not found.", err=True)
            raise typer.Exit(code=2)
        if path.suffix.lower() not in PHOTO_EXTS:
            typer.echo(
                f"--photo {raw!r}: extension {path.suffix!r} is not a Telegram "
                f"photo type ({', '.join(sorted(PHOTO_EXTS))}). Other files "
                "would go out as document attachments — convert the image "
                "first.",
                err=True,
            )
            raise typer.Exit(code=2)
        paths.append(path)
    return paths


def _reject_too_long(text: str, *, media: bool) -> None:
    """Readable report when Telegram rejects the body for length.

    Length is deliberately NOT checked client-side: the cap depends on the
    account (photo captions: 1024 UTF-16 units, 2048 with Premium; text
    posts: 4096) and Telegram is the authority — a hardcoded check would
    wrongly block Premium accounts. Rejection leaves nothing queued/changed."""
    n = _utf16_len(text)
    if media:
        cap = "photo-caption cap (1024 UTF-16 units, 2048 with Telegram Premium)"
    else:
        cap = "text-post cap (4096 UTF-16 units)"
    typer.echo(
        f"Telegram rejected the body: {n} UTF-16 units is over this account's "
        f"{cap}. Shorten the body — nothing was queued or changed.",
        err=True,
    )
    raise typer.Exit(code=1)


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


def _parse_when(at: str) -> datetime:
    """ISO-8601-with-offset -> aware datetime, enforcing the lead-time floor.

    Naive (offset-less) values are rejected: for a *published* post, guessing
    the timezone could place it an hour off and silently defeat the floor."""
    normalized = at.strip()
    if normalized.endswith(("Z", "z")):  # 3.10's fromisoformat rejects 'Z'
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        typer.echo(
            f"Invalid --at {at!r}: use ISO-8601 with an offset, e.g. "
            "2026-06-27T18:00:00+03:00.",
            err=True,
        )
        raise typer.Exit(code=2) from None
    if dt.tzinfo is None:
        typer.echo(
            f"--at {at!r} has no UTC offset. Naive times are ambiguous for a "
            "published post — include one, e.g. 2026-06-27T18:00:00+03:00.",
            err=True,
        )
        raise typer.Exit(code=2)
    earliest = datetime.now(UTC) + MIN_LEAD
    if dt < earliest:
        local_earliest = earliest.astimezone(dt.tzinfo).isoformat(timespec="minutes")
        typer.echo(
            f"--at {dt.isoformat()} is too soon: posts must be scheduled at "
            f"least 1 hour ahead (earliest {local_earliest}).",
            err=True,
        )
        raise typer.Exit(code=1)
    return dt


async def schedule_post(
    channel: str,
    text: str,
    entities: list,
    when: datetime,
    session_file: str,
    photos: list[Path] | None = None,
    caption_above: bool = False,
) -> None:
    async with channel_session(session_file, channel) as (client, entity):
        log.info("authenticated, scheduling post to %s for %s", channel, when.isoformat())
        try:
            if photos:
                # parse_mode=None matters: with an *empty* entity list send_file
                # falls back to Telethon's own Markdown parser, which would
                # mangle literal */_/` in the caption. None disables that
                # fallback; non-empty entities are used either way. Several
                # photos become one album, with the caption on its first item.
                patch = (
                    _invert_media_patch(client, True)
                    if caption_above
                    else nullcontext()
                )
                with patch:
                    sent = await client.send_file(
                        entity,
                        [str(p) for p in photos] if len(photos) > 1 else str(photos[0]),
                        caption=text,
                        formatting_entities=entities,
                        parse_mode=None,
                        schedule=when,
                    )
                msg = sent[0] if isinstance(sent, list) else sent
            else:
                msg = await client.send_message(
                    entity,
                    text,
                    formatting_entities=entities,
                    schedule=when,
                )
        except MediaCaptionTooLongError:
            _reject_too_long(text, media=True)
        except MessageTooLongError:
            _reject_too_long(text, media=False)
    summarize_schedule(
        channel,
        {
            "id": msg.id,
            "date": msg.date.astimezone(UTC).isoformat() if msg.date else None,
            "requested": when.isoformat(),
            "text": text,
            "entities": len(entities),
            "photos": len(photos) if photos else 0,
            "caption_above": caption_above,
        },
    )


async def _get_scheduled(client, entity, msg_id: int) -> Message:
    """Fetch one post from the channel's scheduled queue by its sched-msg id.

    One round-trip via GetScheduledMessages (no full-history scan). Exits 1
    with an actionable message if nothing matches — the id is most likely
    stale (the post published, or was already removed)."""
    result = await client(GetScheduledMessagesRequest(peer=entity, id=[msg_id]))
    found = [
        m
        for m in getattr(result, "messages", [])
        if isinstance(m, Message) and m.id == msg_id
    ]
    if not found:
        typer.echo(
            f"No scheduled post #{msg_id} in the queue. List the queue with "
            "`tg_scrape.py scheduled --channel <chan>`.",
            err=True,
        )
        raise typer.Exit(code=1)
    return found[0]


async def reschedule_post(
    channel: str, msg_id: int, when: datetime, session_file: str
) -> None:
    async with channel_session(session_file, channel) as (client, entity):
        existing = await _get_scheduled(client, entity, msg_id)
        log.info("rescheduling post #%d in %s to %s", msg_id, channel, when.isoformat())
        # text=None -> Telegram keeps the body and entities, only moves the time.
        # edit_message returns None for scheduled edits (Telethon can't map the
        # UpdateNewScheduledMessage response to a Message), so build the summary
        # from known inputs: the id is stable and the new time is `when`.
        # The patch re-sends the post's invert_media (caption-above) state: the
        # raw edit would otherwise silently reset it to caption-below.
        with _invert_media_patch(client, bool(existing.invert_media)):
            await client.edit_message(entity, msg_id, schedule=when)
    summarize_schedule(
        channel,
        {
            "id": msg_id,
            "date": when.astimezone(UTC).isoformat(),
            "requested": when.isoformat(),
            "text": existing.message or "",
            "entities": None,
        },
        action="Rescheduled",
    )


async def edit_post(
    channel: str, msg_id: int, text: str, entities: list, session_file: str
) -> None:
    async with channel_session(session_file, channel) as (client, entity):
        existing = await _get_scheduled(client, entity, msg_id)
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
            _reject_too_long(text, media=True)
        except MessageTooLongError:
            _reject_too_long(text, media=False)
    summarize_schedule(
        channel,
        {
            "id": msg_id,
            "date": when.astimezone(UTC).isoformat() if when else None,
            "requested": None,
            "text": text,
            "entities": len(entities),
        },
        action="Edited",
    )


ChannelOpt = Annotated[
    str,
    typer.Option(help="Telegram channel username, required (you need post rights)."),
]
FileOpt = Annotated[
    str | None,
    typer.Option(
        help="Path to the Markdown file with the post body. Omit (or pass '-') "
        "to read the body from stdin, e.g. `--file - <<'EOF' ... EOF`."
    ),
]
CaptionAboveOpt = Annotated[
    bool,
    typer.Option(
        "--caption-above",
        help="Render the body above the photos instead of below (the UI's "
        "'move caption up'). Only meaningful with --photo.",
    ),
]
PhotoOpt = Annotated[
    list[str] | None,
    typer.Option(
        help="Path to an image to attach (.jpg/.jpeg/.png/.webp). Repeat for "
        "an album, up to 10. The body becomes the caption (may be empty; "
        "Telegram caps it at 1024 chars, 2048 with Premium)."
    ),
]
AtOpt = Annotated[
    str,
    typer.Option(
        help="When to publish, ISO-8601 with a UTC offset "
        "(e.g. 2026-06-27T18:00:00+03:00). Must be at least 1 hour ahead."
    ),
]
IdOpt = Annotated[
    int,
    typer.Option(help="Scheduled-message id, from `tg_scrape.py scheduled`."),
]
SessionOpt = Annotated[str, typer.Option(help="Telethon session file name.")]


@app.command("schedule")
def schedule(
    channel: ChannelOpt,
    at: AtOpt,
    file: FileOpt = None,
    photo: PhotoOpt = None,
    caption_above: CaptionAboveOpt = False,
    session_file: SessionOpt = DEFAULT_SESSION,
) -> None:
    """Queue a Markdown post to publish at a future time.

    Body is Markdown rendered straight to Telegram entities (_md2entities).
    --photo attaches images: the body becomes the caption (may be empty) and
    several photos form one album; --caption-above puts the caption on top of
    the photos. Length caps are enforced by Telegram, not the CLI (captions:
    1024, 2048 with Premium; text posts: 4096) — a rejection is reported
    readably and nothing is queued. The post must be scheduled at least
    1 hour ahead; scheduled posts are not persisted (their ids differ from
    published ids and carry no engagement)."""
    when = _parse_when(at)
    photos = _check_photos(photo) if photo else None
    if caption_above and not photos:
        typer.echo("--caption-above only makes sense with --photo.", err=True)
        raise typer.Exit(code=2)
    text, entities = _render_markdown(file, allow_empty=bool(photos))
    if caption_above and not text.strip():
        typer.echo("--caption-above needs a non-empty body to place above.", err=True)
        raise typer.Exit(code=2)
    _require_session(session_file)
    asyncio.run(
        schedule_post(
            channel, text, entities, when, session_file, photos, caption_above
        )
    )


@app.command("reschedule")
def reschedule(
    channel: ChannelOpt,
    id: IdOpt,
    at: AtOpt,
    session_file: SessionOpt = DEFAULT_SESSION,
) -> None:
    """Move an existing scheduled post to a new time; body unchanged.

    Same 1-hour floor as `schedule` (it sets a new publish time). Identify the
    post by its `sched-msg` id from `tg_scrape.py scheduled`."""
    when = _parse_when(at)
    _require_session(session_file)
    asyncio.run(reschedule_post(channel, id, when, session_file))


@app.command("edit")
def edit(
    channel: ChannelOpt,
    id: IdOpt,
    file: FileOpt = None,
    session_file: SessionOpt = DEFAULT_SESSION,
) -> None:
    """Replace the body of an existing scheduled post; publish time unchanged.

    Reads the new body from --file or stdin and renders it the same way
    `schedule` does. No 1-hour floor check — editing text never moves the
    publish time. Identify the post by its `sched-msg` id from `scheduled`."""
    text, entities = _render_markdown(file)
    _require_session(session_file)
    asyncio.run(edit_post(channel, id, text, entities, session_file))


if __name__ == "__main__":
    app()
