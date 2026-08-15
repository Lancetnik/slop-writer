"""`slop-writer install` / `uninstall` — the agent wiring, and nothing else.

The contract is Lancetnik/slop-writer#19: this module knows about MCP clients
and never about Telegram. No credentials, no TTY, no network — which is what
makes `install` and `init` independent in either order.

Three files get written, with **per-file idempotency** because they have
different owners:

- `.mcp.json` and the skill directory are ours, replaced wholesale every run;
  that replacement *is* the upgrade path.
- `.claude/settings.json` and `CLAUDE.md` are the human's, seeded on **first
  install only** — detected by the absence of our key in `.mcp.json`, so no
  extra marker file is needed. Someone who deliberately dropped
  `mcp__slop-writer__publish_schedule` from `ask` (headless autoposting by the
  channel's own owner, the case #15 protected) must not have it restored by an
  upgrade.

Nothing here prints: `install_project` / `uninstall_project` return a result
shape and `render.summarize_install` turns it into text, the same split every
other command in this package uses.
"""

import json
import shutil
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path

from .db import DATA_DIR_NAME
from .errors import SlopWriterError
from .server import SERVER_NAME, permission_rules

#: The skill directory, identical in the repository, in the wheel, and under
#: `.claude/skills/`. Both install channels (this one and `npx skills add`)
#: must land on the same path or a project ends up with two copies (#21).
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

#: Markers so `uninstall` can remove exactly what was written and a human can
#: see where their own text starts.
BLOCK_START = "<!-- slop-writer:start -->"
BLOCK_END = "<!-- slop-writer:end -->"

#: The read/write split, read from `server.py` rather than restated here.
#: #18 put it next to the roster it matches on purpose: a tool renamed without
#: its rule silently loses its gate, and this module would be the second place
#: to forget.
PUBLISH_TOOLS = tuple(permission_rules()["ask"])

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


def address_block() -> str:
    """What a Task-tool subagent is told, and no more.

    Server `instructions` never reach a subagent (#11), and a subagent that
    calls these tools without the metric invariants returns confidently wrong
    numbers. #26 measured which readers actually need this: a custom agent with
    a restricted `tools:` list, granted the MCP tools explicitly — it has no
    skills listing and no `Skill` tool at all, so a skill *name* is a door it
    cannot open. Hence a file path.

    Not one invariant is copied in. One fact in memory is bait that reads as
    'the knowledge is here' and stops the subagent fetching the other three."""
    skill_dir = "/".join((*SKILLS_PARENT, SKILL_DIR_NAME))
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
    """The enterprise policy file, if this machine has one."""
    for raw in MANAGED_MCP_PATHS:
        path = Path(raw)
        if path.is_file():
            return path
    return None


@dataclass
class InstallResult:
    project_root: Path
    version: str
    first_install: bool
    mcp_config: Path
    skill_target: Path
    settings: Path
    memory_file: Path
    permissions_seeded: bool
    memory_block_written: bool
    skill_existed: bool
    #: Which of our names `skills-lock.json` tracks — reported, never edited.
    skills_lock_names: tuple[str, ...]
    on_path: bool
    #: The pre-0.4 directory this run deleted, or None if there was none (#34).
    legacy_skill_removed: Path | None = None
    ask_tools: tuple[str, ...] = PUBLISH_TOOLS


@dataclass
class UninstallResult:
    project_root: Path
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)


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


def _write_memory_block(memory_path: Path) -> None:
    """Append the address block, creating CLAUDE.md if there is none."""
    existing = memory_path.read_text(encoding="utf-8") if memory_path.is_file() else ""
    if BLOCK_START in existing:
        return
    separator = "" if not existing or existing.endswith("\n\n") else (
        "\n" if existing.endswith("\n") else "\n\n"
    )
    memory_path.write_text(
        existing + separator + address_block() + "\n", encoding="utf-8"
    )


