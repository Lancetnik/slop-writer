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

from slop_writer.db import data_dir
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
    codex_approval_rules,
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


def test_the_codex_approval_tables_name_tools_that_exist(tools):
    """The second client's gate, held to the first one's standard: a table
    naming no tool is a gate over nothing."""
    for name in codex_approval_rules():
        assert name in tools


def test_every_telegram_write_is_gated_in_codex_and_nothing_else_is(tools):
    """The direction that matters, again: a new `publish_*` tool added without
    its approval table fails here rather than shipping ungated on one client."""
    gated = set(codex_approval_rules())
    assert gated == {name for name in tools if name.startswith("publish_")}
    assert gated.isdisjoint(READ_TOOLS)


def test_the_two_gates_name_the_same_three_tools():
    """One roster, two client vocabularies. Claude Code matches a prefixed
    rule name, Codex a bare tool key under its server — and the emitters are
    two spellings of one fact, which is why they live in this module."""
    claude = {rule.rsplit("__", 1)[-1] for rule in permission_rules()["ask"]}
    assert claude == set(codex_approval_rules())


def test_the_codex_gate_asks_and_never_forbids(tools):
    """`approval_mode` has no deny value: this axis can only put a human in
    front of the call. Pinned so that a future edit to a stronger-sounding
    mode is a deliberate one."""
    assert all(
        table == {"approval_mode": "prompt"}
        for table in codex_approval_rules().values()
    )


def test_codex_approval_rules_cannot_be_mutated_between_callers():
    rules = codex_approval_rules()
    rules.clear()
    assert codex_approval_rules()


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
        failure(server, "run_query", channel="@c", queries=[{"sql": "SELECT 1"}]),
    ]
    for payload in payloads:
        assert payload["code"] in ERROR_CODES
        assert set(payload) >= {"code", "message", "hint"}


def test_a_query_before_the_first_scrape_says_so(server):
    """The one condition that is still `isError` on this path: no database
    means no per-question answer to give, however many questions were asked.
    That is what the flag is reserved for now that a query's own verdict
    travels as content."""
    payload = failure(server, "run_query", channel="@chan", queries=[{"sql": "SELECT 1"}])
    assert payload["code"] == "NO_DATA"


@pytest.fixture
def seeded(tmp_path):
    """A server over a project that has one real (empty) channel database."""
    from slop_writer.db import open_db

    open_db(data_dir(tmp_path), "chan").close()
    return build_server(tmp_path)


def test_one_bad_query_still_returns_the_others(seeded):
    """The batch's reason to exist: `isError` would discard three good answers
    to report one bad statement, and the model would have to re-ask them all —
    the round trip the batch was collapsing in the first place."""
    text = call(
        seeded, "run_query", channel="@chan",
        queries=[
            {"sql": "SELECT 1 AS one", "label": "fine"},
            {"sql": "SELECT nope FROM posts", "label": "broken"},
            {"sql": "SELECT 2 AS two", "label": "also fine"},
        ],
    )
    assert "## 1. fine" in text and "## 3. also fine" in text
    assert "**failed** (QUERY_REJECTED)" in text
    assert "| 1 |" in text and "| 2 |" in text


def test_a_rejected_statement_is_content_not_a_call_failure(seeded):
    """The database was there and the call ran, so the refusal is this query's
    verdict rather than the call's. Raising here would have made `isError`
    mean two unrelated things — "no database" and "bad SQL" — and only one of
    them is fixed by looking at the schema."""
    text = call(
        seeded, "run_query", channel="@chan", queries=[{"sql": "DROP TABLE posts"}]
    )
    assert text.startswith("**failed** (QUERY_REJECTED)")


def test_a_batch_that_answers_nothing_is_still_not_an_error(seeded):
    """The flag does not count failures. Every section failing is the same
    kind of answer as one section failing, and it says *which* question died
    of *what* — where one raised code would have had to stand for two
    unrelated refusals and name only the first."""
    text = call(
        seeded, "run_query", channel="@chan",
        queries=[
            {"sql": "DROP TABLE posts", "label": "one"},
            {"sql": "SELECT nope FROM posts", "label": "two"},
        ],
    )
    assert "## 1. one" in text and "## 2. two" in text
    assert text.count("**failed**") == 2


