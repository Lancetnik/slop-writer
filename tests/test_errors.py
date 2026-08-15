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
"""

import ast
from pathlib import Path

import pytest

import slop_writer
from slop_writer.errors import ERROR_CODES, SlopWriterError, UsageError

#: Vocabulary entries that no raise site names yet — see
#: `test_every_code_is_reachable_from_some_raise_site`. Listed rather than
#: tolerated so that adding a third is a decision someone makes on purpose.
UNREACHED_CODES = {
    # Telegram enforces the caption/body caps, not this package (adr/0003), so
    # a too-long body comes back from Telethon and reaches the model as
    # INTERNAL. The code is the shape of the fix, not a description of today.
    "MESSAGE_TOO_LONG",
    # The group scans do require membership, but the failure currently arrives
    # as whatever Telethon raised rather than as this code.
    "NOT_A_MEMBER",
}


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
    value it will never receive. The two exceptions are recorded above with
    the reason each is still aspirational."""
    unreached = set(ERROR_CODES) - set(_codes_named_in_source())
    assert unreached == UNREACHED_CODES


def test_the_boundarys_own_codes_come_from_the_boundary():
    """`INTERNAL` and `FLOOD_WAIT` are the server's to name — the domain never
    raises either, because neither is a failure it can recognise: one is an
    exception it did not anticipate, the other is raised by Telethon from any
    call at any depth."""
    named = _codes_named_in_source()
    assert named["INTERNAL"] == {"server.py"}
    assert named["FLOOD_WAIT"] == {"server.py"}
