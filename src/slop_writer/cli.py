"""The `slop-writer` command.

Only `serve` today; `install` and `init` arrive with Lancetnik/slop-writer#20.

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
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .db import env_path
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

    parser = argparse.ArgumentParser(
        prog="slop-writer",
        description="Telegram channel analytics: MCP server and project setup.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

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
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