def test_a_lone_unlabelled_query_renders_as_the_bare_table(seeded):
    """`queries` carries the single question too, and there a section number
    orders nothing — the most common call must not pay for the batch's
    scaffolding."""
    text = call(seeded, "run_query", channel="@chan", queries=[{"sql": "SELECT 1 AS one"}])
    assert text.startswith("| one |")
    assert "##" not in text


def test_a_flood_wait_carries_its_seconds(tmp_path):
    """Telethon raises this from any call at any depth, so the boundary is the
    only place it can be named once — and the number is what the model needs
    to decide between waiting and narrowing the request."""

    @_guarded(data_dir(tmp_path))
    async def flooded():
        raise FloodWaitError(request=None)

    with pytest.raises(ToolError) as exc:
        run(flooded())
    payload = payload_of(exc.value)
    assert payload["code"] == "FLOOD_WAIT"
    assert "seconds" in payload


def test_an_unexpected_exception_answers_in_the_contract(tmp_path):
    """Even a bug answers as JSON: a traceback is something the model can do
    nothing with, and `isError` without a code breaks the branch."""

    @_guarded(data_dir(tmp_path))
    async def broken():
        raise ZeroDivisionError("boom")

    with pytest.raises(ToolError) as exc:
        run(broken())
    payload = payload_of(exc.value)
    assert payload["code"] == "INTERNAL"
    assert "ZeroDivisionError" in payload["message"]


def test_a_domain_hint_survives_when_it_is_not_a_setup_failure(tmp_path):
    """Only the setup pair and `CANNOT_RESOLVE` get the server's remedy
    swapped in; every other hint is the domain's own and must cross
    untouched."""

    @_guarded(data_dir(tmp_path))
    async def refused():
        raise SlopWriterError("nope", hint="try the other thing", code="NOT_ADMIN")

    with pytest.raises(ToolError) as exc:
        run(refused())
    assert payload_of(exc.value)["hint"] == "try the other thing"


def unresolvable(output_dir) -> dict:
    """Drive `CANNOT_RESOLVE` through the boundary and read its hint.

    Raised directly rather than through a tool: reaching `resolve_peer` needs
    a session, and the hint is the entrypoint's work either way."""

    @_guarded(output_dir)
    async def missing():
        raise SlopWriterError(
            "Cannot resolve @typo",
            hint="Check the handle for typos.",
            code="CANNOT_RESOLVE",
        )

    with pytest.raises(ToolError) as exc:
        run(missing())
    return payload_of(exc.value)


def test_an_unresolvable_handle_names_the_channels_the_project_has(tmp_path):
    """#43: the agent that cannot name a channel reads `.tg-analytic/` to find
    one — five of thirteen sessions in #41's drive did, one of them out of a
    neighbouring checkout. The failure it should have hit answers instead."""
    output_dir = data_dir(tmp_path)
    output_dir.mkdir(parents=True)
    (output_dir / "fastnewsdev.db").touch()
    (output_dir / "opensource_findings_chat.db").touch()

    hint = unresolvable(output_dir)["hint"]
    assert "fastnewsdev" in hint
    assert "opensource_findings_chat" in hint


def test_a_project_with_no_data_is_told_to_scrape_rather_than_offered_nothing(
    tmp_path,
):
    """The list is empty on every project before its first scrape, and
    "retry with one of these" over nothing is an instruction to guess."""
    hint = unresolvable(data_dir(tmp_path))["hint"]
    assert "scrape" in hint


def test_the_payload_is_json_in_the_text_block_not_structured_content(server):
    """#12: Claude Code discards `content` whenever `structuredContent` is
    present, so the machine-readable half travels *inside* the text."""
    payload = failure(server, "run_query", channel="@chan", queries=[{"sql": "SELECT 1"}])
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
