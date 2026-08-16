"""The `slop-writer` command: `install`, `init`, `serve`, `uninstall`.

argparse rather than typer, deliberately. #22 removed typer from the package's
dependencies when the domain stopped reporting its own errors, and a
three-flag launcher is not a reason to put it back — every installed copy of
the server would carry a CLI framework so that one subcommand could have
coloured help.

This module is where "the project root" is decided, exactly as the PEP-723
scripts decide it: `slop_writer` itself never reads the current working
directory. #19 settled that the client launches a stdio server with cwd = the
project root, which is what lets the `.mcp.json` entry stay path-free and
committable; `--project` is the override for every other launcher.

It is also the only module that owns a TTY. `init` is an interactive flow —
prompts, a masked hash, Telethon's own SMS/2FA questions — and every `input()`
in this package lives here, so `slop_writer.init` stays a set of functions
testable without a terminal.
"""

import argparse
import asyncio
import logging
import os
import sys
from getpass import getpass
from pathlib import Path

from .db import env_path
from .errors import SlopWriterError
from .init import (
    CREDENTIALS_URL,
    ENV_KEYS,
    ensure_gitignored,
    inspect_project,
    logout_session,
    require_credentials,
    run_login,
    verify_session,
    write_env,
)
from .install import install_project, package_version, uninstall_project
from .render import summarize_install, summarize_uninstall
from .server import assert_text_only, build_server


