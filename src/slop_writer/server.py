"""The MCP server: a second entrypoint into the domain the CLIs already call.

Every tool here parses arguments, calls one `slop_writer` function, and renders
its result — the same three steps the typer scripts take, which is the whole
point of the extraction in Lancetnik/slop-writer#22. No Telegram logic lives in
this module.

Four invariants, each decided elsewhere and enforced here:

1. **Text only.** No tool sets `structuredContent` (#16). Claude Code discards
   the `content` blocks entirely when structure is present, and the bytes the
   model receives are identical either way, so structure travels *inside* text.
   The SDK's default is the lossy branch — annotating `-> str` silently turns
   on `outputSchema` — so registration goes through `_tool` (which pins
   `structured_output=False`) and `assert_text_only` refuses to start a server
   that leaked one anyway.
2. **Nothing writes to stdout but the transport.** stdio *is* the JSON-RPC
   stream; a stray `print` corrupts the protocol. `render.summarize_*` return
   strings for exactly this reason, and the rest of the package logs through
   `logging`, which goes to stderr.
3. **Errors are `{code, message, hint}` JSON in the text block** (#15), over
   the closed `ERROR_CODES` vocabulary. The domain raises `SlopWriterError`;
   `_guarded` turns it into that payload. An unexpected exception becomes
   `INTERNAL` rather than a traceback, so even a bug answers in the contract.
4. **Tools address data by `channel`, never by path.** The project root is
   server configuration, closed over by `build_server` — invisible to the
   model, and the seam a hosted multi-tenant server would replace with a
   tenant lookup.
5. **Telegram writes are gated by *name*.** This module imports the read paths
   and `publish` alike, so adr/0003's "auditable at the file level" no longer
   holds at the entrypoint: what carries the split here is the `publish_`
   prefix, which is what a permission rule matches on. `WRITE_TOOLS` and
   `permission_rules` are that split written down, so `install` (#20) copies a
   list it cannot get out of step with.
"""

import functools
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from telethon.errors import FloodWaitError

from . import __version__
from .db import data_dir, scraped_channels
from .errors import SlopWriterError
from .group import scan_group
from .publish import edit_post, parse_schedule_time, prepare_schedule, render_body
from .publish import reschedule_post as reschedule_scheduled_post
from .publish import schedule_post as send_scheduled_post
from .query import run_query as run_sql
from .render import (
    summarize_group,
    summarize_query,
    summarize_schedule,
    summarize_scheduled,
    summarize_scrape,
    summarize_subscribers,
    summarize_views,
)
from .scheduled import list_scheduled as fetch_scheduled_queue
from .scrape import refresh_posts as refresh_post_ids
from .scrape import scrape_posts as scrape_channel
from .stats import fetch_subscribers as fetch_subscriber_stats
from .stats import fetch_views_by_hour as fetch_views_stats
from .tg import require_session, session_path

log = logging.getLogger(__name__)

SERVER_NAME = "slop-writer"

#: The tools that write to *Telegram*. Scans write too — to the local SQLite
#: DB — but they are idempotent re-upserts, and prompting on them trains the
#: human to click through, which is a net loss of safety (#15).
#:
#: The `publish_` prefix is load-bearing rather than cosmetic: Claude Code
#: matches permission rules by tool name, so the name is the only thing
#: carrying the read/write split into the prompt.
WRITE_TOOLS = ("publish_schedule", "publish_reschedule", "publish_edit")


def permission_rules() -> dict[str, list[str]]:
    """The permissions block `install` writes into `.claude/settings.json`.

    It lives here, next to the roster it matches, because the two are one fact
    in two files: a tool renamed without its rule silently loses its gate, and
    a rule naming no tool is a gate over nothing. `install` (#20) copies this;
    `tests/test_server.py` asserts the halves still line up.

    Precedence is deny → ask → allow with specificity *ignored*, so the three
    `ask` entries beat the server-wide `allow` (verified in #12).

    A returned dict, not a constant: nothing should be able to edit the gate
    of every other caller by mutating a shared literal."""
    return {
        "allow": [f"mcp__{SERVER_NAME}"],
        "ask": [f"mcp__{SERVER_NAME}__{name}" for name in WRITE_TOOLS],
    }

