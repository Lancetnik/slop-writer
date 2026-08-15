# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "slop-writer>=0.3,<0.4",
#     "python-dotenv>=1.0",
#     "typer>=0.12,<1",
# ]
# ///
"""Read-side CLI: argument parsing and output rendering, nothing else.

Every command here resolves its arguments, calls one `slop_writer` function,
and prints the result. The scraping itself lives in the package so the MCP
server can call the same functions (Lancetnik/slop-writer#22).
"""
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from slop_writer.db import data_dir, env_path
from slop_writer.errors import SlopWriterError
from slop_writer.group import scan_group
from slop_writer.render import (
    summarize_group,
    summarize_scheduled,
    summarize_scrape,
    summarize_subscribers,
    summarize_views,
)
from slop_writer.scheduled import list_scheduled
from slop_writer.scrape import PROGRESS_EVERY, refresh_posts, scrape_posts
from slop_writer.stats import fetch_subscribers, fetch_views_by_hour
from slop_writer.tg import channel_session, require_session, session_path

# The CLI layer is where "the project root" gets decided: it is the directory
# the user launched from, never the skill's install location. The library takes
# it as an argument and assumes nothing.
PROJECT_ROOT = Path.cwd()
DEFAULT_OUTPUT_DIR = data_dir(PROJECT_ROOT)
DEFAULT_SESSION = str(session_path(PROJECT_ROOT))
LOGIN_COMMAND = f"uv run {Path(__file__).resolve()} login"

