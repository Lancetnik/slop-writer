"""Failures the caller is expected to report, not tracebacks to debug.

This module exists because the domain used to say `typer.echo(...)` +
`raise typer.Exit(1)` — a CLI reporting its own errors. A library with a
second caller (the MCP server) cannot do that: the server has no stderr the
model reads and no exit code to return. So the domain raises, and each
entrypoint decides how to report — the CLIs print to stderr and exit, the
server will build its error payload.

Stdlib-only, like `db`: importing this must not drag Telethon in.

`code` is the machine-readable half, from the closed vocabulary decided in
Lancetnik/slop-writer#15 (`ERROR_CODES`). It is optional, and several raise
sites here carry `code=None` on purpose — the vocabulary was drawn up against
the tool surface and does not yet name every failure the domain can produce
(rejected photo arguments, an unknown scheduled-message id, a channel with no
linked discussion group, a body Telegram refuses for length). Inventing codes
for them here would freeze that half of the contract in the wrong ticket;
#17 closes the gap when it builds the payload the codes appear in.
"""

# The closed vocabulary from #15. Listed for reference and to keep raise sites
# honest — nothing validates against it yet, since the server that renders
# `code` doesn't exist.
ERROR_CODES = (
    "NO_CREDENTIALS",
    "NO_SESSION",
    "CANNOT_RESOLVE",
    "NOT_A_CHANNEL",
    "NOT_A_MEMBER",
    "NOT_ADMIN",
    "NO_POST_RIGHTS",
    "NO_DATA",
    "FLOOD_WAIT",
    "QUERY_REJECTED",
    "INVALID_SCHEDULE_TIME",
)


class SlopWriterError(Exception):
    """An operation that failed for a reason worth telling the caller about.

    `message` is the what, `hint` the what-to-do-about-it — kept apart because
    the two go to different places: a CLI prints both to stderr, while the tool
    contract puts them in separate JSON fields.
    """

    #: What a CLI should exit with. 1 = the operation failed.
    exit_code = 1

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code


class UsageError(SlopWriterError):
    """Bad input — the caller can fix the arguments and retry.

    Separate from the base class only to preserve the CLIs' exit 2 for
    argument-shaped failures, which is what a shell script keys off.
    """

    exit_code = 2
