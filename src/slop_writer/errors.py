"""Failures the caller is expected to report, not tracebacks to debug.

This module exists because the domain used to say `typer.echo(...)` +
`raise typer.Exit(1)` — a CLI reporting its own errors. A library with a
second caller (the MCP server) cannot do that: the server has no stderr the
model reads and no exit code to return. So the domain raises, and each
entrypoint decides how to report — the CLIs print to stderr and exit, the
server will build its error payload.

Stdlib-only, like `db`: importing this must not drag Telethon in.

**A message or a hint names an argument or an operation, never a surface.**
The same string reaches a human reading stderr and a model reading a tool
result, and only one of them has `--at` or a script to run; the server passes
every non-setup hint through verbatim (`test_server.py`), so a remedy phrased
for the wrong reader is a remedy that reader cannot follow. Where the surface
genuinely differs the package has two seams and neither is a raise site: the
caller supplies the differing noun (`publish.prepare_schedule(body_source=…)`
— the CLI says `--file`, the server says the `body` argument), or the boundary
swaps the whole hint (`server._SETUP_HINT`, for `NO_CREDENTIALS`/`NO_SESSION`,
where the remedy really is a different command per caller). Everything else
stays caller-neutral, and `test_errors.py` scans the source for the two forms
that are unambiguously a CLI. The rule used to live in `publish.py`'s
docstring, scoped to the one file #18 found the defect in; #40 found it had
already been broken next door in `scheduled.py`.

`code` is the machine-readable half, from the closed vocabulary decided in
Lancetnik/slop-writer#15 (`ERROR_CODES`) and completed in #17, which found the
original eleven short by six failures. It stays optional in the signature —
an unexpected exception reaching the server boundary has no code of its own —
but every raise site in this package now names one, and `ERROR_CODES` is
enforced at construction rather than merely documented.
"""

# The closed vocabulary. The first eleven are #15's; the rest close the gap
# #22 found while mapping the raise sites, and are named here rather than at
# the boundary so the CLIs report the same code the tools do.
#
# INVALID_ARGUMENT covers all four unnamed input-validation failures (a
# rejected photo, an empty body, `caption_above` without photos, a body/at
# pair the domain refuses) as one code with the offending argument named in
# `message`. Four codes would have been four things to remember and one more
# thing to get wrong; the model's recovery is identical in every case — fix
# the argument and retry — so the code it branches on can be too.
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
    # Added by #17.
    "INVALID_ARGUMENT",
    "NO_LINKED_GROUP",
    "NO_SUCH_MESSAGE",
    "MESSAGE_TOO_LONG",
    # Raised by nothing here: the server boundary's label for an exception the
    # domain did not anticipate, so that even a bug answers in the contract's
    # shape instead of leaking a traceback.
    "INTERNAL",
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
        # A code outside the vocabulary is a bug in this package, not bad user
        # input: the tool contract promises a closed set, and a typo'd code
        # would otherwise reach the model as a token it cannot branch on.
        if code is not None and code not in ERROR_CODES:
            raise ValueError(f"{code!r} is not one of ERROR_CODES")
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
