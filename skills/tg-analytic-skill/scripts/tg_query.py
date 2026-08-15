# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "slop-writer>=0.3,<0.4",
# ]
# ///
"""Query-side CLI: parse arguments, run one read-only query, print a table.

The guards and the SQL live in `slop_writer.query`; this file is argparse and
stdout. Still Telegram-free — `slop_writer.query` and `slop_writer.db` are
stdlib-only, so nothing here drags a client in.
"""
import argparse
import logging
import sys
from pathlib import Path

from slop_writer.db import data_dir
from slop_writer.errors import SlopWriterError
from slop_writer.query import run_query
from slop_writer.render import summarize_query

# The CLI layer decides what "the project root" is: the directory the user
# launched from. `slop_writer.db` itself never reads the cwd.
DEFAULT_OUTPUT_DIR = data_dir(Path.cwd())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a read-only SQL query against .tg-analytic/<channel>.db and print a Markdown table.",
    )
    parser.add_argument("sql", help="SQL SELECT statement to run against the channel DB.")
    parser.add_argument(
        "--channel",
        required=True,
        help="Channel username; picks .tg-analytic/<channel>.db (required).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory containing the per-channel DBs (default: %(default)s).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max rows to print, 0 = unlimited (default: %(default)s).",
    )
    parser.add_argument(
        "--no-truncate",
        action="store_true",
        help="Disable per-cell text truncation (default truncates at 200 chars).",
    )
    args = parser.parse_args()

    try:
        result = run_query(args.sql, args.channel, args.output_dir)
    except SlopWriterError as exc:
        log.error("%s", exc.message)
        # The schema dump rides in `hint` — printed to stderr so it can't be
        # mistaken for part of the table on stdout.
        if exc.hint:
            print(exc.hint, file=sys.stderr)
        return exc.exit_code

    print(
        summarize_query(
            result.columns, result.rows, args.limit, truncate=not args.no_truncate
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
