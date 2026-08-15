"""The closed error vocabulary — the half of the tool contract that is a
promise about *values*, not about shapes.

`{code, message, hint}` is checked in `test_server.py`, where a tool is driven
to its boundary. What cannot be checked there is the vocabulary itself: a code
is only validated when its raise site actually runs, and most raise sites in
this package need a Telegram session to reach. `NOT_ADMIN` fires for a channel
whose stats are refused; `NO_LINKED_GROUP` for a channel without a discussion
group. A live run finds a typo in one of those; CI never would.

So this module checks the vocabulary two ways: at construction, where
`SlopWriterError.__init__` enforces it, and statically over the package's own
source, where every `code=` literal is visible whether or not a test can reach
the line it sits on.

The same argument, and the same `ast` walk, carries the other half of the
promise — the *prose*. `message` and `hint` are read by whichever caller the
domain is serving, and #40 found a hint telling a model to run a script only
the CLI has. That defect is invisible to a construction-time check for exactly
the reason above, so it gets the static treatment too.
"""

import ast
import re
from collections import deque
from pathlib import Path

import pytest

import slop_writer
from slop_writer.errors import ERROR_CODES, SlopWriterError, UsageError

#: Vocabulary entries that no raise site names — see
#: `test_every_code_is_reachable_from_some_raise_site`.
#:
#: Empty as of #35, which closed the two that were here: `MESSAGE_TOO_LONG` now
#: rides `publish._too_long`, and `NOT_A_MEMBER` splits Telegram's "you may not
#: see this peer" out of `CANNOT_RESOLVE`. The set survives its own emptiness on
#: purpose — it is where a *deliberately* unreachable code would be recorded
#: with its reason, so re-adding one is an edit somebody has to justify rather
#: than a green suite quietly widening the contract.
UNREACHED_CODES: set[str] = set()


def _package_sources() -> list[Path]:
    """The installed package's own modules — the editable checkout, which
    `test_packaging.py` is what actually pins."""
    return sorted(
        p for p in Path(slop_writer.__file__).parent.glob("*.py")
        if p.name != "errors.py"
    )


def _codes_named_in_source() -> dict[str, set[str]]:
    """Every string literal that this package hands to a failure as its code.

    Both spellings count: the domain raises `SlopWriterError(..., code="X")`,
    while the server's boundary builds its payload positionally
    (`_payload("FLOOD_WAIT", ...)`) for failures the domain never sees."""
    found: dict[str, set[str]] = {}
    for path in _package_sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            literals = [
                kw.value.value for kw in node.keywords
                if kw.arg == "code" and isinstance(kw.value, ast.Constant)
            ]
            literals += [
                arg.value for arg in node.args
                if isinstance(arg, ast.Constant) and arg.value in ERROR_CODES
            ]
            for code in literals:
                found.setdefault(code, set()).add(path.name)
    return found


# --------------------------------------------------------------------------
# Enforced at construction
# --------------------------------------------------------------------------


def test_a_code_outside_the_vocabulary_is_refused_at_construction():
    """#17 moved this from documented to enforced. A typo'd code is a bug in
    this package, not bad user input: the contract promises a closed set, and
    an unlisted token reaches the model as something it cannot branch on."""
    with pytest.raises(ValueError, match="not one of ERROR_CODES"):
        SlopWriterError("nope", code="NO_SUCH_CODE")


def test_every_code_in_the_vocabulary_can_be_raised():
    """The other direction of the same check — a typo *inside* the tuple would
    otherwise only surface at the raise site that needs it."""
    for code in ERROR_CODES:
        assert SlopWriterError("x", code=code).code == code


def test_a_failure_may_carry_no_code_at_all():
    """`code` stays optional: an unexpected exception reaching the server
    boundary has none of its own, and the boundary labels it there."""
    assert SlopWriterError("x").code is None


def test_usage_errors_are_held_to_the_same_vocabulary():
    """`UsageError` exists for the CLIs' exit 2 and for nothing else — it must
    not become a second, laxer contract."""
    assert UsageError("x", code="INVALID_ARGUMENT").exit_code == 2
    assert SlopWriterError("x").exit_code == 1
    with pytest.raises(ValueError):
        UsageError("x", code="NO_SUCH_CODE")


def test_the_message_and_the_hint_stay_apart():
    """They go to different places: a CLI prints both to stderr, the tool
    contract puts them in separate JSON fields."""
    err = SlopWriterError("what", hint="what to do", code="NO_DATA")
    assert (err.message, err.hint) == ("what", "what to do")
    assert str(err) == "what"


def test_the_vocabulary_is_a_set_written_as_a_tuple():
    """A duplicate is harmless at runtime and a sign the list was edited in two
    places — which is how a near-miss like NO_SESSION/NO_SESSIONS starts."""
    assert len(set(ERROR_CODES)) == len(ERROR_CODES)


# --------------------------------------------------------------------------
# Enforced over the source, where an unreachable raise site is still visible
# --------------------------------------------------------------------------


def test_no_raise_site_names_a_code_outside_the_vocabulary():
    """The check that construction cannot make in CI. Most raise sites need a
    Telegram session to reach, so `__init__`'s guard fires in a live run —
    which is exactly the acceptance step this ticket exists to stop relying
    on."""
    named = set(_codes_named_in_source())
    assert not named - set(ERROR_CODES)


def test_every_code_is_reachable_from_some_raise_site():
    """A code nothing raises is a promise to the model that never comes true:
    it appears in the contract's closed set, and the model may branch on a
    value it will never receive. `UNREACHED_CODES` is the ledger of exceptions
    and is empty — every code in the vocabulary is named somewhere."""
    unreached = set(ERROR_CODES) - set(_codes_named_in_source())
    assert unreached == UNREACHED_CODES