# The two failures whose remedy is a human at a terminal. The domain's own
# hints name the CLI's remedy (a setup skill, a script path); the server's
# remedy is `slop-writer init`, and picking it here rather than parameterising
# the raise sites keeps "how to report" where the entrypoint is — the same
# split that lets these functions serve two callers at all.
_SETUP_CODES = frozenset({"NO_CREDENTIALS", "NO_SESSION"})
_SETUP_HINT = (
    "Ask the user to run `slop-writer init` in their own terminal, then stop. "
    "Telegram's login prompts for an SMS code on a TTY, so it cannot be "
    "completed from a tool call."
)


def _resolve_hint(output_dir: Path) -> str:
    """A handle that names nothing is answered with the ones that do.

    Which channels exist is a fact about the *caller*, not about the Telegram
    lookup that failed, so it is attached here rather than in `resolve_peer` —
    the same split as `_SETUP_HINT`, and the seam a hosted server replaces
    with a tenant lookup while `tg.py` stays untouched.

    It replaces the domain's "check the handle for typos", which is the wrong
    remedy for the agent that had no handle to begin with: #41 watched five of
    thirteen sessions answer that question by reading `.tg-analytic/`, one of
    them out of a neighbouring checkout."""
    channels = scraped_channels(output_dir)
    if not channels:
        return (
            "Ask the user which channel they mean, then scrape it — this "
            "project has no channel data yet."
        )
    return (
        "Retry with one of the channels this project has data for: "
        f"{', '.join(channels)}. A private group also resolves only for an "
        "account that has already seen it."
    )


def normalize_channel(handle: str) -> str:
    """`@chan` and `chan` are one channel — decided server-side (#15).

    Case is left alone. Telegram treats usernames case-insensitively, so
    lowercasing would be *more* correct, but the DB filename is derived from
    this string and the CLIs do not lowercase: folding case here would hand a
    mixed-case handle a second, empty database."""
    return handle.strip().lstrip("@")


# --------------------------------------------------------------------------
# Selection: a tagged union, and nested on purpose.
#
# #23 verified this empirically: a combinator under a *property* crosses to the
# model byte-for-byte, while one at the root of `inputSchema` is flattened into
# a property bag with the arm-level `required` discarded. So `select` is an
# object, never spread across top-level arguments. The same research found that
# nothing validates tool input on the way in — hence `extra="forbid"`, without
# which `{"mode": "latest", "offset_id": 100}` validates as `latest` and the
# foreign field vanishes in silence.
# --------------------------------------------------------------------------


class LatestSelect(BaseModel):
    """The N most recent posts, newest first. The default, and the right
    choice unless you are deliberately paging through old history."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["latest"] = "latest"
    count: int = Field(
        100,
        ge=1,
        le=5000,
        description=(
            "How many recent posts. An album counts as one, so fewer coming "
            "back means the channel has no older history."
        ),
    )


class WindowSelect(BaseModel):
    """A chronological walk forward from an offset — for paging through
    history a page at a time. Walks oldest-first from the offset, so an
    unbounded window starting at 0 reads the whole channel."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["window"]
    offset_id: int = Field(
        0, ge=0, description="Start at this message id, inclusive. 0 = from the start."
    )
    offset_date: datetime | None = Field(
        None, description="Start after this moment (ISO-8601)."
    )
    limit: int | None = Field(
        None,
        ge=1,
        description=(
            "Cap on posts fetched in the walk. An album counts as one, so "
            "fewer coming back means the walk reached the newest post."
        ),
    )


Select = Annotated[LatestSelect | WindowSelect, Field(discriminator="mode")]

#: The default arm, as one shared instance — safe because nothing mutates a
#: selection: an omitted `select` is read straight through to `_window`, and a
#: supplied one is a fresh model the SDK validated into being.
DEFAULT_SELECT = LatestSelect()


def _window(select: LatestSelect | WindowSelect) -> dict:
    """The selection arm as the four keyword arguments the domain takes."""
    if isinstance(select, LatestSelect):
        return {"latest": select.count, "limit": None, "offset_id": 0,
                "offset_date": None}
    return {"latest": None, "limit": select.limit, "offset_id": select.offset_id,
            "offset_date": select.offset_date}


# --------------------------------------------------------------------------
# The error boundary
# --------------------------------------------------------------------------


def _payload(code: str, message: str, hint: str | None, **extra) -> str:
    """The failure contract from #15, in the text block rather than beside it."""
    return json.dumps(
        {"code": code, "message": message, "hint": hint, **extra},
        ensure_ascii=False,
    )


