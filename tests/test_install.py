"""`slop-writer install` / `uninstall` — the agent wiring (#19, #20).

Every test here runs against a `tmp_path` project. The one thing that cannot
be faked is which files a real user already has, so each test starts by
planting the config it is about to defend: another MCP server, a hand-written
permission list, a CLAUDE.md with the human's own notes.
"""

import json

import pytest

from slop_writer.errors import SlopWriterError
from slop_writer.install import (
    BLOCK_END,
    BLOCK_START,
    PUBLISH_TOOLS,
    SERVER_NAME,
    install_project,
    server_entry,
    skill_source,
    uninstall_project,
)


def read(path):
    return json.loads(path.read_text())


def mcp_config(root):
    return read(root / ".mcp.json")["mcpServers"]


def settings(root):
    return read(root / ".claude" / "settings.json")["permissions"]


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
    assert result.permissions_seeded


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

    assert not result.first_install
    assert not result.permissions_seeded
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


def test_first_install_is_detected_from_the_mcp_entry_alone(tmp_path):
    """No marker file: the absence of our key in `.mcp.json` is the signal, so
    there is no second piece of state to drift out of sync (#19)."""
    assert install_project(tmp_path).first_install
    assert not install_project(tmp_path).first_install


def test_install_writes_the_skill_and_replaces_it_next_time(tmp_path):
    result = install_project(tmp_path)
    skill = tmp_path / ".claude" / "skills" / "slop-writer"

    assert (skill / "SKILL.md").is_file()
    assert not result.skill_existed

    # The skill is documentation of the server you have, never the user's
    # config — a local edit is not preserved, and `install` says so (#19).
    (skill / "SKILL.md").write_text("mine now")
    (skill / "stray.md").write_text("left over from an older version")
    again = install_project(tmp_path)

    assert again.skill_existed
    assert (skill / "SKILL.md").read_text() != "mine now"
    assert not (skill / "stray.md").exists()


def test_the_shipped_skill_is_the_one_in_this_checkout():
    """Both install channels serve the same directory (#21). If the wheel and
    `npx skills add` disagreed about *which* directory, a project would end up
    with two copies under different names."""
    assert skill_source().name == "slop-writer"
    assert (skill_source() / "SKILL.md").is_file()


def test_the_address_block_is_an_address_and_not_an_invariant(tmp_path):
    """#26 measured who needs this: a custom subagent with a restricted
    `tools:` list, which has no skills listing and no `Skill` tool — so the
    block must name a file path. And no invariant is copied in: one fact in
    memory reads as "the knowledge is here" and stops the fetch."""
    install_project(tmp_path)
    block = (tmp_path / "CLAUDE.md").read_text()

    assert ".claude/skills/slop-writer/SKILL.md" in block
    assert f"mcp__{SERVER_NAME}__*" in block
    for invariant in ("MAX(id)", "is_thread_root", "post_metrics", "SELECT"):
        assert invariant not in block


def test_the_address_block_appends_to_an_existing_memory_file(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# My project\n\nSome notes.\n")

    install_project(tmp_path)
    text = (tmp_path / "CLAUDE.md").read_text()

    assert text.startswith("# My project\n\nSome notes.\n")
    assert text.count(BLOCK_START) == 1


def test_the_address_block_is_written_once(tmp_path):
    install_project(tmp_path)
    (tmp_path / "CLAUDE.md").write_text(
        (tmp_path / "CLAUDE.md").read_text() + "\nmy own note\n"
    )

    install_project(tmp_path)
    text = (tmp_path / "CLAUDE.md").read_text()

    assert text.count(BLOCK_START) == 1
    assert "my own note" in text


def test_install_refuses_to_guess_at_corrupt_json(tmp_path):
    """Starting from `{}` would drop every other server in the file."""
    (tmp_path / ".mcp.json").write_text("{ not json")

    with pytest.raises(SlopWriterError) as e:
        install_project(tmp_path)
    assert "not valid JSON" in e.value.message


def test_install_reports_a_missing_command_on_path(tmp_path, monkeypatch):
    """The one self-check `install` runs: a `uv tool install` that missed PATH
    otherwise surfaces inside the client as an undiagnosable "server didn't
    start", which no log the user reads explains."""
    monkeypatch.setattr("slop_writer.install.shutil.which", lambda _: None)
    assert not install_project(tmp_path).on_path


def test_install_stops_when_mcp_config_is_enterprise_managed(tmp_path, monkeypatch):
    """#11: a `managed-mcp.json` takes exclusive control and `add` fails
    outright. Report, never retry."""
    policy = tmp_path / "managed-mcp.json"
    policy.write_text("{}")
    monkeypatch.setattr("slop_writer.install.MANAGED_MCP_PATHS", (str(policy),))

    with pytest.raises(SlopWriterError) as e:
        install_project(tmp_path)
    assert "managed" in e.value.message


def test_uninstall_removes_what_install_wrote(tmp_path):
    install_project(tmp_path)
    result = uninstall_project(tmp_path)

    assert SERVER_NAME not in mcp_config(tmp_path)
    assert not (tmp_path / ".claude" / "skills" / "slop-writer").exists()
    assert BLOCK_START not in (tmp_path / "CLAUDE.md").read_text()
    assert BLOCK_END not in (tmp_path / "CLAUDE.md").read_text()
    assert len(result.removed) == 3


def test_uninstall_never_touches_telegram_state(tmp_path):
    """`.tg-analytic/` holds a live session and databases that took hours of
    scraping to build. Uninstalling the wiring is not a reason to lose them."""
    install_project(tmp_path)
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
    assert uninstall_project(tmp_path).removed == []
