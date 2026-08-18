"""`slop-writer install` / `uninstall` — the agent wiring (#19, #20, adr/0008).

Every test here runs against a `tmp_path` project. The one thing that cannot
be faked is which files a real user already has, so each test starts by
planting the config it is about to defend: another MCP server, a hand-written
permission list, a CLAUDE.md with the human's own notes.

Two clients now, and the shape of an assertion follows from that: a fact is
asserted against **one client's** half of the project, because what a project
holds for one says nothing about another. The three that belong to neither —
the cross-agent block, the cross-agent skill, `.tg-analytic/` surviving — are
asserted against the project.

What a client would find on disk is the subject throughout: the TOML emitter
is read back through the file it wrote, never called directly, exactly as the
JSON writer is.
"""

import json
import tomllib

import pytest

from slop_writer.errors import SlopWriterError, UsageError
from slop_writer.install import (
    BLOCK_END,
    BLOCK_START,
    CLAUDE,
    CLIENTS,
    CODEX,
    CODEX_PUBLISH_TOOLS,
    LEGACY_SKILL_DIR_NAME,
    PUBLISH_TOOLS,
    SERVER_NAME,
    codex_server_entry,
    install_project,
    server_entry,
    skill_source,
    uninstall_project,
)
from slop_writer.render import summarize_install, summarize_uninstall


def read(path):
    return json.loads(path.read_text())


def mcp_config(root):
    return read(root / ".mcp.json")["mcpServers"]


def settings(root):
    return read(root / ".claude" / "settings.json")["permissions"]


def codex_config(root):
    return tomllib.loads((root / ".codex" / "config.toml").read_text())


def codex_entry(root):
    return codex_config(root)["mcp_servers"][SERVER_NAME]


# --------------------------------------------------------------------------
# Client selection
# --------------------------------------------------------------------------


def test_a_bare_install_wires_claude_code_and_nothing_else(tmp_path):
    """User story 3: upgrading changes nothing about what a project that only
    ever used one client holds."""
    result = install_project(tmp_path)

    assert [c.client for c in result.clients] == [CLAUDE]
    assert mcp_config(tmp_path)[SERVER_NAME] == server_entry()
    assert not (tmp_path / ".codex").exists()


def test_naming_claude_is_the_same_as_naming_nothing(tmp_path):
    explicit = install_project(tmp_path, [CLAUDE])

    assert [c.client for c in explicit.clients] == [CLAUDE]
    assert mcp_config(tmp_path)[SERVER_NAME] == server_entry()


def test_the_selection_is_repeatable_and_deduplicated(tmp_path):
    result = install_project(tmp_path, [CODEX, CLAUDE, CODEX])

    assert [c.client for c in result.clients] == [CODEX, CLAUDE]
    assert SERVER_NAME in mcp_config(tmp_path)
    assert SERVER_NAME in codex_config(tmp_path)["mcp_servers"]


def test_an_unknown_client_is_a_usage_error(tmp_path):
    """Named and not written is worse than refused: the human would believe
    the project is wired for something it is not."""
    with pytest.raises(UsageError):
        install_project(tmp_path, ["emacs"])
    with pytest.raises(UsageError):
        uninstall_project(tmp_path, ["emacs"])


def test_first_install_is_a_property_of_a_client(tmp_path):
    """A project can be an upgrade for one client and a first install for
    another, which is why the flag hangs off the client and not the result."""
    install_project(tmp_path, [CLAUDE])

    second = install_project(tmp_path, [CLAUDE, CODEX])

    assert not second.for_client(CLAUDE).first_install
    assert second.for_client(CODEX).first_install


def test_first_install_is_detected_from_the_clients_own_entry_alone(tmp_path):
    """No marker file, on either side: the absence of our key in that client's
    config is the signal, so there is no second piece of state to drift out of
    sync (#19)."""
    for client in (CLAUDE, CODEX):
        assert install_project(tmp_path, [client]).for_client(client).first_install
        assert not install_project(tmp_path, [client]).for_client(client).first_install


@pytest.mark.parametrize("client", CLIENTS)
def test_every_known_client_can_be_wired_and_unwired(tmp_path, client):
    """The roster and the two per-client tables are one fact — a name accepted
    at the command line with no installer behind it would fail at the moment a
    user typed it."""
    assert install_project(tmp_path, [client]).for_client(client) is not None

    result = uninstall_project(tmp_path, [client])

    assert result.for_client(client).removed


