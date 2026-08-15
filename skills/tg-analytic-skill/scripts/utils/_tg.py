"""Telethon session + credential plumbing shared by the write/read scripts.

Lives apart from `_common.py` on purpose: `_common` is stdlib-only so
`tg_query.py` keeps its empty-dependencies property, whereas everything here
imports Telethon. Both `tg_scrape.py` (reads) and `tg_publish.py` (the one
write path) import these helpers so the connect/auth dance has a single home.
"""
import os
from contextlib import asynccontextmanager
from pathlib import Path

import typer
from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)

from ._common import DATA_DIR

DEFAULT_SESSION_FILE = DATA_DIR / "session.session"
DEFAULT_SESSION = str(DEFAULT_SESSION_FILE)

# `login` lives in tg_scrape.py; point users there regardless of which script
# tripped the missing-session check. One level up from this utils package.
_LOGIN_SCRIPT = Path(__file__).resolve().parent.parent / "tg_scrape.py"

# Setup is a separate, user-run skill: an agent that hits one of the errors
# below must hand the work back to the human rather than try to log in itself
# (the SMS prompt needs a TTY). Every message names the skill first and the
# bare-terminal equivalent second, so the CLI stays usable on its own.
_SETUP_SKILL = "setup-tg-analytic"


def _credentials() -> tuple[int, str, str]:
    """Read TG_API_ID / TG_API_HASH / TG_PHONE lazily, at connect time.

    Reading them at import time would crash even `--help` with a bare KeyError
    when .tg-analytic/.env doesn't exist yet; deferring turns that into a
    clear, actionable message on the first command that actually connects."""
    try:
        return (
            int(os.environ["TG_API_ID"]),
            os.environ["TG_API_HASH"],
            os.environ["TG_PHONE"],
        )
    except KeyError as e:
        typer.echo(
            f"Missing {e.args[0]} - Telegram credentials are not set up yet.\n"
            f"Setup is a human step: ask the user to run the `/{_SETUP_SKILL}` "
            "skill (once per project), then stop.\n"
            "Doing it yourself: create an app at https://my.telegram.org/apps "
            f"and put TG_API_ID/TG_API_HASH/TG_PHONE in {DATA_DIR / '.env'}.",
            err=True,
        )
        raise typer.Exit(code=1) from None


def make_client(session_file: str) -> TelegramClient:
    api_id, api_hash, _ = _credentials()
    return TelegramClient(str(session_file), api_id, api_hash)


def _require_session(session_file: str) -> None:
    """Fail fast if no Telethon session exists.

    Auth needs an interactive TTY for the SMS code prompt, so it cannot run
    inside a Bash subprocess. Surface that with a clear message instead of
    deadlocking on input()."""
    if not Path(session_file).exists():
        # Print the real path — the skill installs under varying roots
        # (.claude/skills/, .agents/skills/, the source repo), so a hardcoded
        # relative path would point nowhere for most users.
        typer.echo(
            f"Telegram session not found at {session_file}\n"
            f"Setup is a human step: ask the user to run the `/{_SETUP_SKILL}` "
            "skill, then stop — `login` prompts for an SMS code on a TTY and "
            "hangs when launched from a tool call.\n"
            f"Doing it yourself, at a real terminal: `uv run {_LOGIN_SCRIPT} "
            "login` from the project root.",
            err=True,
        )
        raise typer.Exit(code=1)


@asynccontextmanager
async def channel_session(session_file: str, channel: str | None = None):
    """Connected Telegram client with an owned lifecycle.

    Yields `(client, entity)` — entity resolved when `channel` is given, None
    otherwise (login). One home for the connect / resolve / disconnect dance
    every command previously copied; the client never crosses this seam
    unmanaged."""
    client = make_client(session_file)
    await client.start(phone=_credentials()[2])
    try:
        entity = await _resolve_peer(client, channel) if channel else None
        yield client, entity
    finally:
        await client.disconnect()


async def _resolve_peer(client: TelegramClient, channel: str):
    """Resolve a handle, or exit 1 with the one cause worth reporting.

    A typo'd handle is the common failure and Telethon surfaces it as a bare
    ValueError traceback; callers open their DB only after this returns, so a
    typo no longer leaves an empty .tg-analytic/<typo>.db behind either."""
    try:
        return await client.get_entity(channel)
    except (
        ValueError,
        UsernameInvalidError,
        UsernameNotOccupiedError,
        ChannelPrivateError,
    ) as e:
        typer.echo(
            f"Cannot resolve {channel}: {e}\n"
            "Check the handle for typos, and that this account can see the "
            "channel (join it, or ask for access).",
            err=True,
        )
        raise typer.Exit(code=1) from None
