"""`slop-writer install` / `uninstall` — the agent wiring, and nothing else.

The contract is Lancetnik/slop-writer#19: this module knows about MCP clients
and never about Telegram. No credentials, no TTY, no network — which is what
makes `install` and `init` independent in either order.

**A client is selected, never detected** (adr/0008). `install` writes Claude
Code unless told otherwise and `--client codex` adds the second; autodetection
would write artifacts nobody asked for and leave `uninstall` guessing. Each
client is wired independently, so what a project holds for one says nothing
about another — first install is a property of a *client*, not of the project.

Written per client, with **per-file idempotency** because the files have
different owners:

- **Claude Code** — `.mcp.json` and `.claude/skills/<skill>/` are ours,
  replaced wholesale every run; that replacement *is* the upgrade path.
  `.claude/settings.json` and `CLAUDE.md` are the human's, seeded on **first
  install only**. Someone who deliberately dropped
  `mcp__slop-writer__publish_schedule` from `ask` (headless autoposting by the
  channel's own owner, the case #15 protected) must not have it restored by an
  upgrade.
- **Codex** — one file, `.codex/config.toml`, holding both halves: the server
  entry rewritten every run, the three `tools` approval tables seeded on first
  install only. Codex writes `approval_mode` into that same file when a human
  answers "don't ask again", so the two policies meet inside one file rather
  than either side of one. The **global** Codex config is never read for
  writing and never written: it is the human's file by the same rule that makes
  the permission block theirs, and on a real machine it holds other servers'
  bearer tokens.

Written on **every** install whichever clients were named, because they belong
to no client: the `AGENTS.md` address block and `.agents/skills/<skill>/`. An
agent nobody installed can still find the skill. Only an unnarrowed
`uninstall` removes them.

First install is detected per client from the absence of our own key in that
client's config, so no marker file is needed on either side.

Nothing here prints: `install_project` / `uninstall_project` return a result
shape and `render.summarize_install` turns it into text, the same split every
other command in this package uses.
"""

import json
import shutil
import sysconfig
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .db import DATA_DIR_NAME
from .errors import SlopWriterError, UsageError
from .server import SERVER_NAME, codex_approval_rules, permission_rules

#: The clients `install` knows how to wire. Values, not an enum: they are typed
#: at a command line and compared against argparse `choices`.
CLAUDE = "claude"
CODEX = "codex"
CLIENTS = (CLAUDE, CODEX)

#: What a bare `install` writes. Anything else would change what an upgrade
#: does to a project that only ever asked for one client.
DEFAULT_CLIENTS = (CLAUDE,)

#: The skill directory, identical in the repository, in the wheel, and under
#: each client's skills parent. Both install channels (this one and `npx skills
#: add`) must land on the same path or a project ends up with two copies (#21).
SKILL_DIR_NAME = "slop-writer"

#: What this skill was called before 0.4. A project installed from the old
#: `npx skills add` still carries it, and nothing renamed it in place — so a
#: pre-0.4 machine loads **two** model-invocable skills whose descriptions
#: disagree, the stale one advertising CLIs that #30 stopped documenting.
#: `install` deletes it (#34): #19's "it is the human's file" rule guards
#: config they authored, and this is a directory we wrote under our own
#: former name.
LEGACY_SKILL_DIR_NAME = "tg-analytic-skill"

MCP_CONFIG_NAME = ".mcp.json"
SETTINGS_PATH = (".claude", "settings.json")
SKILLS_PARENT = (".claude", "skills")
MEMORY_FILE = "CLAUDE.md"

#: Codex reads a project layer from `<dir>/.codex/config.toml` for every
#: directory between the project root and the cwd, and only once the human has
#: trusted the directory in their *own* config — which is why the report says
#: so and `install` does not try to grant it.
CODEX_CONFIG_PATH = (".codex", "config.toml")

#: The cross-agent pair, owned by no client. `AGENTS.md` is a convention
#: several agents read; `.agents/skills/` is where Codex discovers repo skills
#: without any config layer at all, so it keeps working in a directory the
#: human has not trusted yet.
AGENTS_FILE = "AGENTS.md"
AGENTS_SKILLS_PARENT = (".agents", "skills")

#: Markers so `uninstall` can remove exactly what was written and a human can
#: see where their own text starts.
BLOCK_START = "<!-- slop-writer:start -->"
BLOCK_END = "<!-- slop-writer:end -->"

#: The read/write split, read from `server.py` rather than restated here.
#: #18 put it next to the roster it matches on purpose: a tool renamed without
#: its rule silently loses its gate, and this module would be the second place
#: to forget. Both clients' gates come from there for the same reason.
PUBLISH_TOOLS = tuple(permission_rules()["ask"])
CODEX_PUBLISH_TOOLS = tuple(codex_approval_rules())