def test_a_refusal_for_one_client_writes_nothing_for_any(tmp_path, monkeypatch):
    """Wiring two clients is one command, so it either reports what landed or
    lands nothing. Writing Claude Code and *then* refusing Codex leaves the
    human with a failure and no report of the half that succeeded."""
    policy = tmp_path / "managed_config.toml"
    policy.write_text("")
    monkeypatch.setattr("slop_writer.install.MANAGED_CODEX_PATHS", (str(policy),))

    with pytest.raises(SlopWriterError):
        install_project(tmp_path, [CLAUDE, CODEX])

    assert not (tmp_path / ".mcp.json").exists()
    assert not (tmp_path / "AGENTS.md").exists()


def test_installing_one_client_leaves_the_others_half_untouched(tmp_path):
    """The independence the glossary's **Client** entry claims."""
    install_project(tmp_path, [CLAUDE])
    before = (tmp_path / ".mcp.json").read_text()

    install_project(tmp_path, [CODEX])

    assert (tmp_path / ".mcp.json").read_text() == before


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------


def test_the_server_entry_carries_no_machine_specific_string():
    """#19 overturned "bake in an absolute path": the client launches a stdio
    server with cwd = the project root, so the entry stays committable and a
    teammate who clones the repo needs no edit."""
    entry = server_entry()
    assert entry["args"] == ["serve", "--mcp"]
    assert not any("/" in arg or "\\" in arg for arg in entry["args"])
    assert "cwd" not in entry
    assert "--project" not in entry["args"]


def test_install_merges_into_an_existing_mcp_config(tmp_path):
    """Other servers survive. Clobbering the file is the one failure a user
    cannot undo from the message alone."""
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"codegraph": {"command": "codegraph"}}})
    )
    install_project(tmp_path)

    servers = mcp_config(tmp_path)
    assert servers["codegraph"] == {"command": "codegraph"}
    assert servers[SERVER_NAME] == server_entry()


def test_install_seeds_the_read_write_split(tmp_path):
    """`allow` on the whole server plus `ask` on the three publish names: with
    specificity ignored and ask > allow (#12), those two entries compose into
    exactly the intended shape."""
    result = install_project(tmp_path)

    perms = settings(tmp_path)
    assert f"mcp__{SERVER_NAME}" in perms["allow"]
    assert list(PUBLISH_TOOLS) == perms["ask"]
    assert result.for_client(CLAUDE).gate_seeded


def test_the_permission_block_is_the_servers_own(tmp_path):
    """#18 put `permission_rules()` next to the roster it matches, so that a
    tool renamed without its rule cannot silently lose its gate. `install`
    copies it rather than restating it — this is the assertion that it stays
    a copy and not a second source."""
    from slop_writer.server import permission_rules

    install_project(tmp_path)

    perms = settings(tmp_path)
    for key, rules in permission_rules().items():
        assert perms[key] == rules


def test_install_keeps_the_users_own_permission_entries(tmp_path):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}))

    install_project(tmp_path)

    assert settings(tmp_path)["allow"][0] == "Bash(ls:*)"


def test_a_reinstall_does_not_restore_a_removed_ask_rule(tmp_path):
    """The case #15 protected when it rejected `requiresUserInteraction`:
    headless autoposting by the channel's own owner. Someone who deliberately
    dropped a publish tool from `ask` must not have it silently restored by an
    upgrade — which is why the permission block is first-install only."""
    install_project(tmp_path)
    path = tmp_path / ".claude" / "settings.json"
    data = read(path)
    data["permissions"]["ask"].remove(PUBLISH_TOOLS[0])
    path.write_text(json.dumps(data))

    result = install_project(tmp_path)

    assert not result.for_client(CLAUDE).first_install
    assert not result.for_client(CLAUDE).gate_seeded
    assert PUBLISH_TOOLS[0] not in settings(tmp_path)["ask"]


def test_the_server_entry_is_replaced_on_every_run(tmp_path):
    """The other half of per-file idempotency: our own files *are* the upgrade
    path, so a stale entry from an older version gets overwritten."""
    install_project(tmp_path)
    path = tmp_path / ".mcp.json"
    data = read(path)
    data["mcpServers"][SERVER_NAME]["args"] = ["serve", "--legacy"]
    path.write_text(json.dumps(data))

    install_project(tmp_path)

    assert mcp_config(tmp_path)[SERVER_NAME] == server_entry()