load_dotenv(env_path(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
# Telethon's network chatter (Connecting/Disconnecting) is pure noise for an
# LLM consuming the output - keep only warnings/errors from it.
logging.getLogger("telethon").setLevel(logging.WARNING)

app = typer.Typer(help="Scrape posts, forwards and comments from a Telegram channel.")

# Shared option declarations — one home per flag's help text. Commands reuse
# these aliases, so the CLI surface stays identical across commands by
# construction instead of by copy-paste discipline.
ChannelOpt = Annotated[
    str, typer.Option(help="Telegram channel username (required).")
]
AdminChannelOpt = Annotated[
    str,
    typer.Option(help="Telegram channel username, required (you must be an admin)."),
]
PostRightsChannelOpt = Annotated[
    str,
    typer.Option(help="Telegram channel username, required (you need post rights)."),
]
OutputDirOpt = Annotated[
    Path, typer.Option(help="Directory for the SQLite DB and downloaded media.")
]
SessionOpt = Annotated[str, typer.Option(help="Telethon session file name.")]
CommentsOpt = Annotated[bool, typer.Option(help="Fetch post comments.")]
MediaOpt = Annotated[bool, typer.Option(help="Download post media.")]
ChannelInfoOpt = Annotated[
    bool,
    typer.Option(
        help="Resolve detail info about outer public channels that forwarded posts."
    ),
]
VerboseOpt = Annotated[
    bool,
    typer.Option(
        "--verbose", "-v",
        help="Per-post progress + Telethon network logs (otherwise every "
        f"{PROGRESS_EVERY} posts).",
    ),
]


def _prepare(session_file: str, verbose: bool = False) -> None:
    """Shared command preamble: a session must exist; -v raises log verbosity."""
    require_session(session_file, LOGIN_COMMAND)
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("telethon").setLevel(logging.INFO)


def _run(coro):
    """Run a domain coroutine, reporting its failures the way a CLI should.

    `SlopWriterError` is the library saying "tell the caller this" — here that
    means stderr and an exit code. A second caller (the MCP server) catches
    the same exception and builds a JSON payload instead."""
    try:
        return asyncio.run(coro)
    except SlopWriterError as exc:
        typer.echo(exc.message + (f"\n{exc.hint}" if exc.hint else ""), err=True)
        raise typer.Exit(code=exc.exit_code) from None


@app.command("scrape")
def scrape_cmd(
    channel: ChannelOpt,
    output_dir: OutputDirOpt = DEFAULT_OUTPUT_DIR,
    session_file: SessionOpt = DEFAULT_SESSION,
    limit: Annotated[
        int | None,
        typer.Option(
            help="Max messages fetched in the chronological walk. Use with "
            "--offset-id/--offset-date to cap a forward page; for 'N newest' "
            "use --latest instead."
        ),
    ] = None,
    offset_id: Annotated[
        int,
        typer.Option(
            help="Start at this post id (inclusive) and walk forward to newer "
            "posts. 0 = walk from the beginning of history."
        ),
    ] = 0,
    offset_date: Annotated[
        datetime | None,
        typer.Option(
            formats=["%d-%m-%Y", "%d-%m-%Y %H:%M:%S"],
            help="Start after this date and walk forward to newer posts.",
        ),
    ] = None,
    latest: Annotated[
        int | None,
        typer.Option(
            help="Fetch the N most recent posts (newest-first). Overrides "
            "--limit/--offset-id/--offset-date."
        ),
    ] = None,
    comments: CommentsOpt = True,
    media: MediaOpt = True,
    channel_info: ChannelInfoOpt = True,
    verbose: VerboseOpt = False,
) -> None:
    """Run the scraper."""
    _prepare(session_file, verbose)
    result = _run(
        scrape_posts(
            channel,
            output_dir,
            session_file,
            limit,
            offset_id,
            offset_date,
            latest,
            comments,
            media,
            channel_info,
        )
    )
    print(summarize_scrape(result.channel, result.posts, result.channels))


@app.command("fetch")
def fetch_cmd(
    post_ids: Annotated[
        list[int],
        typer.Argument(help="One or more post ids, e.g. `fetch 103 105 108`."),
    ],
    channel: ChannelOpt,
    output_dir: OutputDirOpt = DEFAULT_OUTPUT_DIR,
    session_file: SessionOpt = DEFAULT_SESSION,
    comments: CommentsOpt = True,
    media: MediaOpt = True,
    channel_info: ChannelInfoOpt = True,
    verbose: VerboseOpt = False,
) -> None:
    """Fetch specific posts by id and persist them like `scrape` does.

    Useful for refreshing a known post or pulling a small set without
    iterating the whole channel history. Missing ids are logged and skipped."""
    _prepare(session_file, verbose)
    result = _run(
        refresh_posts(
            channel,
            post_ids,
            output_dir,
            session_file,
            comments,
            media,
            channel_info,
        )
    )
    print(summarize_scrape(result.channel, result.posts, result.channels))


@app.command("subscribers")
def subscribers(
    channel: AdminChannelOpt,
    output_dir: OutputDirOpt = DEFAULT_OUTPUT_DIR,
    session_file: SessionOpt = DEFAULT_SESSION,
) -> None:
    """Export daily subscriber dynamics into the SQLite DB
    (subscribers + subscriber_sources tables)."""
    _prepare(session_file)
    result = _run(fetch_subscribers(channel, output_dir, session_file))
    print(summarize_subscribers(result.channel, result.rows))


@app.command("views")
def views(
    channel: AdminChannelOpt,
    session_file: SessionOpt = DEFAULT_SESSION,
) -> None:
    """Print views per hour of day to the console: hour|views."""
    _prepare(session_file)
    result = _run(fetch_views_by_hour(channel, session_file))
    print(
        summarize_views(
            result.channel,
            result.hours,
            result.views,
            result.period_start,
            result.period_end,
        )
    )


@app.command("scheduled")
def scheduled(
    channel: PostRightsChannelOpt,
    session_file: SessionOpt = DEFAULT_SESSION,
) -> None:
    """List the channel's scheduled (not-yet-published) posts to the console.

    Read-only — scheduled posts have no engagement yet and their ids differ
    from published ids, so nothing is persisted. Requires post rights on the
    channel."""
    _prepare(session_file)
    result = _run(list_scheduled(channel, session_file))
    print(summarize_scheduled(result.channel, result.items))


@app.command("group")
def group_cmd(
    channel: Annotated[
        str | None,
        typer.Option(
            help="Channel username - scan its linked discussion group "
            "(rows land in the CHANNEL's DB, threads join to posts)."
        ),
    ] = None,
    group: Annotated[
        str | None,
        typer.Option(
            help="Group username - scan a standalone group (own DB, no "
            "thread linkage). For a group attached to a channel you "
            "analyze, prefer --channel."
        ),
    ] = None,
    output_dir: OutputDirOpt = DEFAULT_OUTPUT_DIR,
    session_file: SessionOpt = DEFAULT_SESSION,
    limit: Annotated[
        int | None,
        typer.Option(
            help="Max messages fetched in the chronological walk. Use with "
            "--offset-id/--offset-date to cap a forward page; for 'N newest' "
            "use --latest instead."
        ),
    ] = None,
    offset_id: Annotated[
        int,
        typer.Option(
            help="Start at this group-message id (inclusive) and walk "
            "forward. 0 = walk from the beginning of history."
        ),
    ] = 0,
    offset_date: Annotated[
        datetime | None,
        typer.Option(
            formats=["%d-%m-%Y", "%d-%m-%Y %H:%M:%S"],
            help="Start after this date and walk forward to newer messages.",
        ),
    ] = None,
    latest: Annotated[
        int | None,
        typer.Option(
            help="Fetch the N most recent group messages (newest-first). "
            "Overrides --limit/--offset-id/--offset-date."
        ),
    ] = None,
    verbose: VerboseOpt = False,
) -> None:
    """Scan a discussion group: messages, threads, join/leave events.

    Joins/leaves come from the group's service messages (membership
    needed, no admin). Pass exactly one of --channel/--group."""
    if (channel is None) == (group is None):
        typer.echo("Pass exactly one of --channel or --group.", err=True)
        raise typer.Exit(code=2)
    _prepare(session_file, verbose)
    result = _run(
        scan_group(
            channel, group, output_dir, session_file,
            limit, offset_id, offset_date, latest,
        )
    )
    print(
        summarize_group(
            result.label,
            result.overview,
            result.messages,
            result.events,
            result.threads,
        )
    )


@app.command("login")
def login(
    session_file: SessionOpt = DEFAULT_SESSION,
) -> None:
    """One-time interactive Telegram auth.

    Run this **in your own terminal** (not via Claude Code's Bash tool) before
    using scrape/fetch/subscribers/views. Telethon prompts on stdin for the
    SMS code (and the 2FA password if you have one enabled), then writes the
    session file. Subsequent commands reuse it."""
    Path(session_file).parent.mkdir(parents=True, exist_ok=True)

    async def _go() -> None:
        async with channel_session(session_file):
            pass

    _run(_go())
    typer.echo(f"Saved Telegram session to {session_file}")


if __name__ == "__main__":
    app()