#: Per-server field found by #11 (milliseconds; values under 1000 are ignored).
#: A scrape runs for minutes, so the default is the wrong bet in the one
#: direction that is expensive — a killed server mid-ingest.
SERVER_TIMEOUT_MS = 600_000

#: Enterprise policy files: when one exists, MCP config is locked and writing
#: ours is pointless. #11 says report, never retry.
MANAGED_MCP_PATHS = (
    "/Library/Application Support/ClaudeCode/managed-mcp.json",
    "/etc/claude-code/managed-mcp.json",
    "C:\\ProgramData\\ClaudeCode\\managed-mcp.json",
)

#: Codex's equivalents. `/etc/codex/` is the Unix path; elsewhere the file
#: sits in `CODEX_HOME`, which defaults to `~/.codex`. A managed layer does not
#: merely coexist with the project layer — it **outranks** it, so a gate we
#: wrote could be overridden by one, which is a stronger reason to refuse than
#: the symmetry with the check above. The macOS MDM form is a managed
#: preference rather than a file and is undetectable here (adr/0008).
MANAGED_CODEX_PATHS = (
    "/etc/codex/managed_config.toml",
    "~/.codex/managed_config.toml",
)

#: Our tables inside Codex's config — the unit `install` replaces and
#: `uninstall` removes, and the only part of that file either of them touches.
_CODEX_TABLE = ("mcp_servers", SERVER_NAME)


def server_entry() -> dict:
    """The `.mcp.json` entry, deliberately free of machine-specific strings.

    No path argument: #19 checked live that Claude Code launches a stdio server
    with cwd = the project root, so `serve --mcp` resolves `.tg-analytic/` from
    there. That is the whole reason project scope exists — a teammate clones
    the repo, runs `slop-writer init`, restarts, and the committed entry works
    unchanged. `--project` stays available for a launcher whose cwd proves
    wrong, but `install` never writes it."""
    return {
        "type": "stdio",
        "command": "slop-writer",
        "args": ["serve", "--mcp"],
        "timeout": SERVER_TIMEOUT_MS,
    }


def codex_server_entry() -> dict:
    """The `[mcp_servers.slop-writer]` table, path-free for the same reason.

    Codex's own config vocabulary, not Claude Code's: no `type`, and the
    timeout keys are named and scaled differently, so nothing is carried over
    from `server_entry()` on the strength of the two looking alike. What is
    carried over is the *property* — an entry a teammate can clone and use.

    Committability is weaker on this client than on the other, and not because
    of this table: Codex ignores a project layer until the human trusts the
    directory, and that trust lives in their global config. Reported, not
    worked around."""
    return {"command": "slop-writer", "args": ["serve", "--mcp"]}


def address_block(skill_dir: str) -> str:
    """What a subagent — or an agent nobody installed — is told, and no more.

    Server `instructions` never reach a subagent (#11), and a subagent that
    calls these tools without the metric invariants returns confidently wrong
    numbers. #26 measured which readers actually need this: a custom agent with
    a restricted `tools:` list, granted the MCP tools explicitly — it has no
    skills listing and no `Skill` tool at all, so a skill *name* is a door it
    cannot open. Hence a file path, and hence a parameter: the same block
    addresses `.claude/skills/` in `CLAUDE.md` and `.agents/skills/` in
    `AGENTS.md`, and each file must name the copy its own readers can open.

    Not one invariant is copied in. One fact in memory is bait that reads as
    'the knowledge is here' and stops the subagent fetching the other three.
    Four lines, because `AGENTS.md` is injected into every turn's instructions
    from the repository root and every ancestor, under a truncating size cap."""
    return (
        f"{BLOCK_START}\n"
        f"Telegram channel analytics goes through the `mcp__{SERVER_NAME}__*` "
        "tools.\n"
        f"Read `{skill_dir}/SKILL.md` before calling one — it routes to the "
        "metric\ninvariants and the DB schema, without which the numbers come "
        "back wrong.\n"
        f"{BLOCK_END}"
    )