def test_install_writes_the_skill_and_replaces_it_next_time(tmp_path):
    result = install_project(tmp_path)
    skill = tmp_path / ".claude" / "skills" / "slop-writer"

    assert (skill / "SKILL.md").is_file()
    assert not result.for_client(CLAUDE).skill_existed

    # The skill is documentation of the server you have, never the user's
    # config — a local edit is not preserved, and `install` says so (#19).
    (skill / "SKILL.md").write_text("mine now")
    (skill / "stray.md").write_text("left over from an older version")
    again = install_project(tmp_path)

    assert again.for_client(CLAUDE).skill_existed
    assert (skill / "SKILL.md").read_text() != "mine now"
    assert not (skill / "stray.md").exists()


def plant_legacy_skill(root):
    """A pre-0.4 project: the old `npx skills add` directory, still loadable."""
    legacy = root / ".claude" / "skills" / LEGACY_SKILL_DIR_NAME
    (legacy / "references").mkdir(parents=True)
    (legacy / "SKILL.md").write_text("---\nname: tg-analytic-skill\n---\n")
    (legacy / "references" / "scraping.md").write_text("tg_scrape.py --latest")
    return legacy


def test_install_removes_the_pre_04_skill_directory(tmp_path):
    """#34: the rename left the old directory loadable, so a pre-0.4 machine
    carries two model-invocable skills claiming the same job — and since #30
    the stale one advertises CLIs that are documented nowhere. `install`
    deletes it, because it is our artifact under our own former name."""
    legacy = plant_legacy_skill(tmp_path)

    result = install_project(tmp_path)

    assert not legacy.exists()
    assert result.for_client(CLAUDE).legacy_skill_removed == legacy
    assert (tmp_path / ".claude" / "skills" / "slop-writer" / "SKILL.md").is_file()


def test_removing_the_old_skill_is_never_silent(tmp_path):
    """Deleting something the user did not ask us to delete is exactly what
    #19's "say what you wrote" rule exists for."""
    plant_legacy_skill(tmp_path)

    printed = summarize_install(install_project(tmp_path))

    assert LEGACY_SKILL_DIR_NAME in printed
    assert "removed" in printed


def test_a_project_without_the_old_skill_reports_no_removal(tmp_path):
    """The common case says nothing — an upgrade note that fires on every
    install stops being read."""
    result = install_project(tmp_path)

    assert result.for_client(CLAUDE).legacy_skill_removed is None
    assert LEGACY_SKILL_DIR_NAME not in summarize_install(result)


def test_install_refuses_to_guess_at_corrupt_json(tmp_path):
    """Starting from `{}` would drop every other server in the file."""
    (tmp_path / ".mcp.json").write_text("{ not json")

    with pytest.raises(SlopWriterError) as e:
        install_project(tmp_path)
    assert "not valid JSON" in e.value.message


def test_install_stops_when_mcp_config_is_enterprise_managed(tmp_path, monkeypatch):
    """#11: a `managed-mcp.json` takes exclusive control and `add` fails
    outright. Report, never retry."""
    policy = tmp_path / "managed-mcp.json"
    policy.write_text("{}")
    monkeypatch.setattr("slop_writer.install.MANAGED_MCP_PATHS", (str(policy),))

    with pytest.raises(SlopWriterError) as e:
        install_project(tmp_path)
    assert "managed" in e.value.message
    assert str(policy) in e.value.message


def test_a_managed_claude_policy_does_not_stop_the_other_client(tmp_path, monkeypatch):
    """Independence again: one client's machine-wide policy says nothing about
    what the other can be wired for."""
    policy = tmp_path / "managed-mcp.json"
    policy.write_text("{}")
    monkeypatch.setattr("slop_writer.install.MANAGED_MCP_PATHS", (str(policy),))

    assert install_project(tmp_path, [CODEX]).for_client(CODEX).first_install


# --------------------------------------------------------------------------
# Codex
# --------------------------------------------------------------------------