def test_internal_is_the_boundarys_alone():
    """`INTERNAL` labels an exception the domain did not anticipate, so it can
    only be named where anticipation runs out. A second module naming it means
    a raise site decided its own bug was unexpected — which is a code the model
    can do nothing with, chosen where a real one was available."""
    assert _codes_named_in_source()["INTERNAL"] == {"server.py"}


def test_the_tool_boundary_names_flood_wait_once_for_every_tool():
    """Telethon raises `FloodWaitError` from any call at any depth, so the
    boundary is the only place a *tool* can name it once rather than eleven
    times. `init.py` names it too and is not a counter-example: the login flood
    happens before any tool exists, on a call the CLI makes directly."""
    named = _codes_named_in_source()["FLOOD_WAIT"]
    assert "server.py" in named
    assert named <= {"server.py", "init.py"}


# --------------------------------------------------------------------------
# The prose half: a remedy the reader can actually follow
# --------------------------------------------------------------------------
#
# `errors.py` states the rule — a message or a hint names an argument or an
# operation, never a surface. It is enforced here rather than at the raise
# sites because it is a property of a *literal*, and literals are visible in
# source whether or not any test reaches the line.
#
# The scan is deliberately narrow: it catches the two forms that are
# unambiguously a command line, not the general claim. A hint that says
# "run `slop-writer init`" passes and should — that command is real, a human
# runs it, and `server._SETUP_HINT` swaps in the server's own wording anyway.
# What no reader of a tool result has is a flag or a script file.
_CLI_ONLY = re.compile(r"--[a-z]|[\w-]+\.py\b")


def _all_modules() -> dict[str, Path]:
    return {p.stem: p for p in Path(slop_writer.__file__).parent.glob("*.py")}


def _server_reachable() -> dict[str, Path]:
    """The modules a tool call can raise from, by walking `server.py`'s imports.

    Derived rather than listed, so the scope maintains itself: a module the
    server starts importing joins the scan without anyone remembering, and the
    exclusions are a consequence rather than a policy. `install`, `init` and
    `cli` fall out because nothing on a tool path imports them — their reader
    is always a human at a terminal, which is why "run `slop-writer install`
    again" is exactly the right thing for them to say.

    **Module-level imports only.** `render.summarize_install` imports `install`
    from inside its own body, and that deferred import is the author drawing
    this very line: the function renders for a human at a terminal, and the
    module says so. Following it would drag the whole CLI half back in."""
    modules = _all_modules()
    seen: set[str] = set()
    queue = deque(["server"])
    while queue:
        name = queue.popleft()
        if name in seen or name not in modules:
            continue
        seen.add(name)
        tree = ast.parse(modules[name].read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.level and node.module:
                queue.append(node.module.split(".")[0])
    return {name: modules[name] for name in seen}


def _failure_prose() -> list[tuple[str, int, str]]:
    """Every `message`/`hint` string a failure carries, as written in source.

    Only the *literal* parts of an f-string: an interpolated value is the
    caller's (`body_source`), and the CLI passing `--file draft.md` through it
    is the seam working, not a violation of it."""

    def literals(node: ast.expr) -> list[str]:
        if isinstance(node, ast.Constant):
            return [node.value] if isinstance(node.value, str) else []
        if isinstance(node, ast.JoinedStr):
            return [
                v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            ]
        if isinstance(node, ast.BinOp):
            return literals(node.left) + literals(node.right)
        return []

    found: list[tuple[str, int, str]] = []
    for name, path in sorted(_server_reachable().items()):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            called = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if called not in {"SlopWriterError", "UsageError"}:
                continue
            parts = list(node.args[:1]) + [
                kw.value for kw in node.keywords if kw.arg == "hint"
            ]
            for part in parts:
                found.extend(
                    (f"{name}.py", node.lineno, text) for text in literals(part)
                )
    return found


def test_the_scan_reaches_the_modules_a_tool_call_runs_through():
    """The guard above is only as good as its scope, and the scope is computed.
    A `server.py` rename or an import deleted by accident would otherwise
    shrink it to nothing and leave every test below passing vacuously."""
    reached = set(_server_reachable())
    assert {"server", "publish", "scheduled", "tg", "query", "group"} <= reached
    # Not reachable, and the reason the scan can afford to be strict.
    assert reached.isdisjoint({"install", "init", "cli"})


def test_no_failure_points_the_reader_at_a_surface_it_may_not_have():
    """A flag or a script name in a message or a hint is a remedy addressed to
    exactly one of two readers. #18 found `--at` and `--photo` being handed to
    a model; #40 found `tg_scrape.py scheduled --channel <chan>` still doing it
    from the read module next door, which #18's file-scoped test never opened.

    Every non-setup hint crosses to the model verbatim
    (`test_server.py::test_a_domain_hint_survives_when_it_is_not_a_setup_failure`),
    so there is no later stage that could rewrite this one."""
    offenders = [
        (module, line, text)
        for module, line, text in _failure_prose()
        if _CLI_ONLY.search(text)
    ]
    assert not offenders


def test_the_scan_would_catch_the_defect_it_was_written_for():
    """The scan is worth only what it rejects, and everything it guards now
    passes — so the regex is checked against the string #40 actually found,
    and against the two shapes that must keep passing."""
    assert _CLI_ONLY.search("List the queue with `tg_scrape.py scheduled --channel x`")
    assert _CLI_ONLY.search("Shorten the body, or drop --photo and retry.")
    assert not _CLI_ONLY.search("Ask the user to run `slop-writer init`, then stop.")
    assert not _CLI_ONLY.search("Scrape the channel first — em dashes are — fine.")
