"""The contract as a client actually receives it — one real stdio session.

`test_server.py` builds a `FastMCP` in-process and calls its tools directly.
That covers everything the server decides, and nothing the transport does:
`isError`, `structuredContent`, `_meta`, the JSON-Schema `$defs` a client
forwards to the model, and the version in the handshake are all produced on
the way out, by code no in-process call runs. #17 drove exactly this by hand
once, against a live channel; a harness that runs once is a fact about the
afternoon it ran.

So this module launches `slop-writer serve --mcp` as a subprocess and speaks
MCP to it. Two consequences worth stating rather than discovering:

- **The project root is the cwd**, with no `--project`. That is the shipped
  path (#19) — the client launches the server in the project directory, which
  is what lets the `.mcp.json` entry stay path-free and committable.
- **The server having started at all is an assertion.** `cli._serve` runs
  `assert_text_only` before a byte of protocol, so a handshake that completes
  is the guard passing in the real entrypoint rather than in a test's copy of
  it.

Nothing here touches Telegram: the project root holds one empty database and
no session, so `run_query` is the one tool that can succeed and every other
failure stops at setup.
"""

import asyncio
import json
import sys
import threading
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from slop_writer import __version__
from slop_writer.db import data_dir, open_db
from slop_writer.errors import ERROR_CODES
from slop_writer.server import SERVER_NAME, build_server

from .conftest import run

#: A channel whose database exists and holds no rows — enough for a query to
#: succeed, which no other tool can do without a session.
SEEDED = "seeded"
#: A channel nothing has scraped: the `NO_DATA` path.
UNSCRAPED = "unscraped"

#: Generous, because they are not measurements: the point is that a wedged
#: server fails the test instead of hanging CI until the job times out.
STARTUP_TIMEOUT = 60.0
CALL_TIMEOUT = 30.0


class _Wire:
    """One live server subprocess, driven from synchronous tests.

    The suite has no async plugin on purpose (`conftest.run` is `asyncio.run`,
    per #27), but a stdio session is not one coroutine: it is a process and two
    streams that must outlive every call made against them. So the session runs
    on a loop of its own thread and each call is submitted to it — which also
    buys one subprocess for the whole module instead of one per test.
    """

    def __init__(self, project_root: Path) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="mcp-stdio", daemon=True
        )
        self._thread.start()
        self._stack = AsyncExitStack()
        self.handshake = self._submit(self._open(project_root), STARTUP_TIMEOUT)

    def _submit(self, coro, timeout: float):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    async def _open(self, project_root: Path):
        params = StdioServerParameters(
            command=sys.executable,
            # `-m`, not the `slop-writer` console script: the script's location
            # depends on how the environment was built, while this interpreter
            # is the one running the suite by definition.
            args=["-m", "slop_writer.cli", "serve", "--mcp"],
            cwd=str(project_root),
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(
            ClientSession(read, write)
        )
        return await self._session.initialize()

    def tools(self) -> dict:
        listing = self._submit(self._session.list_tools(), CALL_TIMEOUT)
        return {tool.name: tool for tool in listing.tools}

    def call(self, name: str, **arguments):
        return self._submit(self._session.call_tool(name, arguments), CALL_TIMEOUT)

    def close(self) -> None:
        try:
            self._submit(self._stack.aclose(), CALL_TIMEOUT)
        except Exception:  # a server that already died is not a test failure
            pass
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)
            self._loop.close()


@pytest.fixture(scope="module")
def wire(tmp_path_factory):
    project_root = tmp_path_factory.mktemp("project")
    open_db(data_dir(project_root), SEEDED).close()
    session = _Wire(project_root)
    yield session
    session.close()


def text_of(result) -> str:
    return "\n".join(block.text for block in result.content if block.type == "text")


def contract_of(text: str) -> dict | None:
    """The `{code, message, hint}` object, or `None` if this is not it.

    Deliberately forgiving: the SDK puts its own prose in front of the payload,
    so the reader has to find the object rather than parse the string — and a
    failure that never reached `_guarded` has no object to find, which is a
    result this module asserts rather than an error it works around."""
    start = text.find("{")
    if start < 0:
        return None
    try:
        payload = json.loads(text[start:])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) and "code" in payload else None


# --------------------------------------------------------------------------
# The handshake
# --------------------------------------------------------------------------


def test_the_handshake_names_the_server_and_this_package_version(wire):
    """`FastMCP` takes no version, so `build_server` sets it through the SDK's
    private `_mcp_server` — behind a `getattr` guard, which means an SDK rename
    costs the version line silently and answers with the SDK's own instead.
    This is the only place that would notice."""
    info = wire.handshake.serverInfo
    assert info.name == SERVER_NAME
    assert info.version == __version__


def test_the_server_offers_no_instructions(wire):
    """#21: `instructions` ride in every request's context, so the domain
    knowledge lives in the skill. In-process this is a field on the object;
    here it is what the client was actually handed."""
    assert not wire.handshake.instructions


# --------------------------------------------------------------------------
# The surface, as the client forwards it to the model
# --------------------------------------------------------------------------


