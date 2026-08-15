# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "slop-writer>=0.2,<0.3",
#     "python-dotenv>=1.0",
#     "typer>=0.12,<1",
# ]
# ///
"""Publish-side CLI: queue a future channel post.

The skill's one *write* entrypoint, kept in its own script so "this code can
post" is auditable at the file level — the read/scrape/query scripts never
import `slop_writer.publish`, which is the module that can. See docs/adr/0003.

Argument parsing and output rendering only: the body is read from --file or
stdin here, everything after that is `slop_writer.publish`.
"""
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from slop_writer.db import env_path
from slop_writer.errors import SlopWriterError
from slop_writer.publish import (
    edit_post,
    parse_schedule_time,
    prepare_schedule,
    render_body,
    reschedule_post,
    schedule_post,
)
from slop_writer.render import summarize_schedule
from slop_writer.tg import require_session, session_path

# The CLI layer decides what "the project root" is — the directory the user
# launched from. `login` lives in the sibling read CLI, so the fix-it hint
# points there rather than at this script.
PROJECT_ROOT = Path.cwd()
DEFAULT_SESSION = str(session_path(PROJECT_ROOT))
LOGIN_COMMAND = f"uv run {Path(__file__).resolve().parent / 'tg_scrape.py'} login"

load_dotenv(env_path(PROJECT_ROOT))

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

app = typer.Typer(help="Publish to a Telegram channel: schedule / reschedule / edit posts.")


@app.callback()
def _main() -> None:
    """Keep the subcommand name required even with a single command, so the
    CLI reads `tg_publish.py schedule ...` and stays open to future verbs."""


def _fail(exc: SlopWriterError) -> typer.Exit:
    typer.echo(exc.message + (f"\n{exc.hint}" if exc.hint else ""), err=True)
    return typer.Exit(code=exc.exit_code)


def _read_body(path: str | None, *, optional: bool = False) -> str:
    """Read the Markdown body from a file, or from stdin when `path` is None/`-`.

    stdin keeps the agent from writing a temp file just to strip a draft's
    metainfo: it produces the clean body and pipes it via a quoted heredoc,
    which passes backticks/`$`/quotes verbatim (no shell escaping). The TTY
    guard turns a bare interactive run into a clear message, not a silent hang
    — except when the body is `optional` (a photo post may have no caption).

    Reading the body is the CLI's job and stops here: the library takes the
    text, because a file path is not a thing a tool call can hand it."""
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


def _body_source(path: str | None) -> str:
    """How to name the body's origin in an error — only the CLI knows."""
    return "stdin" if path in (None, "-") else f"--file {path!r}"


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

    Body is Markdown rendered straight to Telegram entities. --photo attaches
    images: the body becomes the caption (may be empty) and several photos
    form one album; --caption-above puts the caption on top of the photos.
    Length caps are enforced by Telegram, not the CLI (captions: 1024, 2048
    with Premium; text posts: 4096) — a rejection is reported readably and
    nothing is queued. The post must be scheduled at least 1 hour ahead;
    scheduled posts are not persisted (their ids differ from published ids
    and carry no engagement)."""
    body = _read_body(file, optional=bool(photo))
    try:
        draft = prepare_schedule(
            body,
            at,
            photo_paths=photo,
            caption_above=caption_above,
            body_source=_body_source(file),
        )
        require_session(session_file, LOGIN_COMMAND)
        result = asyncio.run(schedule_post(channel, draft, session_file))
    except SlopWriterError as exc:
        raise _fail(exc) from None
    summarize_schedule(result.channel, result.item, result.action)


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
    try:
        when: datetime = parse_schedule_time(at)
        require_session(session_file, LOGIN_COMMAND)
        result = asyncio.run(reschedule_post(channel, id, when, session_file))
    except SlopWriterError as exc:
        raise _fail(exc) from None
    summarize_schedule(result.channel, result.item, result.action)


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
    body = _read_body(file)
    try:
        text, entities = render_body(body, source=_body_source(file))
        require_session(session_file, LOGIN_COMMAND)
        result = asyncio.run(edit_post(channel, id, text, entities, session_file))
    except SlopWriterError as exc:
        raise _fail(exc) from None
    summarize_schedule(result.channel, result.item, result.action)


if __name__ == "__main__":
    app()