def skill_source() -> Path:
    """Where the skill directory this build ships actually sits.

    The wheel carries `skills/` as purelib data (`[tool.uv.build-backend]`),
    so an installed copy lands *beside* the package in site-packages rather
    than inside it — uv_build only packs files under the module root, and a
    symlink into `skills/` fails the build outright.

    The source checkout is tried **first** because an editable install has
    both: uv materialises the data directory at sync time and then never
    refreshes it when a skill file is edited, so a developer would otherwise
    install last sync's skill and see no reason why."""
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / "skills" / SKILL_DIR_NAME,
        Path(sysconfig.get_paths()["purelib"]) / SKILL_DIR_NAME,
        here.parent.parent / SKILL_DIR_NAME,
    ]
    for path in candidates:
        if (path / "SKILL.md").is_file():
            return path
    raise SlopWriterError(
        "The skill directory is missing from this installation.",
        hint=(
            "Reinstall the package (`uv tool install --force slop-writer`). "
            "Looked in: " + ", ".join(str(c) for c in candidates)
        ),
    )


def package_version() -> str:
    """The one version covering package and skill — they ship in one wheel."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("slop-writer")
    except PackageNotFoundError:  # pragma: no cover - source tree without dist
        return "unknown"


def managed_mcp_file() -> Path | None:
    """Claude Code's enterprise policy file, if this machine has one."""
    return _first_existing(MANAGED_MCP_PATHS)


def managed_codex_file() -> Path | None:
    """Codex's managed configuration layer, if this machine has one."""
    return _first_existing(MANAGED_CODEX_PATHS)


def _first_existing(paths: Iterable[str]) -> Path | None:
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_file():
            return path
    return None


def normalize_clients(clients: Sequence[str] | None, default: Sequence[str]) -> tuple[str, ...]:
    """The selection, de-duplicated in the order it was given.

    A name outside the roster is `UsageError` rather than a silent skip: a
    client that was asked for and not written is a project the human believes
    is wired and is not."""
    selected = default if clients is None else clients
    unknown = [name for name in selected if name not in CLIENTS]
    if unknown:
        # No `code=`: like every other raise in this module, this failure
        # reaches a human at a terminal and never the tool contract — the
        # server does not import `install`.
        raise UsageError(
            f"Unknown client: {', '.join(unknown)}.",
            hint=f"Known clients are {', '.join(CLIENTS)}.",
        )
    return tuple(dict.fromkeys(selected))


@dataclass
class ClientInstall:
    """What one client's half of a project now holds.

    One value per client rather than one value with a client's field names:
    first install, the artifacts written and the "already existed" facts all
    differ per client, and a single flag would lie in both directions."""

    client: str
    first_install: bool
    #: The file holding the server entry.
    config_file: Path
    #: The file holding the approval gate — a second file on Claude Code, the
    #: same file on Codex.
    gate_file: Path
    #: Whether this run wrote the gate. False on every run after the first, so
    #: a human who removed a rule keeps it removed.
    gate_seeded: bool
    #: The tool names this version expects behind the gate, in that client's
    #: spelling.
    gate_tools: tuple[str, ...]
    skill_target: Path | None = None
    skill_existed: bool = False
    address_file: Path | None = None
    address_block_written: bool = False
    #: The pre-0.4 directory this run deleted, or None if there was none (#34).
    legacy_skill_removed: Path | None = None


@dataclass
class InstallResult:
    project_root: Path
    version: str
    clients: tuple[ClientInstall, ...]
    #: The cross-agent pair, written whichever clients were selected.
    agents_file: Path
    agents_block_written: bool
    shared_skill_target: Path
    shared_skill_existed: bool
    #: Which of our names a `skills-lock.json` tracks — reported, never edited.
    skills_lock_names: tuple[str, ...]
    on_path: bool

    def for_client(self, client: str) -> ClientInstall | None:
        return next((c for c in self.clients if c.client == client), None)


@dataclass
class ClientUninstall:
    client: str
    removed: list[str] = field(default_factory=list)


@dataclass
class UninstallResult:
    project_root: Path
    clients: tuple[ClientUninstall, ...] = ()
    #: The cross-agent pair — removed only by an unnarrowed `uninstall`.
    shared_removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)

    def for_client(self, client: str) -> ClientUninstall | None:
        return next((c for c in self.clients if c.client == client), None)