def test_the_codex_entry_carries_no_machine_specific_string():
    """The property the Claude Code entry has, held to for the second client:
    a committed entry a teammate clones and uses unchanged. If a live run ever
    shows Codex launching the server somewhere other than the project root,
    this is the assertion that has to be changed on purpose."""
    entry = codex_server_entry()
    assert entry["args"] == ["serve", "--mcp"]
    assert not any("/" in arg or "\\" in arg for arg in entry["args"])
    assert "cwd" not in entry
    assert "--project" not in entry["args"]


def test_install_writes_the_codex_server_entry(tmp_path):
    install_project(tmp_path, [CODEX])

    assert codex_entry(tmp_path)["command"] == "slop-writer"
    assert codex_entry(tmp_path)["args"] == ["serve", "--mcp"]


def test_the_codex_approval_tables_name_exactly_the_publishing_tools(tmp_path):
    """The gate the distribution carries, on the second client. Compared
    against the roster's own emitter rather than a literal list — that is what
    makes a tool renamed without its rule fail here (adr/0008)."""
    install_project(tmp_path, [CODEX])

    tools = codex_entry(tmp_path)["tools"]
    assert set(tools) == set(CODEX_PUBLISH_TOOLS)
    assert all(table == {"approval_mode": "prompt"} for table in tools.values())


def test_the_codex_gate_is_seeded_on_first_install_only(tmp_path):
    """"Allow and don't ask me again" is a decision Codex records in this very
    file. An upgrade that restored the prompt would overrule a human."""
    install_project(tmp_path, [CODEX])
    path = tmp_path / ".codex" / "config.toml"
    path.write_text(
        path.read_text().replace('approval_mode = "prompt"', 'approval_mode = "auto"', 1)
    )

    result = install_project(tmp_path, [CODEX])

    assert not result.for_client(CODEX).gate_seeded
    modes = [t["approval_mode"] for t in codex_entry(tmp_path)["tools"].values()]
    assert "auto" in modes


def test_a_deleted_codex_approval_table_stays_deleted(tmp_path):
    install_project(tmp_path, [CODEX])
    path = tmp_path / ".codex" / "config.toml"
    dropped = CODEX_PUBLISH_TOOLS[0]
    path.write_text(
        "\n".join(
            line
            for line in path.read_text().splitlines()
            if dropped not in line
        )
    )

    install_project(tmp_path, [CODEX])

    assert dropped not in codex_entry(tmp_path).get("tools", {})


def test_the_codex_server_entry_is_rewritten_on_every_run(tmp_path):
    """The upgrade path, on the half of the file that is ours."""
    install_project(tmp_path, [CODEX])
    path = tmp_path / ".codex" / "config.toml"
    path.write_text(path.read_text().replace('"--mcp"', '"--legacy"'))

    install_project(tmp_path, [CODEX])

    assert codex_entry(tmp_path)["args"] == ["serve", "--mcp"]


def test_install_merges_into_an_existing_codex_config(tmp_path):
    """Other servers and the user's own keys survive, and so does the text
    they wrote around them — the file is rewritten only where it is ours."""
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '# my notes\nmodel = "gpt-5"\n\n'
        "[mcp_servers.other]\n"
        'command = "other-server"\n'
        'args = ["--stdio"]\n'
    )

    install_project(tmp_path, [CODEX])

    config = codex_config(tmp_path)
    assert config["model"] == "gpt-5"
    assert config["mcp_servers"]["other"] == {
        "command": "other-server",
        "args": ["--stdio"],
    }
    assert "# my notes" in path.read_text()


def test_reinstalling_does_not_duplicate_the_codex_section(tmp_path):
    install_project(tmp_path, [CODEX])
    install_project(tmp_path, [CODEX])

    text = (tmp_path / ".codex" / "config.toml").read_text()
    assert text.count(f"[mcp_servers.{SERVER_NAME}]") == 1
    assert text.count("approval_mode") == len(CODEX_PUBLISH_TOOLS)


def test_install_refuses_to_guess_at_corrupt_toml(tmp_path):
    """Same reason as the JSON side: this file can hold every other MCP server
    the human registered, and overwriting it is not undoable from a message."""
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text("[mcp_servers\nbroken = ")
    before = path.read_text()

    with pytest.raises(SlopWriterError) as e:
        install_project(tmp_path, [CODEX])

    assert "not valid TOML" in e.value.message
    assert path.read_text() == before