def test_the_transport_carries_the_roster_unchanged(wire, tmp_path):
    """Compared against the in-process server rather than a second literal
    list: the roster is pinned once, in `test_server.py`, and what this asks is
    whether anything is lost between registration and the wire."""
    expected = {tool.name for tool in run(build_server(tmp_path).list_tools())}
    assert set(wire.tools()) == expected


def test_no_tool_carries_an_output_schema_over_the_wire(wire):
    """#16: Claude Code discards the `content` blocks whenever structure is
    present. `_tool` pins `structured_output=False` and `assert_text_only`
    refuses to start otherwise — this is that promise as the client sees it."""
    leaked = [name for name, t in wire.tools().items() if t.outputSchema is not None]
    assert leaked == []


def test_the_always_load_flag_survives_the_transport(wire):
    """`_meta` is a passthrough field, and #15's whole deferral scheme rests on
    it arriving: `run_query` always loaded, the other ten behind ToolSearch."""
    always = [
        name for name, tool in wire.tools().items()
        if (tool.meta or {}).get("anthropic/alwaysLoad")
    ]
    assert always == ["run_query"]


def test_the_nested_union_survives_serialization(wire):
    """#23 measured this against a live client and found the combinator crosses
    byte-for-byte *under a property*. `$defs`/`$ref` are the part a naive
    schema flattener would resolve away, taking the arm boundaries with it."""
    schema = wire.tools()["scrape_posts"].inputSchema
    select = schema["properties"]["select"]
    assert select["discriminator"]["propertyName"] == "mode"
    assert [arm["$ref"].rsplit("/", 1)[-1] for arm in select["oneOf"]] == [
        "LatestSelect", "WindowSelect"
    ]
    for arm in ("LatestSelect", "WindowSelect"):
        assert schema["$defs"][arm]["additionalProperties"] is False


# --------------------------------------------------------------------------
# Results, as the client receives them
# --------------------------------------------------------------------------


def test_a_successful_call_is_text_and_only_text(wire):
    """The success half of #16, which no in-process call can show: the result
    carries prose and `structuredContent` stays absent, so the client renders
    what the tool wrote instead of a table built from a schema."""
    result = wire.call("run_query", channel=SEEDED, queries=[{"sql": "SELECT 1 AS one"}])
    assert result.isError is False
    assert result.structuredContent is None
    assert [block.type for block in result.content] == ["text"]
    assert "one" in text_of(result)


def test_a_failure_is_the_contract_inside_the_text_block(wire):
    """`isError` plus `{code, message, hint}` as text — not as structure,
    because structure would cost the message. The database for this channel
    was never created, which is the failure a first turn actually hits."""
    result = wire.call("run_query", channel=UNSCRAPED, queries=[{"sql": "SELECT 1"}])
    assert result.isError is True
    assert result.structuredContent is None

    text = text_of(result)
    # The payload is the *tail*: the SDK writes its own prose in front of it.
    # That prefix is the contract's one concession, so this asserts that
    # something precedes the object rather than pinning the SDK's wording.
    assert not text.startswith("{")

    payload = contract_of(text)
    assert payload is not None
    assert payload["code"] == "NO_DATA"
    assert payload["code"] in ERROR_CODES
    assert set(payload) >= {"code", "message", "hint"}


def test_a_rejected_query_is_a_section_not_a_call_failure(wire):
    """What the client actually receives for bad SQL: a *successful* call whose
    text carries the refusal, code and all. `isError` stays reserved for the
    call itself refusing — the test above, where there is no database to look
    in — so the two are visible here as different results of the same tool."""
    result = wire.call("run_query", channel=SEEDED, queries=[{"sql": "DROP TABLE posts"}])
    assert result.isError is False
    assert result.structuredContent is None
    assert "**failed** (QUERY_REJECTED)" in text_of(result)


def test_a_setup_failure_carries_the_servers_own_remedy(wire):
    """`NO_SESSION` is where the hosted service's auth flow will attach. The
    hint has to name `slop-writer init` — the CLI's setup skill is the wrong
    remedy here and the model cannot run a TTY login either way."""
    result = wire.call("list_scheduled", channel=SEEDED)
    assert result.isError is True
    payload = contract_of(text_of(result))
    assert payload["code"] in {"NO_SESSION", "NO_CREDENTIALS"}
    assert "slop-writer init" in payload["hint"]


def test_a_malformed_selection_arm_escapes_the_contract(wire):
    """The hole #17 recorded, kept as a test rather than as a sentence.

    `extra="forbid"` makes pydantic reject the arm *before* `_guarded` runs, so
    this one failure comes back outside the JSON contract — the model gets a
    validation message it can act on, but not a `code` it can branch on. If a
    future SDK or a boundary change closes the hole, this test fails, which is
    the point: the record stops being true out loud."""
    result = wire.call(
        "scrape_posts", channel=SEEDED,
        select={"mode": "latest", "offset_id": 100},
    )
    assert result.isError is True
    assert contract_of(text_of(result)) is None
