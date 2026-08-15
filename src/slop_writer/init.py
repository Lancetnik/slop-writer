"""`slop-writer init` — the Telegram state, and nothing else.

The counterpart to `install` under Lancetnik/slop-writer#19: this module knows
about credentials and sessions and never about MCP clients. The two commands
run in either order with no cross-checks, which is what keeps `init` from
having to learn every client's config format.

**Additive and idempotent, never destructive.** Whatever is already valid is
kept and reported; only what is missing gets asked for. So the safe reflex —
"just run `init` again" — is also the correct one.

No prompting happens here. `input()` and `getpass` belong to whoever owns the
TTY, which is `cli.py`; this module reads, writes, and verifies. That is also
what makes it testable without a terminal.

The reason `init` is a terminal command at all rather than a pair of MCP tools:
`setup-tg-analytic` was an *agent* skill, so `api_id`/`api_hash` were typed into
chat and landed in the model's context, in transcripts, and in whatever those
get sent to. A CLI keeps them out entirely. #12 confirmed MCP elicitation would
technically work — this is the reason no future ticket should use it.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
)

from .db import DATA_DIR_NAME, env_path
from .errors import SlopWriterError
from .tg import make_client, session_path

#: The three values `.tg-analytic/.env` carries. `TG_PHONE` is kept even though
#: nothing but the login reads it: it is not a secret worth the friction, and
#: it makes the next login one field shorter.
ENV_KEYS = ("TG_API_ID", "TG_API_HASH", "TG_PHONE")

CREDENTIALS_URL = "https://my.telegram.org/apps"

GITIGNORE_LINE = f"{DATA_DIR_NAME}/"


@dataclass
class ProjectState:
    """What `init` found before touching anything."""

    project_root: Path
    env_file: Path
    session_file: Path
    values: dict[str, str]
    missing: tuple[str, ...]
    session_exists: bool
    is_git_repo: bool
    gitignored: bool


def read_env(project_root: Path) -> dict[str, str]:
    """Parse `.tg-analytic/.env` into the keys this project owns.

    A hand-rolled reader rather than dotenv's: `init` must round-trip the file
    it read, and `load_dotenv` populates `os.environ` instead of handing back
    what was on disk — which is the difference between preserving a user's
    unrelated line and silently dropping it."""
    path = env_path(project_root)
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _invalid(values: dict[str, str]) -> tuple[str, ...]:
    """Keys that are absent, blank, or obviously the wrong shape.

    Only `TG_API_ID` gets a shape check, and only because Telethon reports a
    non-numeric one as an opaque `ApiIdInvalidError` at connect time — hours
    later, from a tool call. The other two are opaque strings; guessing at
    their format here would reject valid input for no gain."""
    bad = []
    for key in ENV_KEYS:
        value = values.get(key, "").strip()
        if not value:
            bad.append(key)
        elif key == "TG_API_ID" and not value.isdigit():
            bad.append(key)
    return tuple(bad)


def inspect_project(project_root: Path) -> ProjectState:
    """Report the state before changing anything — `init` prompts from this."""
    values = read_env(project_root)
    gitignore = project_root / ".gitignore"
    ignored = (
        gitignore.is_file()
        and GITIGNORE_LINE
        in {line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()}
    )
    return ProjectState(
        project_root=project_root,
        env_file=env_path(project_root),
        session_file=session_path(project_root),
        values=values,
        missing=_invalid(values),
        session_exists=session_path(project_root).is_file(),
        is_git_repo=(project_root / ".git").exists(),
        gitignored=ignored,
    )


def write_env(project_root: Path, values: dict[str, str]) -> Path:
    """Write `.tg-analytic/.env`, preserving keys this project does not own.

    Rewritten whole rather than appended to: a `.env` with two `TG_API_ID`
    lines is legal, and which one wins depends on the reader — a difference
    that shows up as an inexplicable auth failure much later."""
    path = env_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {**read_env(project_root), **values}
    ordered = [k for k in ENV_KEYS if k in merged]
    ordered += [k for k in merged if k not in ENV_KEYS]
    body = "".join(f"{key}={merged[key]}\n" for key in ordered)
    path.write_text(body, encoding="utf-8")
    return path


def ensure_gitignored(project_root: Path) -> bool:
    """Append `.tg-analytic/` to `.gitignore`. Returns whether it was added.

    Unconditional, with no prompt: `session.session` is a live login to the
    user's Telegram account, and asking permission to keep a credential out of
    version control is theatre. Only in a git repo — elsewhere the file would
    be noise."""
    if not (project_root / ".git").exists():
        return False
    gitignore = project_root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    if GITIGNORE_LINE in {line.strip() for line in existing.splitlines()}:
        return False
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    gitignore.write_text(
        f"{existing}{prefix}{GITIGNORE_LINE}\n", encoding="utf-8"
    )
    return True


@contextmanager
def _translated_auth_errors():
    """Turn Telethon's auth exceptions into something a human can act on.

    These are the failures `init` exists to catch, and every one of them names
    a value the user just typed. Left raw they surface as a traceback ending in
    `ApiIdInvalidError`, which says nothing about *which* of the three fields
    is wrong — the single most common way this setup goes wrong is a hash
    pasted from a different app than the id."""
    try:
        yield
    except ApiIdInvalidError:
        raise SlopWriterError(
            "Telegram rejected TG_API_ID / TG_API_HASH.",
            hint=f"They must be from the same app at {CREDENTIALS_URL}. "
            "Re-enter both with `slop-writer init --force`.",
            code="NO_CREDENTIALS",
        ) from None
    except PhoneNumberInvalidError:
        raise SlopWriterError(
            "Telegram rejected TG_PHONE.",
            hint="Use international format with the country code, e.g. "
            "+15551234567. Fix it with `slop-writer init --force`.",
            code="NO_CREDENTIALS",
        ) from None
    except PhoneNumberBannedError:
        raise SlopWriterError(
            "This phone number is banned from Telegram.",
            hint="Contact Telegram support, or use another account.",
            code="NO_CREDENTIALS",
        ) from None
    except FloodWaitError as e:
        raise SlopWriterError(
            f"Telegram is rate-limiting logins for {e.seconds}s.",
            hint="Repeated login attempts trigger this. Wait it out and run "
            "`slop-writer init` again — the credentials already on disk are "
            "kept.",
            code="FLOOD_WAIT",
        ) from None


def describe_account(me) -> str:
    """How `get_me` is reported back — the whole point of verifying live.

    People have several Telegram accounts, and logging in as the wrong one is
    otherwise discovered days later through inexplicably empty result sets."""
    name = " ".join(
        filter(
            None,
            (getattr(me, "first_name", None), getattr(me, "last_name", None)),
        )
    )
    handle = f"@{me.username}" if getattr(me, "username", None) else None
    phone = f"+{me.phone}" if getattr(me, "phone", None) else None
    parts = [p for p in (name or None, handle, phone) if p]
    return " / ".join(parts) if parts else "unknown account"


async def verify_session(project_root: Path) -> str | None:
    """Return the logged-in account, or None if there is no working session.

    "Working" means a live `get_me`, not a file that exists. A session revoked
    from Telegram's device list leaves `session.session` in place, and under a
    file-exists check that failure resurfaces inside a tool call — where it is
    far more expensive to diagnose than here (#19)."""
    session_file = session_path(project_root)
    if not session_file.is_file():
        return None
    client = make_client(str(session_file))
    try:
        with _translated_auth_errors():
            await client.connect()
            if not await client.is_user_authorized():
                return None
            return describe_account(await client.get_me())
    finally:
        await client.disconnect()


async def run_login(project_root: Path, phone: str) -> str:
    """Perform the interactive login and return the account it produced.

    This is the step that cannot be a tool and cannot run through the Bash
    tool: Telethon prompts on stdin for the SMS code, and for the 2FA password
    when the account has one. Called from a real terminal, it just works;
    called from anywhere else it deadlocks on the prompt — which is why the
    server's `NO_SESSION` hint tells the agent to hand this back to the human
    and stop."""
    session_file = session_path(project_root)
    session_file.parent.mkdir(parents=True, exist_ok=True)
    client = make_client(str(session_file))
    try:
        with _translated_auth_errors():
            await client.start(phone=phone)
            return describe_account(await client.get_me())
    finally:
        await client.disconnect()


def logout_session(project_root: Path) -> bool:
    """Drop the stored session so `--relogin` can authenticate as someone else.

    Removing the file rather than calling `log_out`: the common reason to
    re-login is a *revoked* session, where the server-side call fails and would
    leave the dead file behind — the exact state this is meant to clear."""
    session_file = session_path(project_root)
    if not session_file.is_file():
        return False
    session_file.unlink()
    return True


def require_credentials(values: dict[str, str]) -> None:
    """Guard the transition from prompting to connecting."""
    missing = _invalid(values)
    if missing:
        raise SlopWriterError(
            "Incomplete Telegram credentials: " + ", ".join(missing),
            hint=f"Create an app at {CREDENTIALS_URL} and run "
            f"`slop-writer init` again; the values go in "
            f"{DATA_DIR_NAME}/.env under the project root.",
            code="NO_CREDENTIALS",
        )
