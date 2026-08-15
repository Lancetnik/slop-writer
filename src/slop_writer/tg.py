"""Telethon session + credential plumbing shared by every Telegram path.

Lives apart from `db.py` on purpose: `db` is stdlib-only so the query path
keeps its Telegram-free property, whereas everything here imports Telethon.

Failures raise `SlopWriterError` rather than reporting themselves: this module
is called by the CLIs and, next, by the MCP server, and only the entrypoint
knows whether an error is a line on stderr or a JSON payload.
"""
import os
from contextlib import asynccontextmanager
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.types import User

from .db import DATA_DIR_NAME, data_dir
from .errors import SlopWriterError

# Setup is a human step at a real terminal: an agent that hits one of the
# errors below must hand it back rather than try to log in itself (the SMS
# prompt needs a TTY). #20 deleted the `setup-tg-analytic` skill these hints
# used to name — its whole job now lives in `slop-writer init`, and this error
# text is what carries that fact to the agent at the moment it fails.
_SETUP_COMMAND = "slop-writer init"


def session_path(project_root: Path) -> Path:
    """The Telethon session file for a project. One login per project root."""
    return data_dir(project_root) / "session.session"


def _credentials() -> tuple[int, str, str]:
    """Read TG_API_ID / TG_API_HASH / TG_PHONE lazily, at connect time.

    Reading them at import time would crash even `--help` with a bare KeyError
    when .tg-analytic/.env doesn't exist yet; deferring turns that into a
    clear, actionable message on the first command that actually connects.

    The message names the env file by the rule (`<project root>/.tg-analytic/
    .env`) rather than as a resolved path: whoever loaded the environment knows
    the project root, this function only knows what it found in `os.environ`."""
    try:
        return (
            int(os.environ["TG_API_ID"]),
            os.environ["TG_API_HASH"],
            os.environ["TG_PHONE"],
        )
    except KeyError as e:
        raise SlopWriterError(
            f"Missing {e.args[0]} - Telegram credentials are not set up yet.",
            hint=(
                f"Setup is a human step: ask the user to run `{_SETUP_COMMAND}` "
                "in their own terminal (once per project), then stop.\n"
                "Doing it yourself: create an app at https://my.telegram.org/apps "
                "and put TG_API_ID/TG_API_HASH/TG_PHONE in "
                f"{DATA_DIR_NAME}/.env under the project root you run from."
            ),
            code="NO_CREDENTIALS",
        ) from None


def make_client(session_file: str) -> TelegramClient:
    api_id, api_hash, _ = _credentials()
    return TelegramClient(str(session_file), api_id, api_hash)


def require_session(session_file: str, login_command: str) -> None:
    """Fail fast if no Telethon session exists.

    Auth needs an interactive TTY for the SMS code prompt, so it cannot run
    inside a Bash subprocess. Surface that with a clear message instead of
    deadlocking on input().

    `login_command` is what the user should type to fix it, and has no default
    on purpose: only the caller knows how it was invoked. This module ships
    inside an installed package, so it cannot point at a script path of its
    own — one derived from `__file__` here would name site-packages."""
    if not Path(session_file).exists():
        # Name the real path — the project root varies per caller, so a
        # hardcoded relative path would point nowhere for most users.
        raise SlopWriterError(
            f"Telegram session not found at {session_file}",
            hint=(
                f"Setup is a human step: ask the user to run `{_SETUP_COMMAND}` "
                "in their own terminal, then stop — it prompts for an SMS code "
                "on a TTY and hangs when launched from a tool call.\n"
                f"Doing it yourself, at a real terminal: `{login_command}` "
                "from the project root."
            ),
            code="NO_SESSION",
        )


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
        entity = await resolve_peer(client, channel) if channel else None
        yield client, entity
    finally:
        await client.disconnect()


async def resolve_peer(client: TelegramClient, channel: str):
    """Resolve a handle, or fail with the one cause worth reporting.

    A typo'd handle is the common failure and Telethon surfaces it as a bare
    ValueError traceback; callers open their DB only after this returns, so a
    typo no longer leaves an empty .tg-analytic/<typo>.db behind either.

    Handles resolving to a person are rejected: every command targets a
    channel or group, and reading (or posting into) someone's private chat
    is out of scope by design.

    Two failures, two codes. `ChannelPrivateError` is Telegram saying the peer
    is real and this account may not see it — a different situation from a
    handle that names nothing, and a different fix: join it, versus correct it.
    They used to share `CANNOT_RESOLVE`, which told an agent holding a perfectly
    good handle to go looking for a typo (#35)."""
    try:
        entity = await client.get_entity(channel)
    except ChannelPrivateError as e:
        raise SlopWriterError(
            f"Cannot access {channel}: {e}",
            hint="The handle is fine — this account is not in that channel or "
            "group (or was removed from it). Join it, or ask its owner for "
            "access, then retry.",
            code="NOT_A_MEMBER",
        ) from None
    except (
        ValueError,
        UsernameInvalidError,
        UsernameNotOccupiedError,
    ) as e:
        raise SlopWriterError(
            f"Cannot resolve {channel}: {e}",
            hint="Check the handle for typos. A private group also resolves "
            "only for an account that has already seen it.",
            code="CANNOT_RESOLVE",
        ) from None
    if isinstance(entity, User):
        raise SlopWriterError(
            f"{channel} is a user, not a channel or group. Private chats are "
            "not supported - pass a channel or group handle.",
            code="NOT_A_CHANNEL",
        )
    return entity