def _guarded(
    output_dir: Path,
) -> Callable[[Callable[..., Awaitable[str]]], Callable[..., Awaitable[str]]]:
    """Turn every failure into the `{code, message, hint}` contract.

    Raising (rather than returning) is what sets `isError: true`; the SDK
    prefixes the text with `Error executing tool <name>: `, so the payload is
    the tail of the string rather than the whole of it. That prefix is the one
    concession — the JSON itself crosses verbatim.

    Takes `output_dir` because two of the codes are answered by the
    *entrypoint* rather than by the domain that raised them: the setup pair
    with `slop-writer init`, and `CANNOT_RESOLVE` with the channels this
    project holds. Both are facts about where the server runs, which no raise
    site under `slop_writer` is allowed to know.

    `functools.wraps` matters beyond tidiness: FastMCP derives the tool's
    input schema from `inspect.signature`, which follows `__wrapped__`."""

    def decorator(
        fn: Callable[..., Awaitable[str]],
    ) -> Callable[..., Awaitable[str]]:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs) -> str:
            try:
                return await fn(*args, **kwargs)
            except SlopWriterError as exc:
                if exc.code in _SETUP_CODES:
                    hint = _SETUP_HINT
                elif exc.code == "CANNOT_RESOLVE":
                    hint = _resolve_hint(output_dir)
                else:
                    hint = exc.hint
                raise ToolError(
                    _payload(exc.code or "INTERNAL", exc.message, hint)
                ) from None
            except FloodWaitError as exc:
                # Telethon raises this from any call, at any depth, so the
                # boundary is the only place that can name it once.
                raise ToolError(
                    _payload(
                        "FLOOD_WAIT",
                        f"Telegram rate-limited this account for {exc.seconds}s.",
                        "Wait it out and retry; narrow the selection to ask "
                        "for less.",
                        seconds=exc.seconds,
                    )
                ) from None
            except Exception as exc:
                # Traceback to stderr for the human; a contract-shaped payload
                # for the model, which can do nothing with a traceback anyway.
                log.exception(
                    "unhandled error in %s", getattr(fn, "__name__", "tool")
                )
                raise ToolError(
                    _payload(
                        "INTERNAL",
                        f"{type(exc).__name__}: {exc}",
                        "Unexpected failure — the server's stderr has the "
                        "traceback.",
                    )
                ) from None

        return wrapper

    return decorator


async def assert_text_only(mcp: FastMCP) -> None:
    """Refuse to serve if any tool carries an `outputSchema`.

    Belt to `_tool`'s braces. Annotating `-> str` is the obvious way to write a
    summary tool and silently switches the SDK to structured output, which
    Claude Code renders by *discarding the prose*. One forgotten flag would
    break one tool quietly, so the check runs at startup where it is loud."""
    leaked = [t.name for t in await mcp.list_tools() if t.outputSchema is not None]
    if leaked:
        raise RuntimeError(
            f"structured output leaked: {leaked} — register through `_tool`, "
            "which pins structured_output=False"
        )


def _complete_sdk_settings_model() -> None:
    """Resolve the SDK's own `Settings` model before anything constructs it.

    `mcp.server.fastmcp.server.Settings` leaves `lifespan`'s forward reference
    unresolved, so pydantic-settings warns on every `FastMCP(...)` — every
    server start — into the stderr our own log lines share. `model_rebuild()`
    is what the warning asks for and leaves the class correctly defined, where
    a `filterwarnings` would also hide the same warning about *our* models.

    Best-effort: it reaches into another package's private shape, so a moved
    class costs a noisy stderr, never a failed start. `mcp` 1.29 is the newest
    release under the `<2` pin and still carries it."""
    try:
        from mcp.server.fastmcp.server import Settings

        Settings.model_rebuild()
    except Exception:  # pragma: no cover - cosmetic, never load-bearing
        pass


