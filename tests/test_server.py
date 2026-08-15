"""The MCP surface: the roster, the write gate, and the error contract.

Everything here builds a real `FastMCP` over a `tmp_path` project root and
asks it questions. Nothing touches Telegram: the two write paths reachable
without a session are the ones this suite drives, and they are exactly the
ones worth pinning — a validation failure that slipped past `_guarded`, or a
publish tool that reached the network before checking its arguments, are both
silent in a live run and loud here.

Three properties are load-bearing rather than incidental:

- **Names and permission rules are one fact.** Claude Code matches
  `mcp__slop-writer__publish_edit` by name, so a rename without its rule
  silently ungates a Telegram write. `permission_rules` and the roster are
  compared against each other, not against a literal list.
- **Validate, then require a session.** A tool that asked for a session first
  would answer "run `slop-writer init`" to a malformed `at`, pointing the
  model at setup it cannot do instead of the argument it can fix.
- **Text only.** Annotating a tool `-> str` is the obvious way to write one
  and silently turns on `outputSchema`, which Claude Code renders by
  discarding the prose (#12).
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from telethon.errors import FloodWaitError

from slop_writer.errors import ERROR_CODES, SlopWriterError
from slop_writer.server import (
    SERVER_NAME,
    WRITE_TOOLS,
    DEFAULT_SELECT,
    LatestSelect,
    WindowSelect,
    _guarded,
    _window,
    assert_text_only,
    build_server,
    normalize_channel,
    permission_rules,
)

from .conftest import run

READ_TOOLS = (
    "scrape_posts",
    "refresh_posts",
    "scan_linked_group",
    "scan_standalone_group",
    "fetch_subscribers",
    "fetch_views_by_hour",
    "list_scheduled",
    "run_query",
)


@pytest.fixture
def server(tmp_path):
    """A server over an empty project root — no session, no databases."""
    return build_server(tmp_path)


@pytest.fixture
def tools(server):
    return {t.name: t for t in run(server.list_tools())}


def call(server, name: str, **arguments) -> str:
    """One tool call, rendered the way the client renders it."""
    blocks = run(server.call_tool(name, arguments))
    return "\n".join(b.text for b in blocks)


def payload_of(exc: ToolError) -> dict:
    """The contract is the *tail* of the error text.

    The SDK prefixes it with `Error executing tool <name>: `, which is the one
    concession the JSON-in-text contract makes — so every reader of it, model
    or test, has to find the object rather than parse the whole string."""
    text = str(exc)
    return json.loads(text[text.index("{"):])


def failure(server, name: str, **arguments) -> dict:
    """Drive a tool to its error contract and hand back the parsed payload."""
    with pytest.raises(ToolError) as exc:
        call(server, name, **arguments)
    return payload_of(exc.value)


def iso_in(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).isoformat()


SOON = timedelta(minutes=90)   # past the MIN_LEAD floor
TOO_SOON = timedelta(minutes=30)


# --------------------------------------------------------------------------
# The roster
# --------------------------------------------------------------------------


def test_the_roster_is_the_eleven_tools_15_decided(tools):
    assert set(tools) == set(READ_TOOLS) | set(WRITE_TOOLS)


def test_no_tool_carries_an_output_schema(server, tools):
    """`assert_text_only` is the startup guard; this is the same check where a
    failure names the offending tool at development time instead of at boot."""
    assert [name for name, t in tools.items() if t.outputSchema is not None] == []
    run(assert_text_only(server))


def test_the_startup_guard_refuses_a_tool_that_leaked_structure(tmp_path):
    """The other half of the guard: that it *fails*.

    The test above proves the property holds today; this proves the check can
    still catch it being broken. `mcp.tool()` is the registration path someone
    reaches for when they add a tool without noticing `_tool` — and a bare
    `-> str` is enough, because the SDK wraps a scalar return in
    `{"result": …}` and calls that an output schema."""
    mcp = build_server(tmp_path)

    @mcp.tool()
    async def leaky() -> str:  # pragma: no cover - never called, only listed
        return "x"

    with pytest.raises(RuntimeError, match="structured output leaked"):
        run(assert_text_only(mcp))


def test_the_server_ships_no_instructions(server):
    """#21: `instructions` are always in context, so domain knowledge lives in
    the skill instead. Prose added here is paid for on every request of every
    session, which is the cost nobody decided to take on."""
    assert not server.instructions


def test_only_run_query_is_always_loaded(tools):
    """Every other tool is deferred behind ToolSearch (#15). `alwaysLoad` on a
    second tool is a per-turn context cost nobody decided to pay."""
    always = [
        name for name, t in tools.items()
        if (t.meta or {}).get("anthropic/alwaysLoad")
    ]
    assert always == ["run_query"]


def test_channel_is_required_on_every_tool(tools):
    """The map's forward-compat constraint: tools address data by channel.
    `scan_standalone_group` is the one exception in name only — its target is
    a group, and it says so."""
    for name, tool in tools.items():
        required = tool.inputSchema.get("required", [])
        handle = "group" if name == "scan_standalone_group" else "channel"
        assert handle in required, name


def test_no_tool_exposes_a_filesystem_path_for_state(tools):
    """`--output-dir`/`--session-file` are server configuration. `photo_paths`
    is the deliberate exception, and a recorded service-migration seam (#15)."""
    for name, tool in tools.items():
        leaked = set(tool.inputSchema.get("properties", {})) & {
            "output_dir", "session_file", "project", "project_root"
        }
        assert not leaked, f"{name} exposes {leaked}"


# --------------------------------------------------------------------------
# The write gate
# --------------------------------------------------------------------------


def test_the_ask_rules_name_tools_that_exist(tools):
    """A rule matching no tool is a gate over nothing — the failure mode of
    keeping the roster and the permission block in two files."""
    for rule in permission_rules()["ask"]:
        assert rule.rsplit("__", 1)[-1] in tools


def test_every_telegram_write_is_gated_and_nothing_else_is(tools):
    """The read/write split, checked in the direction that matters: a new
    `publish_*` tool added without its rule fails here."""
    gated = {r.rsplit("__", 1)[-1] for r in permission_rules()["ask"]}
    assert gated == {name for name in tools if name.startswith("publish_")}
    assert gated.isdisjoint(READ_TOOLS)


def test_the_allow_rule_covers_the_server_not_a_tool(tools):
    """`allow: [mcp__slop-writer]` + three `ask` entries is the whole block:
    precedence is deny → ask → allow with specificity ignored (#12), so the
    narrow rules beat the broad one."""
    assert permission_rules()["allow"] == [f"mcp__{SERVER_NAME}"]


def test_permission_rules_cannot_be_mutated_between_callers():
    rules = permission_rules()
    rules["ask"].clear()
    assert permission_rules()["ask"]


def test_a_telegram_write_is_never_annotated_read_only(tools):
    for name in WRITE_TOOLS:
        assert tools[name].annotations.readOnlyHint is False


def test_replacing_a_body_is_the_destructive_one(tools):
    """Scheduling adds and rescheduling moves; only `publish_edit` discards
    something a human may have written."""
    assert tools["publish_edit"].annotations.destructiveHint is True
    assert tools["publish_schedule"].annotations.destructiveHint is False


# --------------------------------------------------------------------------
# Write tools: validation happens before a session is required
# --------------------------------------------------------------------------


def test_scheduling_inside_the_floor_is_refused_without_a_session(server):
    """The whole point of the ordering. There is no session in this project
    root, so a session-first tool would answer `NO_SESSION` here and bury the
    real problem."""
    payload = failure(
        server, "publish_schedule",
        channel="@chan", body="hello", at=iso_in(TOO_SOON),
    )
    assert payload["code"] == "INVALID_SCHEDULE_TIME"
    assert "too soon" in payload["message"]


def test_rescheduling_re_applies_the_floor(server):
    payload = failure(
        server, "publish_reschedule",
        channel="@chan", message_id=42, at=iso_in(TOO_SOON),
    )
    assert payload["code"] == "INVALID_SCHEDULE_TIME"


def test_a_naive_time_is_refused_rather_than_guessed_at(server):
    payload = failure(
        server, "publish_schedule",
        channel="@chan", body="hello",
        at=(datetime.now(UTC) + SOON).replace(tzinfo=None).isoformat(),
    )
    assert payload["code"] == "INVALID_SCHEDULE_TIME"
    assert "offset" in payload["message"]


def test_an_empty_body_names_the_argument_it_came_from(server):
    """The CLI says "stdin" or "--file 'draft.md'"; a tool call has neither,
    so the message has to name the argument the model can actually edit."""
    payload = failure(
        server, "publish_schedule", channel="@chan", body="  ", at=iso_in(SOON)
    )
    assert payload["code"] == "INVALID_ARGUMENT"
    assert "`body`" in payload["message"]


def test_editing_to_an_empty_body_is_refused(server):
    payload = failure(server, "publish_edit", channel="@chan", message_id=42, body="")
    assert payload["code"] == "INVALID_ARGUMENT"


def test_a_photo_that_is_not_there_is_refused_before_the_network(server):
    payload = failure(
        server, "publish_schedule",
        channel="@chan", body="caption", at=iso_in(SOON),
        photo_paths=["/nope/missing.jpg"],
    )
    assert payload["code"] == "INVALID_ARGUMENT"
    assert "file not found" in payload["message"]


def test_caption_above_without_photos_is_refused(server):
    payload = failure(
        server, "publish_schedule",
        channel="@chan", body="hello", at=iso_in(SOON), caption_above=True,
    )
    assert payload["code"] == "INVALID_ARGUMENT"


def test_a_valid_request_gets_as_far_as_the_missing_session(server):
    """Everything the server can check without Telegram passed, so the next
    failure is setup — and it must arrive as the code the model branches on,
    carrying the server's own remedy rather than the CLI's setup skill."""
    payload = failure(
        server, "publish_schedule",
        channel="@chan", body="**hello**", at=iso_in(SOON),
    )
    assert payload["code"] == "NO_SESSION"
    assert "slop-writer init" in payload["hint"]


def test_editing_a_post_gets_as_far_as_the_missing_session(server):
    """`publish_edit` has no floor to check — adr/0003: a typo fix on an
    imminent post must not be blocked — so a well-formed body is the last
    thing standing between it and the network."""
    payload = failure(
        server, "publish_edit", channel="@chan", message_id=42, body="fixed",
    )
    assert payload["code"] == "NO_SESSION"


# --------------------------------------------------------------------------
# The error contract
# --------------------------------------------------------------------------


def test_every_payload_code_is_in_the_closed_vocabulary(server):
    """`code` is the seam the service's auth flow attaches to later, so an
    unlisted code is a contract break, not a typo."""
    payloads = [
        failure(server, "publish_schedule", channel="@c", body="x",
                at=iso_in(TOO_SOON)),
        failure(server, "publish_schedule", channel="@c", body="x", at=iso_in(SOON)),
        failure(server, "run_query", channel="@c", sql="SELECT 1"),
    ]
    for payload in payloads:
        assert payload["code"] in ERROR_CODES
        assert set(payload) >= {"code", "message", "hint"}


def test_a_query_before_the_first_scrape_says_so(server):
    payload = failure(server, "run_query", channel="@chan", sql="SELECT 1")
    assert payload["code"] == "NO_DATA"


def test_a_rejected_query_is_not_an_internal_error(server):
    payload = failure(server, "run_query", channel="@chan", sql="DROP TABLE posts")
    assert payload["code"] in {"QUERY_REJECTED", "NO_DATA"}


def test_a_flood_wait_carries_its_seconds(server):
    """Telethon raises this from any call at any depth, so the boundary is the
    only place it can be named once — and the number is what the model needs
    to decide between waiting and narrowing the request."""

    @_guarded
    async def flooded():
        raise FloodWaitError(request=None)

    with pytest.raises(ToolError) as exc:
        run(flooded())
    payload = payload_of(exc.value)
    assert payload["code"] == "FLOOD_WAIT"
    assert "seconds" in payload


def test_an_unexpected_exception_answers_in_the_contract(server):
    """Even a bug answers as JSON: a traceback is something the model can do
    nothing with, and `isError` without a code breaks the branch."""

    @_guarded
    async def broken():
        raise ZeroDivisionError("boom")

    with pytest.raises(ToolError) as exc:
        run(broken())
    payload = payload_of(exc.value)
    assert payload["code"] == "INTERNAL"
    assert "ZeroDivisionError" in payload["message"]


def test_a_domain_hint_survives_when_it_is_not_a_setup_failure(server):
    """Only `NO_CREDENTIALS`/`NO_SESSION` get the server's remedy swapped in;
    every other hint is the domain's own and must cross untouched."""

    @_guarded
    async def refused():
        raise SlopWriterError("nope", hint="try the other thing", code="NOT_ADMIN")

    with pytest.raises(ToolError) as exc:
        run(refused())
    assert payload_of(exc.value)["hint"] == "try the other thing"


def test_the_payload_is_json_in_the_text_block_not_structured_content(server):
    """#12: Claude Code discards `content` whenever `structuredContent` is
    present, so the machine-readable half travels *inside* the text."""
    payload = failure(server, "run_query", channel="@chan", sql="SELECT 1")
    assert isinstance(payload, dict)


# --------------------------------------------------------------------------
# Arguments
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [("@chan", "chan"), ("chan", "chan"), ("  @chan  ", "chan"),
     ("@MixedCase", "MixedCase")],
)
def test_at_prefixed_and_bare_handles_are_one_channel(raw, expected):
    """Case is deliberately left alone: the DB filename derives from this
    string, so folding case would hand a mixed-case handle a second, empty
    database."""
    assert normalize_channel(raw) == expected


def test_the_default_selection_is_the_newest_posts():
    """The CLI's bias, preserved: a bare `--limit` walks oldest-first from
    message 1, which is almost never what an analytics turn wants."""
    assert _window(DEFAULT_SELECT) == {
        "latest": 100, "limit": None, "offset_id": 0, "offset_date": None
    }


def test_a_window_selection_walks_from_the_offset():
    window = _window(WindowSelect(mode="window", offset_id=500, limit=20))
    assert window == {
        "latest": None, "limit": 20, "offset_id": 500, "offset_date": None
    }


def test_the_two_arms_are_mutually_exclusive_by_construction():
    """`latest` and `limit` were mutually exclusive CLI flags checked at
    runtime; the union is what makes the wrong pair unsayable."""
    assert _window(LatestSelect(count=5))["limit"] is None
    assert _window(WindowSelect(mode="window"))["latest"] is None


def test_a_foreign_field_is_rejected_rather_than_silently_dropped():
    """#23's pydantic trap: without `extra="forbid"` this validates as the
    `latest` arm and `offset_id` vanishes in silence."""
    with pytest.raises(Exception):
        LatestSelect.model_validate({"mode": "latest", "offset_id": 100})


def test_the_selection_union_reaches_the_model_nested(tools):
    """#23 measured it: a combinator under a property crosses byte-for-byte,
    while one at the root of `inputSchema` is flattened into a property bag
    with the arm-level `required` discarded. So `select` stays an object."""
    schema = tools["scrape_posts"].inputSchema
    assert "select" in schema["properties"]
    assert schema.get("$defs", {}).keys() >= {"LatestSelect", "WindowSelect"}
    for arm in ("LatestSelect", "WindowSelect"):
        assert schema["$defs"][arm]["additionalProperties"] is False


def test_the_union_crosses_as_a_combinator_with_its_tag(tools):
    """The shape #23 measured, asserted rather than assumed: `oneOf` over two
    `$ref` arms plus a `discriminator` naming `mode`. A model that has to guess
    which arm it is filling is the failure this buys off — and the arms are
    told apart by nothing else, since `mode` is the only field they share."""
    select = tools["scrape_posts"].inputSchema["properties"]["select"]
    assert select["discriminator"]["propertyName"] == "mode"
    assert [arm["$ref"].rsplit("/", 1)[-1] for arm in select["oneOf"]] == [
        "LatestSelect", "WindowSelect"
    ]


def test_the_window_arm_must_say_so_and_the_default_arm_need_not(tools):
    """`mode` is required on `WindowSelect` and defaulted on `LatestSelect`, so
    an omitted tag reads as `latest` — which is also what an omitted `select`
    means. Two ways to say the common thing, one way to say the rare one."""
    defs = tools["scrape_posts"].inputSchema["$defs"]
    assert defs["WindowSelect"]["required"] == ["mode"]
    assert "required" not in defs["LatestSelect"]


def test_selecting_a_window_is_optional_on_every_tool_that_takes_one(tools):
    """`select` absent must stay legal: `DEFAULT_SELECT` is the newest-first
    bias the CLI had, and requiring the argument would push the model into
    writing a window by hand."""
    for name, tool in tools.items():
        schema = tool.inputSchema
        if "select" in schema.get("properties", {}):
            assert "select" not in schema.get("required", []), name


def test_the_rendered_row_cap_is_bounded_in_the_schema(tools):
    """`truncate_cells=false` with an unbounded limit is how the 25,000-token
    output cap gets hit."""
    limit = tools["run_query"].inputSchema["properties"]["limit"]
    assert limit["maximum"] == 500