# --------------------------------------------------------------------------
# JSON: Claude Code's two files
# --------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    """Parse a config file, treating a corrupt one as a hard stop.

    Silently starting from `{}` would drop every other MCP server in the file,
    which is the one failure a user cannot undo from the message alone."""
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as e:
        raise SlopWriterError(
            f"{path} is not valid JSON: {e}",
            hint="Fix or move the file, then run `slop-writer install` again. "
            "Overwriting it would drop whatever else it configures.",
        ) from None
    if not isinstance(loaded, dict):
        raise SlopWriterError(
            f"{path} does not hold a JSON object.",
            hint="Fix or move the file, then run `slop-writer install` again.",
        )
    return loaded


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# TOML: Codex's one file
#
# Read with the standard library, written by the small emitter below — no new
# runtime dependency, which is the same call `cli.py` made when it took
# argparse over typer. The emitter only ever writes *our* tables, which is what
# keeps it small: everything else in the file is carried across as the text the
# human wrote, comments included.
# --------------------------------------------------------------------------


def _toml_key(name: str) -> str:
    if name and all((c.isascii() and c.isalnum()) or c in "-_" for c in name):
        return name
    return _toml_string(name)


def _toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise SlopWriterError(
        f"Cannot write {type(value).__name__} into a Codex configuration.",
        hint="Remove the value from the `slop-writer` entry by hand, then run "
        "`slop-writer install` again.",
    )


def _emit_table(path: tuple[str, ...], mapping: dict) -> str:
    """One table and its sub-tables, in the shape adr/0008 shows.

    A table with sub-tables and no scalars of its own is left out entirely —
    `[mcp_servers.slop-writer.tools]` says nothing that the three tables under
    it do not."""
    scalars = {k: v for k, v in mapping.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in mapping.items() if isinstance(v, dict)}
    header = "[" + ".".join(_toml_key(part) for part in path) + "]"
    blocks = []
    if scalars or not tables:
        lines = [header]
        lines += [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in scalars.items()]
        blocks.append("\n".join(lines))
    blocks += [_emit_table((*path, key), sub) for key, sub in tables.items()]
    return "\n\n".join(blocks)


def _parse_key_path(raw: str) -> tuple[str, ...]:
    """A dotted TOML key, quoted segments included.

    Enough of the grammar to recognise our own table headers under whichever
    spelling a human used — `[mcp_servers.slop-writer]` or
    `["mcp_servers"."slop-writer"]` are the same table."""
    parts: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        while i < n and raw[i] in " \t":
            i += 1
        if i >= n:
            break
        if raw[i] in "\"'":
            quote = raw[i]
            i += 1
            buf: list[str] = []
            while i < n and raw[i] != quote:
                if quote == '"' and raw[i] == "\\" and i + 1 < n:
                    buf.append(raw[i + 1])
                    i += 2
                    continue
                buf.append(raw[i])
                i += 1
            i += 1
            parts.append("".join(buf))
        else:
            start = i
            while i < n and raw[i] not in ". \t":
                i += 1
            parts.append(raw[start:i])
        while i < n and raw[i] in " \t":
            i += 1
        if i < n and raw[i] == ".":
            i += 1
    return tuple(part for part in parts if part)


def _header_path(line: str) -> tuple[str, ...] | None:
    """The key path of a `[table]` / `[[array]]` header line, or None."""
    stripped = line.strip()
    if not stripped.startswith("["):
        return None
    start = 2 if stripped.startswith("[[") else 1
    quote = None
    for i in range(start, len(stripped)):
        char = stripped[i]
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "]":
            return _parse_key_path(stripped[start:i])
    return None


def _strip_toml_tables(text: str, path: tuple[str, ...]) -> tuple[str, int]:
    """Remove every table at or under `path`, and nothing else.

    Text surgery rather than a parse-and-re-emit round trip, because the
    alternative needs a *general* TOML writer — every type, every nesting — and
    would rewrite the whole file to move one table. This way the only lines
    that change are ours, and a comment the human wrote three tables down comes
    out byte for byte."""
    kept: list[str] = []
    removed = 0
    dropping = False
    for line in text.splitlines():
        here = _header_path(line)
        if here is not None:
            dropping = here[: len(path)] == path
            if dropping:
                removed += 1
                # The blank line that separated our block from what came
                # before it is ours too; leaving it behind grows the file by
                # one line per reinstall.
                while kept and not kept[-1].strip():
                    kept.pop()
        if not dropping:
            kept.append(line)
    return "\n".join(kept), removed


def _read_toml(path: Path) -> dict:
    """Parse Codex's config, treating a corrupt one as a hard stop — for the
    same reason `_read_json` does. This file can hold every other MCP server
    the human registered."""
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise SlopWriterError(
            f"{path} is not valid TOML: {e}",
            hint="Fix or move the file, then run `slop-writer install` again. "
            "Overwriting it would drop whatever else it configures.",
        ) from None


def _without_our_entry(config: dict) -> dict:
    """Everything in a parsed Codex config that is not ours to write."""
    rest = dict(config)
    servers = dict(rest.get("mcp_servers") or {})
    servers.pop(SERVER_NAME, None)
    if servers:
        rest["mcp_servers"] = servers
    else:
        rest.pop("mcp_servers", None)
    return rest


def _rewritten_codex_config(
    path: Path, text: str, before: dict, section: dict | None
) -> str:
    """The file with our tables replaced by `section`, or removed when None.

    The splice is **proved before it is written**: the result is re-parsed and
    compared against the original with our own entry taken out of both. Line
    surgery reads a `[header]` out of the text, and a header-shaped line inside
    a multi-line string would otherwise make it delete something the human
    wrote — the one failure that is silent and unrecoverable. A mismatch stops
    the command with the file untouched."""
    kept, _ = _strip_toml_tables(text, _CODEX_TABLE)
    head = kept.strip("\n")
    body = _emit_table(_CODEX_TABLE, section) if section is not None else ""
    spliced = ((f"{head}\n\n" if head else "") + body).strip("\n")
    new_text = f"{spliced}\n" if spliced else ""
    try:
        after = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError:
        after = None
    if after is None or _without_our_entry(after) != _without_our_entry(before):
        raise SlopWriterError(
            f"{path} cannot be rewritten without changing configuration this "
            "command does not own.",
            hint=f"Nothing was written. Remove the `mcp_servers.{SERVER_NAME}` "
            "tables by hand, then run the command again.",
        )
    return new_text


def _existing_codex_entry(config: dict, path: Path) -> dict | None:
    servers = config.get("mcp_servers")
    if servers is None:
        return None
    if not isinstance(servers, dict):
        raise SlopWriterError(
            f"{path} has an `mcp_servers` key that is not a table.",
            hint="Fix it by hand, then run `slop-writer install` again.",
        )
    entry = servers.get(SERVER_NAME)
    if entry is None or isinstance(entry, dict):
        return entry
    raise SlopWriterError(
        f"{path} has an `mcp_servers.{SERVER_NAME}` key that is not a table.",
        hint="Fix it by hand, then run `slop-writer install` again.",
    )


# --------------------------------------------------------------------------
# The artifacts
# --------------------------------------------------------------------------


def _seed_permissions(settings_path: Path) -> None:
    """The read/write split from #15, written once.

    `allow: ["mcp__slop-writer"]` matches the whole server and `ask` on the
    three `publish_*` names overrides it — precedence is deny > ask > allow
    with specificity ignored (#12), so the two entries compose exactly.

    Merged key by key rather than assigned: `permissions.allow` is where the
    user keeps every other rule in the project, and this is their file."""
    settings = _read_json(settings_path)
    permissions = settings.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise SlopWriterError(
            f"{settings_path} has a `permissions` key that is not an object.",
            hint="Fix it by hand, then run `slop-writer install` again.",
        )
    for key, rules in permission_rules().items():
        existing = permissions.setdefault(key, [])
        for rule in rules:
            if rule not in existing:
                existing.append(rule)
    _write_json(settings_path, settings)


def _write_address_block(address_path: Path, skill_dir: str) -> bool:
    """Append the address block, creating the file if there is none.

    Returns whether it wrote — an existing block is left exactly where the
    human moved it to."""
    existing = address_path.read_text(encoding="utf-8") if address_path.is_file() else ""
    if BLOCK_START in existing:
        return False
    separator = "" if not existing or existing.endswith("\n\n") else (
        "\n" if existing.endswith("\n") else "\n\n"
    )
    address_path.parent.mkdir(parents=True, exist_ok=True)
    address_path.write_text(
        existing + separator + address_block(skill_dir) + "\n", encoding="utf-8"
    )
    return True


def _strip_address_block(address_path: Path) -> bool:
    """Remove our markers and everything between them. Returns whether it hit."""
    if not address_path.is_file():
        return False
    text = address_path.read_text(encoding="utf-8")
    start = text.find(BLOCK_START)
    end = text.find(BLOCK_END)
    if start == -1 or end == -1 or end < start:
        return False
    tail = text[end + len(BLOCK_END) :].lstrip("\n")
    head = text[:start].rstrip("\n")
    joined = f"{head}\n\n{tail}" if head and tail else (head or tail)
    address_path.write_text(joined.rstrip("\n") + "\n" if joined else "", encoding="utf-8")
    return True


def _copy_skill(target: Path) -> None:
    """Replace the skill directory wholesale — never merge.

    The skill is documentation *of the server you have*, not the user's config.
    Offering to preserve their edits would promise fork support this project
    cannot deliver; `install` says what it overwrote instead (#19)."""
    source = skill_source()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def _remove_legacy_skill(project_root: Path) -> Path | None:
    """Delete the pre-0.4 skill directory, returning it if there was one.

    Two skills in one listing is not a cosmetic problem: both are
    model-invocable, both claim the same job, and the stale one describes an
    execution surface that no longer exists — so the agent picks between them
    with no way to tell which is current.

    Deleting rather than warning is #34's call. The counter-argument was #19's
    rule that `.claude/` belongs to the human, but that rule is about
    `settings.json` — config they wrote. This directory is our own artifact
    under our own former name, and `install` already replaces its successor
    wholesale for exactly that reason.

    Nothing here touches `skills-lock.json`: see `_skills_lock_names`."""
    legacy = project_root / Path(*SKILLS_PARENT) / LEGACY_SKILL_DIR_NAME
    if not legacy.is_dir():
        return None
    shutil.rmtree(legacy)
    return legacy


def _skills_lock_names(project_root: Path) -> tuple[str, ...]:
    """Which of our directory names the `npx skills add` channel tracks.

    Both channels serve the current directory from the same repository (#21),
    so that overlap is expected and drift is an accepted cost — but the lock's
    `computedHash` stops matching the moment `install` overwrites, and that
    should surface as a named consequence rather than as a bug report. An
    entry under the *old* name is the other half, and went undetected until
    #34: it outlives the directory `_remove_legacy_skill` just deleted.

    **Reported, never edited.** The lock is another tool's state file with its
    own hashing scheme; rewriting it means fighting npx for ownership of a
    format we do not control, and a wrong edit corrupts a file we did not
    write. Naming it is the honest boundary — and the same boundary applies to
    the cross-agent lock beside `.agents/skills/`, which is why that one is
    read here too and written nowhere."""
    found = []
    for name in ("skills-lock.json", ".claude/skills-lock.json", ".agents/skills-lock.json"):
        path = project_root / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for skill in (SKILL_DIR_NAME, LEGACY_SKILL_DIR_NAME):
            if skill in text and skill not in found:
                found.append(skill)
    return tuple(found)


# --------------------------------------------------------------------------
# Per-client install
#
# Each client comes in two halves: a `_state` function that reads and refuses,
# and an installer that writes. `install_project` runs **every** selected
# client's state function before any of them writes, so a machine-wide policy
# or a config nobody can parse stops the command with the project untouched
# rather than after one client was already wired — the same "validate before
# you act" ordering the write tools use at the server boundary.
# --------------------------------------------------------------------------


def _claude_state(project_root: Path) -> tuple[Path, dict, dict]:
    """Claude Code's config, and every reason not to write it."""
    managed = managed_mcp_file()
    if managed is not None:
        raise SlopWriterError(
            f"MCP configuration on this machine is managed by {managed}.",
            hint="An enterprise policy file takes exclusive control of MCP "
            "servers. Ask whoever administers it to add `slop-writer`; "
            "re-running this command cannot help.",
        )

    mcp_config = project_root / MCP_CONFIG_NAME
    config = _read_json(mcp_config)
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SlopWriterError(
            f"{mcp_config} has an `mcpServers` key that is not an object.",
            hint="Fix it by hand, then run `slop-writer install` again.",
        )
    return mcp_config, config, servers


def _install_claude(project_root: Path) -> ClientInstall:
    """Claude Code's four artifacts, unchanged from 0.4."""
    mcp_config, config, servers = _claude_state(project_root)
    # First install is read *before* the entry is written — the absence of our
    # own key is the marker, so nothing extra has to be stored (#19), and it is
    # now read per client, since a project can be a first install for one and
    # an upgrade for another.
    first_install = SERVER_NAME not in servers
    servers[SERVER_NAME] = server_entry()
    _write_json(mcp_config, config)

    settings = project_root / Path(*SETTINGS_PATH)
    if first_install:
        _seed_permissions(settings)

    address_file = project_root / MEMORY_FILE
    skill_target = project_root / Path(*SKILLS_PARENT) / SKILL_DIR_NAME
    memory_written = False
    if first_install:
        memory_written = _write_address_block(
            address_file, "/".join((*SKILLS_PARENT, SKILL_DIR_NAME))
        )

    skill_existed = skill_target.exists()
    _copy_skill(skill_target)
    # After the copy, so a failure to find our own skill source leaves the old
    # directory in place rather than removing the only skill on the machine.
    legacy_skill_removed = _remove_legacy_skill(project_root)

    return ClientInstall(
        client=CLAUDE,
        first_install=first_install,
        config_file=mcp_config,
        gate_file=settings,
        gate_seeded=first_install,
        gate_tools=PUBLISH_TOOLS,
        skill_target=skill_target,
        skill_existed=skill_existed,
        address_file=address_file,
        address_block_written=memory_written,
        legacy_skill_removed=legacy_skill_removed,
    )


def _codex_state(project_root: Path) -> tuple[Path, str, dict, dict | None]:
    """Codex's config as text and as a parse, and every reason not to write it.

    Reading it twice — once here to refuse, once in the installer to write — is
    the price of refusing before *any* client is touched. The file is small and
    both reads go through the same two functions, so there is one place where a
    refusal can be added."""
    managed = managed_codex_file()
    if managed is not None:
        raise SlopWriterError(
            f"Codex configuration on this machine is managed by {managed}.",
            hint="A managed layer outranks a project's own configuration, so "
            "the approval gate this command writes could be overridden "
            "without notice. Ask whoever administers it to add "
            "`slop-writer`; re-running this command cannot help.",
        )

    config_file = project_root / Path(*CODEX_CONFIG_PATH)
    text = config_file.read_text(encoding="utf-8") if config_file.is_file() else ""
    before = _read_toml(config_file)
    existing = _existing_codex_entry(before, config_file)

    if existing is not None and not _strip_toml_tables(text, _CODEX_TABLE)[1]:
        # The entry is there but not as a table header — an inline table or a
        # dotted key. Appending ours would be a duplicate definition, which
        # makes the whole file unreadable to Codex; removing it means editing
        # a line we cannot see the shape of.
        raise SlopWriterError(
            f"{config_file} defines `mcp_servers.{SERVER_NAME}` in a form this "
            "command cannot rewrite.",
            hint="Delete that entry by hand — as an inline table or a dotted "
            "key it cannot be replaced in place — then run `slop-writer "
            "install` again.",
        )
    return config_file, text, before, existing


def _install_codex(project_root: Path) -> ClientInstall:
    """Codex's one file, holding both halves of the wiring.

    The two idempotency policies meet inside it: the server entry is rewritten
    every run because that rewrite is the upgrade path, and the three approval
    tables are seeded on first install only because Codex itself writes into
    them when a human answers "don't ask me again"."""
    config_file, text, before, existing = _codex_state(project_root)
    first_install = existing is None

    section: dict = dict(codex_server_entry())
    # On an upgrade the gate is whatever the human left there, including
    # nothing: a table they deleted stays deleted, and a mode they changed
    # stays changed.
    tools = codex_approval_rules() if first_install else (existing or {}).get("tools")
    if isinstance(tools, dict) and tools:
        section["tools"] = tools

    new_text = _rewritten_codex_config(config_file, text, before, section)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(new_text, encoding="utf-8")

    return ClientInstall(
        client=CODEX,
        first_install=first_install,
        config_file=config_file,
        gate_file=config_file,
        gate_seeded=first_install,
        gate_tools=CODEX_PUBLISH_TOOLS,
    )


_INSTALLERS = {CLAUDE: _install_claude, CODEX: _install_codex}
#: Read-and-refuse, one per client. Same keys as `_INSTALLERS` by construction
#: of the loop below — a client with no state function raises `KeyError` before
#: anything is written rather than skipping its refusals.
_STATES = {CLAUDE: _claude_state, CODEX: _codex_state}


def install_project(
    project_root: Path, clients: Sequence[str] | None = None
) -> InstallResult:
    """Wire this project's client configuration to the MCP server.

    `clients` defaults to Claude Code alone, so a bare `install` writes exactly
    what it wrote before Codex existed. Cursor and the Copilot coding agent are
    still not written: their launch cwd is unverified and this design rests on
    cwd being the project root, so shipping the untested half means fielding
    the bug reports for it. `render.summarize_install` prints what those users
    should paste instead."""
    selected = normalize_clients(clients, DEFAULT_CLIENTS)
    # Every refusal first, so wiring two clients cannot write one and then stop
    # on the other's machine-wide policy — the human would be left holding a
    # failure and no report of what did land.
    for name in selected:
        _STATES[name](project_root)
    installed = tuple(_INSTALLERS[name](project_root) for name in selected)

    # Written whichever clients were named: these two belong to no client, and
    # an agent nobody installed still needs a way in.
    agents_file = project_root / AGENTS_FILE
    agents_written = _write_address_block(
        agents_file, "/".join((*AGENTS_SKILLS_PARENT, SKILL_DIR_NAME))
    )
    shared_skill = project_root / Path(*AGENTS_SKILLS_PARENT) / SKILL_DIR_NAME
    shared_existed = shared_skill.exists()
    _copy_skill(shared_skill)

    return InstallResult(
        project_root=project_root,
        version=package_version(),
        clients=installed,
        agents_file=agents_file,
        agents_block_written=agents_written,
        shared_skill_target=shared_skill,
        shared_skill_existed=shared_existed,
        skills_lock_names=_skills_lock_names(project_root),
        # The one self-check worth running: a `uv tool install` that missed
        # PATH surfaces inside the client as an undiagnosable "server didn't
        # start". No trial launch — it would prove nothing about how the
        # client launches us, and would cost seconds on every run.
        on_path=shutil.which(server_entry()["command"]) is not None,
    )


# --------------------------------------------------------------------------
# Per-client uninstall
# --------------------------------------------------------------------------


def _uninstall_claude(project_root: Path) -> ClientUninstall:
    result = ClientUninstall(client=CLAUDE)

    mcp_config = project_root / MCP_CONFIG_NAME
    config = _read_json(mcp_config)
    servers = config.get("mcpServers")
    if isinstance(servers, dict) and SERVER_NAME in servers:
        del servers[SERVER_NAME]
        _write_json(mcp_config, config)
        result.removed.append(f"{MCP_CONFIG_NAME} entry `{SERVER_NAME}`")

    skill_target = project_root / Path(*SKILLS_PARENT) / SKILL_DIR_NAME
    if skill_target.is_dir():
        shutil.rmtree(skill_target)
        result.removed.append(str(skill_target.relative_to(project_root)))

    if _strip_address_block(project_root / MEMORY_FILE):
        result.removed.append(f"{MEMORY_FILE} address block")
    return result


def _uninstall_codex(project_root: Path) -> ClientUninstall:
    result = ClientUninstall(client=CODEX)

    config_file = project_root / Path(*CODEX_CONFIG_PATH)
    if not config_file.is_file():
        return result
    text = config_file.read_text(encoding="utf-8")
    if not _strip_toml_tables(text, _CODEX_TABLE)[1]:
        return result
    # Parsed for the same reason `install` parses: a file this command cannot
    # read is a file it must not edit, and this one can hold every other MCP
    # server the human registered.
    new_text = _rewritten_codex_config(
        config_file, text, _read_toml(config_file), None
    )
    if new_text.strip():
        config_file.write_text(new_text, encoding="utf-8")
    else:
        # Nothing of the human's left in it — and if `.codex/` now holds
        # nothing either, it was ours to create and ours to take away.
        config_file.unlink()
        if not any(config_file.parent.iterdir()):
            config_file.parent.rmdir()
    result.removed.append(
        f"{'/'.join(CODEX_CONFIG_PATH)} entry `mcp_servers.{SERVER_NAME}`"
    )
    return result


_UNINSTALLERS = {CLAUDE: _uninstall_claude, CODEX: _uninstall_codex}


def uninstall_project(
    project_root: Path, clients: Sequence[str] | None = None
) -> UninstallResult:
    """Remove exactly what `install` wrote, and nothing else.

    With no selection this removes **every** client's wiring — safe in a way
    the install default is not, because uninstall only ever removes its own
    markers. Narrowed to one client it leaves the others alone, so dropping a
    client is not an all-or-nothing act.

    `.tg-analytic/` is never touched, whatever the selection: it holds a live
    Telegram session and per-channel databases that took hours of scraping to
    build. Permission entries are left too — they are the human's file, and an
    `ask` rule that outlives the server it guarded costs nothing."""
    selected = normalize_clients(clients, CLIENTS)
    result = UninstallResult(
        project_root=project_root,
        clients=tuple(_UNINSTALLERS[name](project_root) for name in selected),
    )

    # The cross-agent pair belongs to no client, so only a selection covering
    # all of them can take it: with Claude Code dropped and Codex kept, the
    # skill `.agents/skills/` holds is still the one Codex reads.
    if set(selected) == set(CLIENTS):
        shared_skill = project_root / Path(*AGENTS_SKILLS_PARENT) / SKILL_DIR_NAME
        if shared_skill.is_dir():
            shutil.rmtree(shared_skill)
            result.shared_removed.append(str(shared_skill.relative_to(project_root)))
        if _strip_address_block(project_root / AGENTS_FILE):
            result.shared_removed.append(f"{AGENTS_FILE} address block")

    if (project_root / DATA_DIR_NAME).exists():
        result.kept.append(f"{DATA_DIR_NAME}/ (session, databases, media)")
    settings = project_root / Path(*SETTINGS_PATH)
    if settings.is_file():
        result.kept.append(f"{'/'.join(SETTINGS_PATH)} (permission entries)")
    return result


def other_client_entry() -> str:
    """The JSON a Cursor / Copilot user pastes by hand.

    Printed rather than written, with its provenance attached: those two
    clients' launch cwd is unverified, and this design rests on it."""
    return json.dumps({"mcpServers": {SERVER_NAME: server_entry()}}, indent=2)