def test_install_refuses_an_entry_it_cannot_rewrite_in_place(tmp_path):
    """An inline table cannot be replaced by appending a `[table]` header —
    that is a duplicate definition, and Codex would refuse the whole file."""
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(f'[mcp_servers]\n"{SERVER_NAME}" = {{ command = "old" }}\n')

    with pytest.raises(SlopWriterError) as e:
        install_project(tmp_path, [CODEX])
    assert "cannot rewrite" in e.value.message


def test_install_proves_the_rewrite_before_it_writes(tmp_path):
    """Our tables are found by reading `[header]` lines out of the text, and a
    header-shaped line inside a multi-line string is the one case that fools
    it. Deleting something the human wrote is silent and unrecoverable, so the
    result is re-parsed and compared before anything reaches disk."""
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    before = 'notes = """\n[mcp_servers.slop-writer]\nnot a table at all\n"""\n'
    path.write_text(before)

    with pytest.raises(SlopWriterError) as e:
        install_project(tmp_path, [CODEX])

    assert path.read_text() == before
    assert "Nothing was written" in e.value.hint


def test_install_stops_when_codex_config_is_managed(tmp_path, monkeypatch):
    """A managed layer outranks the project layer, so the gate written here
    could be overridden without notice."""
    policy = tmp_path / "managed_config.toml"
    policy.write_text("")
    monkeypatch.setattr("slop_writer.install.MANAGED_CODEX_PATHS", (str(policy),))

    with pytest.raises(SlopWriterError) as e:
        install_project(tmp_path, [CODEX])
    assert "managed" in e.value.message
    assert str(policy) in e.value.message


def test_the_global_codex_config_is_never_written(tmp_path, monkeypatch):
    """It is the human's file by the same rule that makes the permission block
    theirs, and on a real machine it holds other servers' bearer tokens."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    global_config = home / ".codex" / "config.toml"
    global_config.write_text('model = "gpt-5"\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    install_project(tmp_path / "project", [CODEX])

    assert global_config.read_text() == 'model = "gpt-5"\n'


def test_the_codex_report_states_the_trust_requirement(tmp_path):
    """Silent by policy rather than by fault is the one failure the human
    cannot diagnose from anything this command wrote."""
    printed = summarize_install(install_project(tmp_path, [CODEX]))

    assert "trust" in printed


# --------------------------------------------------------------------------
# The cross-agent pair — owned by no client
# --------------------------------------------------------------------------


@pytest.mark.parametrize("clients", [[CLAUDE], [CODEX], [CLAUDE, CODEX]])
def test_the_cross_agent_pair_is_written_whichever_client_was_named(tmp_path, clients):
    """An agent nobody installed can still find the skill — which is the whole
    reason these two belong to no client."""
    install_project(tmp_path, clients)

    assert BLOCK_START in (tmp_path / "AGENTS.md").read_text()
    assert (tmp_path / ".agents" / "skills" / "slop-writer" / "SKILL.md").is_file()


def test_the_cross_agent_block_addresses_the_copy_its_readers_can_open(tmp_path):
    """Each file names the skill directory *its own* readers reach: `CLAUDE.md`
    the one under `.claude/`, `AGENTS.md` the one under `.agents/`."""
    install_project(tmp_path)

    assert ".agents/skills/slop-writer/SKILL.md" in (tmp_path / "AGENTS.md").read_text()
    assert ".claude/skills/slop-writer/SKILL.md" in (tmp_path / "CLAUDE.md").read_text()


@pytest.mark.parametrize("memory", ["CLAUDE.md", "AGENTS.md"])
def test_an_address_block_is_an_address_and_not_an_invariant(tmp_path, memory):
    """#26 measured who needs this: a custom subagent with a restricted
    `tools:` list, which has no skills listing and no `Skill` tool — so the
    block must name a file path. And no invariant is copied in: one fact in
    memory reads as "the knowledge is here" and stops the fetch. `AGENTS.md`
    is stricter still — it is injected into every turn under a size cap."""
    install_project(tmp_path)
    block = (tmp_path / memory).read_text()

    assert f"mcp__{SERVER_NAME}__*" in block
    for invariant in ("MAX(id)", "is_thread_root", "post_metrics", "SELECT"):
        assert invariant not in block


@pytest.mark.parametrize("memory", ["CLAUDE.md", "AGENTS.md"])
def test_an_address_block_keeps_the_text_around_it(tmp_path, memory):
    """Whether the human's text precedes the block or follows it."""
    (tmp_path / memory).write_text("# My project\n\nSome notes.\n")

    install_project(tmp_path)
    (tmp_path / memory).write_text(
        (tmp_path / memory).read_text() + "\nmy own note\n"
    )
    install_project(tmp_path)
    text = (tmp_path / memory).read_text()

    assert text.startswith("# My project\n\nSome notes.\n")
    assert "my own note" in text
    assert text.count(BLOCK_START) == 1