def _load_env(project_root: Path) -> None:
    """Populate os.environ from the project's .env, if there is one.

    Best-effort on purpose (#19): `serve` always starts, and a missing or
    incomplete .env surfaces per tool as `NO_CREDENTIALS` with a hint the model
    can act on. Failing at startup would leave the client with a dead server
    and the human with a log line nobody reads."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv is a hard dependency
        logging.getLogger(__name__).warning("python-dotenv missing; .env not read")
        return
    load_dotenv(env_path(project_root))


def _project_root(args: argparse.Namespace) -> Path:
    return Path(args.project).resolve() if args.project else Path.cwd()


def _install(args: argparse.Namespace) -> int:
    print(summarize_install(install_project(_project_root(args))))
    return 0


def _uninstall(args: argparse.Namespace) -> int:
    print(summarize_uninstall(uninstall_project(_project_root(args))))
    return 0


_PROMPTS = {
    "TG_API_ID": "TG_API_ID (the numeric app id)",
    "TG_API_HASH": "TG_API_HASH (the app hash)",
    "TG_PHONE": "TG_PHONE (international format, e.g. +15551234567)",
}


def _ask(key: str, current: str | None) -> str:
    """One credential, read from the terminal and never from a model.

    `TG_API_HASH` goes through `getpass`: it is the one value here that is a
    real secret, and a terminal scrollback is shared far more often than
    anyone intends."""
    label = _PROMPTS[key]
    suffix = f" [{current}]" if current else ""
    reader = getpass if key == "TG_API_HASH" else input
    while True:
        value = reader(f"{label}{suffix}: ").strip()
        if value:
            return value
        if current:
            return current
        print("  Required.", file=sys.stderr)


def _init(args: argparse.Namespace) -> int:
    """Collect credentials, then log in — additive, and safe to re-run.

    The order is fixed by what each step needs: report first (a user who
    mistyped `TG_API_ID` last week needs to see it before being asked
    anything), prompt only for what is missing, and log in only when there is
    no working session. `--force` re-prompts everything; `--relogin` drops the
    session to switch accounts."""
    project_root = _project_root(args)
    state = inspect_project(project_root)

    print(f"Project root: {project_root}")
    print(f"Credentials:  {state.env_file}")
    for key in ENV_KEYS:
        value = state.values.get(key)
        if key == "TG_API_HASH" and value:
            value = f"{value[:4]}…" if len(value) > 4 else "set"
        status = value if value and key not in state.missing else "missing"
        print(f"  {key}: {status}")

    values = dict(state.values)
    needed = ENV_KEYS if args.force else state.missing
    if needed:
        print(f"\nCreate an app at {CREDENTIALS_URL} if you have none. These "
              f"are the credentials of a real Telegram account, not a bot —\n"
              f"the account is what the analytics see, so it must be a member "
              f"of any group you scan and an admin of the channel for\n"
              f"subscriber and view stats.\n")
        for key in needed:
            values[key] = _ask(key, state.values.get(key))
        write_env(project_root, values)
        print(f"\nWrote {state.env_file}")

    require_credentials(values)
    # The session file is a live login to a Telegram account, so this is not a
    # question worth asking (#19).
    if ensure_gitignored(project_root):
        print(f"Added `.tg-analytic/` to {project_root / '.gitignore'}")

    # `init` populates the environment for the same reason `serve` does: the
    # caller that decides the project root is the one that knows which .env to
    # read, and `tg.py` only ever looks at os.environ.
    os.environ.update({k: v for k, v in values.items() if v})

    if args.relogin and logout_session(project_root):
        print("Dropped the stored session — logging in again.")

    account = asyncio.run(verify_session(project_root))
    if account:
        # A file that exists is not a session that works: one revoked from
        # Telegram's device list looks identical on disk, and the failure
        # would otherwise resurface inside a tool call.
        print(f"\nAlready logged in as {account}")
    else:
        print("\nLogging in. Telegram will send a code — enter it below, and "
              "your 2FA password if the account has one.\n")
        account = asyncio.run(run_login(project_root, values["TG_PHONE"]))
        print(f"\nLogged in as {account}")
        print(f"Session saved to {state.session_file}")

    print("\nTelegram state is ready. If you have not run `slop-writer "
          "install` in this project yet, run it now and restart your client.")
    return 0


def _serve(args: argparse.Namespace) -> int:
    if not args.mcp:
        print(
            "serve needs a transport: pass --mcp for the stdio MCP server.",
            file=sys.stderr,
        )
        return 2

    project_root = Path(args.project).resolve() if args.project else Path.cwd()
    _load_env(project_root)

    mcp = build_server(project_root)
    # Before a byte of protocol: a tool that leaked structured output would
    # otherwise fail silently, one tool at a time, in the client's UI (#16).
    asyncio.run(assert_text_only(mcp))
    mcp.run()
    return 0


def main(argv: list[str] | None = None) -> int:
    # stderr, explicitly and for everything: on a stdio server stdout is the
    # JSON-RPC transport, so a log line on it corrupts the stream.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    # Editing a *scheduled* message makes Telethon log a benign WARNING ("No
    # random_id in EditMessageRequest ... to map to") and dump the whole
    # Updates object — the edit applies regardless (adr/0003). Muted here as
    # it is in the CLI script; real failures raise, they do not warn.
    logging.getLogger("telethon.client.messageparse").setLevel(logging.ERROR)

    parser = argparse.ArgumentParser(
        prog="slop-writer",
        description="Telegram channel analytics: MCP server and project setup.",
    )
    # The same version the MCP handshake reports and the skill's frontmatter
    # copies — one string covering package and skill (#21). It answers before
    # the required subcommand does, so `slop-writer --version` stands alone:
    # argparse's version action exits during parsing, and a user asking which
    # release is installed has no verb in mind.
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {package_version()}",
        help="Print the installed version and exit.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_project_flag(p: argparse.ArgumentParser, what: str) -> None:
        p.add_argument("--project", metavar="PATH", help=f"Project root {what}.")

    install = sub.add_parser(
        "install",
        help="Wire this project's Claude Code config to the MCP server.",
        description="Writes the .mcp.json entry, the skill, and — on first "
        "install only — the permission block and the CLAUDE.md address block. "
        "Knows nothing about Telegram; run `init` for that, in either order.",
    )
    _add_project_flag(install, "to wire (default: the current directory)")
    install.set_defaults(func=_install)

    uninstall = sub.add_parser(
        "uninstall",
        help="Remove what `install` wrote. Never touches .tg-analytic/.",
    )
    _add_project_flag(uninstall, "to unwire (default: the current directory)")
    uninstall.set_defaults(func=_uninstall)

    init = sub.add_parser(
        "init",
        help="Collect Telegram credentials and log in. Run this in your own "
        "terminal — Telethon prompts for an SMS code.",
        description="Additive and safe to re-run: prompts only for what is "
        "missing, and logs in only when there is no working session.",
    )
    _add_project_flag(init, "holding .tg-analytic/ (default: the current directory)")
    init.add_argument(
        "--force",
        action="store_true",
        help="Re-prompt for every credential, not just the missing ones.",
    )
    init.add_argument(
        "--relogin",
        action="store_true",
        help="Drop the stored session and log in again (to switch accounts).",
    )
    init.set_defaults(func=_init)

    serve = sub.add_parser(
        "serve", help="Run the MCP server (launched by the MCP client, not by hand)."
    )
    # Named rather than implied: `serve` will grow other transports, and a
    # default that silently changes meaning is worse than one extra flag.
    serve.add_argument(
        "--mcp", action="store_true", help="Serve MCP over stdio."
    )
    serve.add_argument(
        "--project",
        metavar="PATH",
        help="Project root holding .tg-analytic/ (default: the current "
        "directory, which is where the MCP client launches this).",
    )
    serve.set_defaults(func=_serve)

    args = parser.parse_args(argv)
    # The domain raises, the entrypoint reports — the same split the PEP-723
    # scripts use, and the reason `slop_writer` has no `print` of its own.
    # KeyboardInterrupt is caught alongside it because `init` sits blocking on
    # an SMS-code prompt for most of its life, and a Ctrl-C there is a normal
    # way to leave, not a traceback.
    try:
        return args.func(args)
    except SlopWriterError as e:
        print(e.message, file=sys.stderr)
        if e.hint:
            print(e.hint, file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