def build_server(project_root: Path) -> FastMCP:
    """The server for one project root.

    `project_root` is configuration, not an argument: the model never sees it
    and cannot redirect it, which is what makes `--output-dir`/`--session-file`
    absent from the surface. Closing over it (rather than reading a global)
    is the forward-compat constraint from the map — the hosted server replaces
    this closure with a per-tenant lookup and nothing else moves.

    `instructions` stays unset (#21): they are always in context, and the
    domain knowledge belongs in the skill, which is loaded only when needed."""
    output_dir = data_dir(project_root)
    session_file = str(session_path(project_root))

    _complete_sdk_settings_model()
    mcp = FastMCP(name=SERVER_NAME)
    # FastMCP takes no `version`, so `initialize` would otherwise answer with
    # the SDK's — which is the wrong number to read off a bug report. Private
    # attribute, hence the guard: a rename in the SDK costs us the version
    # line, not the server.
    server = getattr(mcp, "_mcp_server", None)
    if server is not None:
        server.version = __version__

    def _tool(**kwargs):
        """The only registration path. Pins the two things a per-tool decision
        would eventually get wrong: text-only output, and the error contract."""
        def decorator(fn):
            guarded = _guarded(output_dir)(fn)
            return mcp.tool(structured_output=False, **kwargs)(guarded)
        return decorator

    def _session() -> None:
        require_session(session_file, "slop-writer init")

    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    # "Read" in this server means "does not write to Telegram". Scans do write
    # — to the local SQLite DB — so they are not readOnly, but they are
    # idempotent re-upserts, which is why #15 leaves them un-gated.
    local_write = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
    # The Telegram writes. `publish_edit` is the destructive one: it discards
    # the body it replaces, while scheduling and rescheduling add or move.
    # Set for spec-correctness and relied on for nothing — #12 found the
    # annotations empirically inert; `permission_rules` is the actual gate.
    telegram_write = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
    telegram_overwrite = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

    @_tool(
        annotations=local_write,
        description=(
            "Scrape a Telegram channel's posts into the project's local "
            "database: text, media, forwards, reactions and comment threads, "
            "plus a metrics snapshot per run. Run this before querying a "
            "channel for the first time, and again to refresh.\n"
            "`select` picks the window: `{\"mode\": \"latest\", \"count\": N}` "
            "for the N newest posts (the default and the usual choice), or "
            "`{\"mode\": \"window\", \"offset_id\": …, \"limit\": …}` to page "
            "forward through history.\n"
            "`media=false` skips image downloads and `comments=false` skips "
            "comment threads — either cuts a slow pass. A cold or wide window "
            "runs to minutes and may finish in the background; wait for it "
            "rather than re-issuing. Re-scraping is safe: rows are upserted."
        ),
    )
    async def scrape_posts(
        ctx: Context,
        channel: str,
        select: Select = DEFAULT_SELECT,
        comments: bool = True,
        media: bool = True,
    ) -> str:
        _session()
        result = await scrape_channel(
            normalize_channel(channel),
            output_dir,
            session_file,
            with_comments=comments,
            with_media=media,
            with_channel_info=True,
            on_progress=_progress_hook(ctx),
            **_window(select),
        )
        return summarize_scrape(
            result.channel, result.posts, result.channels,
            result.history_exhausted,
        )

    @_tool(
        annotations=local_write,
        description=(
            "Refresh specific posts by id — one round trip, no history walk. "
            "Use it to pull fresh metrics for posts you already know about, or "
            "to fill in posts a group scan flagged as unscraped. Ids that no "
            "longer exist in the channel are skipped, not an error.\n"
            "Same persistence as a scrape: a new metrics snapshot per run, "
            "comment threads replaced, media downloaded unless `media=false`."
        ),
    )
    async def refresh_posts(
        ctx: Context,
        channel: str,
        post_ids: list[int],
        comments: bool = True,
        media: bool = True,
    ) -> str:
        _session()
        result = await refresh_post_ids(
            normalize_channel(channel),
            post_ids,
            output_dir,
            session_file,
            with_comments=comments,
            with_media=media,
            with_channel_info=True,
            on_progress=_progress_hook(ctx),
        )
        return summarize_scrape(
            result.channel, result.posts, result.channels,
            result.history_exhausted,
        )

    @_tool(
        annotations=local_write,
        description=(
            "Scan the discussion group linked to a channel: messages, comment "
            "threads, and join/leave events. Rows land in the CHANNEL's "
            "database and comment threads join to their posts, so this is the "
            "tool to use whenever the group belongs to a channel you analyse.\n"
            "Requires membership in the group (not admin). `select` takes the "
            "same window arms as `scrape_posts`, over group-message ids. "
            "Fails if the channel has no linked group."
        ),
    )
    async def scan_linked_group(channel: str, select: Select = DEFAULT_SELECT) -> str:
        _session()
        result = await scan_group(
            normalize_channel(channel), None, output_dir, session_file,
            **_window(select),
        )
        return summarize_group(
            result.label, result.overview, result.messages,
            result.events, result.threads,
        )

    @_tool(
        annotations=local_write,
        description=(
            "Scan a discussion group that is NOT the one attached to a channel "
            "you analyse. Rows land in the group's own database and nothing is "
            "linked to posts.\n"
            "If the group is linked to a channel you care about, use "
            "`scan_linked_group` with that channel instead — this tool would "
            "put the same messages in a separate database with no thread "
            "linkage. Requires membership in the group."
        ),
    )
    async def scan_standalone_group(group: str, select: Select = DEFAULT_SELECT) -> str:
        _session()
        result = await scan_group(
            None, normalize_channel(group), output_dir, session_file,
            **_window(select),
        )
        return summarize_group(
            result.label, result.overview, result.messages,
            result.events, result.threads,
        )

    @_tool(
        annotations=local_write,
        description=(
            "Pull subscriber growth and churn by source from Telegram's stats "
            "API and store it in the channel's database. Returns joins, "
            "leaves, net change and a breakdown by where subscribers came "
            "from.\n"
            "Requires ADMIN rights on the channel, and Telegram only produces "
            "these graphs once a channel is large enough (roughly 500+ "
            "subscribers)."
        ),
    )
    async def fetch_subscribers(channel: str) -> str:
        _session()
        result = await fetch_subscriber_stats(
            normalize_channel(channel), output_dir, session_file
        )
        return summarize_subscribers(result.channel, result.rows)

    @_tool(
        annotations=read_only,
        description=(
            "Views per hour of day from Telegram's stats API — the channel's "
            "daily rhythm, for choosing when to publish. Nothing is stored.\n"
            "Requires ADMIN rights on the channel, and a stats-eligible "
            "channel."
        ),
    )
    async def fetch_views_by_hour(channel: str) -> str:
        _session()
        result = await fetch_views_stats(normalize_channel(channel), session_file)
        return summarize_views(
            result.channel, result.hours, result.views,
            result.period_start, result.period_end,
        )

    @_tool(
        annotations=read_only,
        description=(
            "List the channel's scheduled, not-yet-published posts with their "
            "bodies, attachments and publish times. Read-only: scheduled posts "
            "have no engagement yet and nothing is stored.\n"
            "The `sched-msg #` in the output is the id to pass to the publish "
            "tools when rescheduling or editing — it is NOT the id the post "
            "gets once published. Requires post rights on the channel."
        ),
    )
    async def list_scheduled(channel: str) -> str:
        _session()
        result = await fetch_scheduled_queue(normalize_channel(channel), session_file)
        return summarize_scheduled(result.channel, result.items)

    @_tool(
        annotations=read_only,
        meta={"anthropic/alwaysLoad": True},
        description=(
            "Run one read-only SQL query against a channel's scraped database "
            "and get a Markdown table back. This is how every analytics "
            "question gets answered — scrape once, then query.\n"
            "A single SELECT or WITH statement; anything else is rejected and "
            "the database is opened read-only regardless. Aggregate in SQL "
            "rather than pulling rows and counting by hand.\n"
            "`limit` caps the RENDERED rows (default 50, max 500); the true "
            "row count is always reported, so a clipped answer is always "
            "visible as one. `truncate_cells=false` prints long text in full — "
            "needed to read post bodies, and the fastest way to blow the "
            "output cap.\n"
            "Requires a prior scrape of that channel. A query naming a table "
            "or column that does not exist comes back with the schema "
            "attached, so one retry is usually enough."
        ),
    )
    async def run_query(
        channel: str,
        sql: str,
        limit: int = Field(50, ge=0, le=500),
        truncate_cells: bool = True,
    ) -> str:
        # No session check: `query` and `db` are the Telegram-free pair, which
        # is what lets an analytics question be answered with no client at all.
        result = run_sql(sql, normalize_channel(channel), output_dir)
        return summarize_query(result.columns, result.rows, limit, truncate_cells)

    # ----------------------------------------------------------------------
    # The write surface (adr/0003). Three tools, gated by `permission_rules`.
    #
    # Each validates *before* `_session()`, exactly as the CLI does: a bad
    # time or an unreadable photo is worth reporting without a session, and it
    # keeps the failure the model sees pointed at the argument it can fix
    # rather than at setup it cannot.
    # ----------------------------------------------------------------------

    # `body` arrives as an argument here, where the CLI reads a file or stdin,
    # so the "where did this text come from" half of an empty-body error has to
    # name the argument instead.
    body_source = "the `body` argument"

    @_tool(
        annotations=telegram_write,
        description=(
            "Queue a new post to a Telegram channel at a future time. The post "
            "is SCHEDULED, not published: it sits in the channel's queue until "
            "`at`, and until then it can still be moved "
            "(`publish_reschedule`) or rewritten (`publish_edit`).\n"
            "`body` is Markdown, rendered straight to Telegram formatting "
            "entities. `at` is ISO-8601 WITH a UTC offset (e.g. "
            "2026-06-27T18:00:00+03:00) and must be at least 1 hour ahead — a "
            "time without an offset is rejected rather than guessed at, and "
            "the 1-hour floor has no override.\n"
            "`photo_paths` attaches local image files (.jpg/.jpeg/.png/.webp, "
            "up to 10 as one album) and makes `body` their caption, which may "
            "be empty; `caption_above` puts that caption on top of them. Body "
            "length is capped by Telegram, not by this server, and a rejected "
            "body queues nothing.\n"
            "Requires post rights on the channel. Nothing is stored locally, "
            "and the `sched-msg` id in the reply is NOT the id the post gets "
            "once it publishes."
        ),
    )
    async def publish_schedule(
        channel: str,
        body: str,
        at: str,
        photo_paths: list[str] | None = None,
        caption_above: bool = False,
    ) -> str:
        draft = prepare_schedule(
            body,
            at,
            photo_paths=photo_paths,
            caption_above=caption_above,
            body_source=body_source,
        )
        _session()
        result = await send_scheduled_post(
            normalize_channel(channel), draft, session_file
        )
        return summarize_schedule(result.channel, result.item, result.action)

    @_tool(
        annotations=telegram_write,
        description=(
            "Move an already-scheduled post to a different time. Body, photos "
            "and caption position are untouched — only the publish time "
            "changes.\n"
            "`message_id` is the `sched-msg` id from `list_scheduled`, not the "
            "id of a published post. `at` is ISO-8601 with a UTC offset and is "
            "held to the same 1-hour floor as `publish_schedule`, because it "
            "sets a new publish time.\n"
            "Requires post rights on the channel."
        ),
    )
    async def publish_reschedule(channel: str, message_id: int, at: str) -> str:
        when = parse_schedule_time(at)
        _session()
        result = await reschedule_scheduled_post(
            normalize_channel(channel), message_id, when, session_file
        )
        return summarize_schedule(result.channel, result.item, result.action)

    @_tool(
        annotations=telegram_overwrite,
        description=(
            "Replace the body of an already-scheduled post. The publish time "
            "is untouched, and there is NO 1-hour floor here: fixing a typo on "
            "an imminent post must not be blocked.\n"
            "`body` is the whole replacement text in Markdown — it is not "
            "appended, and the previous body is gone. Read the post first with "
            "`list_scheduled` if you are editing text you did not write in "
            "this session. `message_id` is that tool's `sched-msg` id.\n"
            "On a post with photos, `body` is the caption; the photos "
            "themselves cannot be changed by this tool.\n"
            "Requires post rights on the channel."
        ),
    )
    async def publish_edit(channel: str, message_id: int, body: str) -> str:
        text, entities = render_body(body, source=body_source)
        _session()
        result = await edit_post(
            normalize_channel(channel), message_id, text, entities, session_file
        )
        return summarize_schedule(result.channel, result.item, result.action)

    return mcp


def _progress_hook(ctx: Context):
    """Bridge the scrape pipeline's progress to an MCP notification.

    Claude Code sends a progress token on every call and the model never sees
    the notifications (#12), so this is not a UX feature: it resets the
    client's idle watchdog, which is what keeps a multi-minute scrape of a
    large channel alive."""

    async def hook(done: int, total: int) -> None:
        try:
            await ctx.report_progress(done, total)
        except Exception:  # a client that sent no token is not a scrape failure
            log.debug("progress notification failed", exc_info=True)

    return hook