def test_the_cross_agent_skill_is_replaced_wholesale(tmp_path):
    result = install_project(tmp_path)
    skill = tmp_path / ".agents" / "skills" / "slop-writer"
    assert not result.shared_skill_existed

    (skill / "stray.md").write_text("left over from an older version")
    again = install_project(tmp_path)

    assert again.shared_skill_existed
    assert not (skill / "stray.md").exists()


def test_the_old_name_in_skills_lock_is_reported_and_not_edited(tmp_path):
    """#34's second half. The lock is npx's state file with its own hashing
    scheme, so we name the stale entry and leave it — an entry that outlives
    the directory we just deleted is the user's to clear."""
    lock = tmp_path / "skills-lock.json"
    before = json.dumps({"skills": {LEGACY_SKILL_DIR_NAME: {"computedHash": "x"}}})
    lock.write_text(before)
    plant_legacy_skill(tmp_path)

    result = install_project(tmp_path)

    assert LEGACY_SKILL_DIR_NAME in result.skills_lock_names
    assert lock.read_text() == before, "the lock belongs to npx, not to install"
    assert "Remove it yourself" in summarize_install(result)


def test_a_lock_tracking_only_the_current_name_is_the_expected_overlap(tmp_path):
    """Both channels serve the same directory on purpose (#21), so this one is
    a named consequence rather than the upgrade problem #34 is about."""
    (tmp_path / "skills-lock.json").write_text(
        json.dumps({"skills": {"slop-writer": {"computedHash": "x"}}})
    )

    printed = summarize_install(install_project(tmp_path))

    assert "expected, not a failure" in printed
    assert "Remove it yourself" not in printed


def test_the_cross_agent_lock_is_read_and_never_edited(tmp_path):
    """The same boundary, one directory over: another tool's state file with
    its own hashing scheme."""
    lock = tmp_path / ".agents" / "skills-lock.json"
    lock.parent.mkdir(parents=True)
    before = json.dumps({"skills": {"slop-writer": {"computedHash": "x"}}})
    lock.write_text(before)

    result = install_project(tmp_path)

    assert "slop-writer" in result.skills_lock_names
    assert lock.read_text() == before


def test_the_shipped_skill_is_the_one_in_this_checkout():
    """Both install channels serve the same directory (#21). If the wheel and
    `npx skills add` disagreed about *which* directory, a project would end up
    with two copies under different names."""
    assert skill_source().name == "slop-writer"
    assert (skill_source() / "SKILL.md").is_file()


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def test_the_report_prints_a_section_per_client_with_one_shared_tail(tmp_path):
    """User story 14: which artifact landed for which client, rather than one
    merged list. The PATH warning and the next steps are the project's, not a
    client's, so they are printed once."""
    printed = summarize_install(install_project(tmp_path, [CLAUDE, CODEX]))

    assert printed.count(".mcp.json") == 1
    assert f"{CLAUDE} —" in printed and f"{CODEX} —" in printed
    assert printed.count("Next: restart") == 1


def test_the_report_says_first_install_for_one_client_and_not_the_other(tmp_path):
    """User story 15: the report matches what each client actually holds."""
    install_project(tmp_path, [CLAUDE])

    printed = summarize_install(install_project(tmp_path, [CLAUDE, CODEX]))

    assert f"{CLAUDE} — already wired" in printed
    assert f"{CODEX} — first install" in printed


def test_the_report_no_longer_claims_one_verified_client(tmp_path):
    """The claim adr/0008 made false. What replaces it is the two clients this
    command does not write, and why."""
    printed = summarize_install(install_project(tmp_path))

    assert "Verified on Claude Code only" not in printed
    assert "Cursor" in printed and "unverified" in printed
    assert "Codex" not in printed.split("Cursor")[1].split("Next:")[0]