def _strip_memory_block(memory_path: Path) -> bool:
    """Remove our markers and everything between them. Returns whether it hit."""
    if not memory_path.is_file():
        return False
    text = memory_path.read_text(encoding="utf-8")
    start = text.find(BLOCK_START)
    end = text.find(BLOCK_END)
    if start == -1 or end == -1 or end < start:
        return False
    tail = text[end + len(BLOCK_END) :].lstrip("\n")
    head = text[:start].rstrip("\n")
    joined = f"{head}\n\n{tail}" if head and tail else (head or tail)
    memory_path.write_text(joined.rstrip("\n") + "\n" if joined else "", encoding="utf-8")
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
    write. Naming it is the honest boundary."""
    found = []
    for name in ("skills-lock.json", ".claude/skills-lock.json"):
        path = project_root / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for skill in (SKILL_DIR_NAME, LEGACY_SKILL_DIR_NAME):
            if skill in text and skill not in found:
                found.append(skill)
    return tuple(found)


def install_project(project_root: Path) -> InstallResult:
    """Wire this project's Claude Code config to the MCP server.

    Claude Code only. #19 rejected shipping entries for Cursor, Codex and the
    Copilot coding agent: their launch cwd is unverified, and this design rests
    on cwd being the project root — shipping the untested half means fielding
    the bug reports for it. `render.summarize_install` prints what those users
    should paste instead."""
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
    # First install is read *before* the entry is written — the absence of our
    # own key is the marker, so nothing extra has to be stored (#19).
    first_install = SERVER_NAME not in servers
    servers[SERVER_NAME] = server_entry()
    _write_json(mcp_config, config)

    settings = project_root / Path(*SETTINGS_PATH)
    if first_install:
        _seed_permissions(settings)

    memory_file = project_root / MEMORY_FILE
    if first_install:
        _write_memory_block(memory_file)

    skill_target = project_root / Path(*SKILLS_PARENT) / SKILL_DIR_NAME
    skill_existed = skill_target.exists()
    _copy_skill(skill_target)
    # After the copy, so a failure to find our own skill source leaves the old
    # directory in place rather than removing the only skill on the machine.
    legacy_skill_removed = _remove_legacy_skill(project_root)

    return InstallResult(
        project_root=project_root,
        version=package_version(),
        first_install=first_install,
        mcp_config=mcp_config,
        skill_target=skill_target,
        settings=settings,
        memory_file=memory_file,
        permissions_seeded=first_install,
        memory_block_written=first_install,
        skill_existed=skill_existed,
        legacy_skill_removed=legacy_skill_removed,
        skills_lock_names=_skills_lock_names(project_root),
        # The one self-check worth running: a `uv tool install` that missed
        # PATH surfaces inside the client as an undiagnosable "server didn't
        # start". No trial launch — it would prove nothing about how the
        # client launches us, and would cost seconds on every run.
        on_path=shutil.which(server_entry()["command"]) is not None,
    )


def uninstall_project(project_root: Path) -> UninstallResult:
    """Remove exactly what `install` wrote, and nothing else.

    `.tg-analytic/` is never touched: it holds a live Telegram session and
    per-channel databases that took hours of scraping to build. Permission
    entries are left too — they are the human's file, and an `ask` rule that
    outlives the server it guarded costs nothing."""
    result = UninstallResult(project_root=project_root)

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

    if _strip_memory_block(project_root / MEMORY_FILE):
        result.removed.append(f"{MEMORY_FILE} address block")

    if (project_root / DATA_DIR_NAME).exists():
        result.kept.append(f"{DATA_DIR_NAME}/ (session, databases, media)")
    settings = project_root / Path(*SETTINGS_PATH)
    if settings.is_file():
        result.kept.append(f"{'/'.join(SETTINGS_PATH)} (permission entries)")
    return result


def other_client_entry() -> str:
    """The JSON a Cursor / Codex / Copilot user pastes by hand.

    Printed rather than written, with its provenance attached: this project has
    verified the cwd assumption on Claude Code alone."""
    return json.dumps({"mcpServers": {SERVER_NAME: server_entry()}}, indent=2)