def test_install_reports_a_missing_command_on_path(tmp_path, monkeypatch):
    """The one self-check `install` runs: a `uv tool install` that missed PATH
    otherwise surfaces inside the client as an undiagnosable "server didn't
    start", which no log the user reads explains."""
    monkeypatch.setattr("slop_writer.install.shutil.which", lambda _: None)
    result = install_project(tmp_path, [CLAUDE, CODEX])

    assert not result.on_path
    assert summarize_install(result).count("not on PATH") == 1


# --------------------------------------------------------------------------
# Uninstall
# --------------------------------------------------------------------------


def test_uninstall_removes_what_install_wrote_for_each_client(tmp_path):
    install_project(tmp_path, [CLAUDE, CODEX])

    result = uninstall_project(tmp_path)

    assert SERVER_NAME not in mcp_config(tmp_path)
    assert not (tmp_path / ".codex" / "config.toml").exists()
    assert not (tmp_path / ".claude" / "skills" / "slop-writer").exists()
    assert not (tmp_path / ".agents" / "skills" / "slop-writer").exists()
    for memory in ("CLAUDE.md", "AGENTS.md"):
        text = (tmp_path / memory).read_text() if (tmp_path / memory).is_file() else ""
        assert BLOCK_START not in text and BLOCK_END not in text
    # Per client, because a count over the project would say nothing about
    # which half of it was undone.
    assert len(result.for_client(CLAUDE).removed) == 3
    assert len(result.for_client(CODEX).removed) == 1
    assert len(result.shared_removed) == 2


def test_a_narrowed_uninstall_leaves_the_other_clients_wiring(tmp_path):
    """Dropping one client is not an all-or-nothing act — and the cross-agent
    pair stays, because the client that remains still reads it."""
    install_project(tmp_path, [CLAUDE, CODEX])

    result = uninstall_project(tmp_path, [CODEX])

    assert mcp_config(tmp_path)[SERVER_NAME] == server_entry()
    assert (tmp_path / ".claude" / "skills" / "slop-writer").is_dir()
    assert (tmp_path / ".agents" / "skills" / "slop-writer").is_dir()
    assert BLOCK_START in (tmp_path / "AGENTS.md").read_text()
    assert not (tmp_path / ".codex" / "config.toml").exists()
    assert result.for_client(CLAUDE) is None
    assert result.shared_removed == []


def test_naming_every_client_is_not_a_narrowing(tmp_path):
    """"Unnarrowed" is about what the selection covers, not about how it was
    typed: with no client left to read them, the cross-agent pair goes too."""
    install_project(tmp_path, [CLAUDE, CODEX])

    result = uninstall_project(tmp_path, [CODEX, CLAUDE])

    assert not (tmp_path / ".agents" / "skills" / "slop-writer").exists()
    assert BLOCK_START not in (tmp_path / "AGENTS.md").read_text()
    assert len(result.shared_removed) == 2


def test_uninstall_leaves_the_codex_config_the_user_wrote(tmp_path):
    """Our section out, everything else byte for byte."""
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    before = '# my notes\nmodel = "gpt-5"\n\n[mcp_servers.other]\ncommand = "x"\n'
    path.write_text(before)
    install_project(tmp_path, [CODEX])

    uninstall_project(tmp_path, [CODEX])

    assert path.read_text() == before


def test_uninstall_never_touches_telegram_state(tmp_path):
    """`.tg-analytic/` holds a live session and databases that took hours of
    scraping to build. Uninstalling the wiring is not a reason to lose them."""
    install_project(tmp_path, [CLAUDE, CODEX])
    data = tmp_path / ".tg-analytic"
    data.mkdir()
    (data / "session.session").write_text("credential")

    result = uninstall_project(tmp_path)

    assert (data / "session.session").read_text() == "credential"
    assert any(".tg-analytic" in kept for kept in result.kept)


def test_uninstall_leaves_other_servers_and_the_users_notes(tmp_path):
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"codegraph": {"command": "codegraph"}}})
    )
    (tmp_path / "CLAUDE.md").write_text("# Mine\n")
    install_project(tmp_path)

    uninstall_project(tmp_path)

    assert "codegraph" in mcp_config(tmp_path)
    assert (tmp_path / "CLAUDE.md").read_text().strip() == "# Mine"


def test_uninstall_on_a_project_that_was_never_installed(tmp_path):
    result = uninstall_project(tmp_path)

    assert not any(client.removed for client in result.clients)
    assert result.shared_removed == []
    assert "nothing to remove" in summarize_uninstall(result)
