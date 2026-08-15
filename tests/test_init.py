"""`slop-writer init` — the Telegram state (#19, #20).

The login itself needs a TTY and a real account, so it stays the acceptance
step. Everything either side of it — what `init` finds, what it preserves,
what it refuses — is exercised here against a `tmp_path`, with no session, no
`.tg-analytic/` of the developer's own, and no network.
"""

import pytest

from slop_writer.errors import SlopWriterError
from slop_writer.init import (
    ENV_KEYS,
    describe_account,
    ensure_gitignored,
    inspect_project,
    logout_session,
    read_env,
    require_credentials,
    write_env,
)

GOOD = {
    "TG_API_ID": "123456",
    "TG_API_HASH": "abcdefabcdefabcdefabcdefabcdef12",
    "TG_PHONE": "+15551234567",
}


class FakeUser:
    def __init__(self, first_name=None, last_name=None, username=None, phone=None):
        self.first_name = first_name
        self.last_name = last_name
        self.username = username
        self.phone = phone


def test_inspect_reports_before_anything_is_written(tmp_path):
    """`init` prints the state first: someone who mistyped `TG_API_ID` last
    week needs to see it before being asked anything."""
    state = inspect_project(tmp_path)

    assert state.values == {}
    assert state.missing == ENV_KEYS
    assert not state.session_exists
    assert not (tmp_path / ".tg-analytic").exists()


def test_only_the_missing_keys_are_reported_missing(tmp_path):
    write_env(tmp_path, {"TG_API_ID": "1", "TG_API_HASH": "h"})
    assert inspect_project(tmp_path).missing == ("TG_PHONE",)


def test_a_non_numeric_api_id_counts_as_missing(tmp_path):
    """Telethon reports it as an opaque `ApiIdInvalidError` at connect time —
    hours later, from a tool call. Catching the shape here is the difference
    between a prompt and a mystery."""
    write_env(tmp_path, {**GOOD, "TG_API_ID": "not-a-number"})
    assert inspect_project(tmp_path).missing == ("TG_API_ID",)


def test_a_blank_value_counts_as_missing(tmp_path):
    write_env(tmp_path, {**GOOD, "TG_PHONE": ""})
    assert "TG_PHONE" in inspect_project(tmp_path).missing


def test_write_env_preserves_keys_this_project_does_not_own(tmp_path):
    """Additive, never destructive — the whole reason "just run init again" is
    the correct reflex."""
    write_env(tmp_path, {**GOOD, "SOMETHING_ELSE": "keep me"})
    write_env(tmp_path, {"TG_PHONE": "+440000000000"})

    values = read_env(tmp_path)
    assert values["SOMETHING_ELSE"] == "keep me"
    assert values["TG_API_HASH"] == GOOD["TG_API_HASH"]
    assert values["TG_PHONE"] == "+440000000000"


def test_the_env_file_never_grows_a_duplicate_key(tmp_path):
    """A `.env` with two `TG_API_ID` lines is legal and which one wins depends
    on the reader — an auth failure that surfaces much later."""
    write_env(tmp_path, GOOD)
    write_env(tmp_path, {"TG_API_ID": "999"})

    lines = [
        line for line in (tmp_path / ".tg-analytic" / ".env").read_text().splitlines()
        if line.startswith("TG_API_ID=")
    ]
    assert lines == ["TG_API_ID=999"]


def test_quoted_and_commented_env_lines_are_read(tmp_path):
    path = tmp_path / ".tg-analytic"
    path.mkdir()
    (path / ".env").write_text(
        '# a comment\nTG_API_ID="123"\n\nTG_PHONE=\'+15551234567\'\n'
    )
    values = read_env(tmp_path)

    assert values["TG_API_ID"] == "123"
    assert values["TG_PHONE"] == "+15551234567"


def test_the_session_directory_is_gitignored_without_asking(tmp_path):
    """`session.session` is a live login to the user's account. Asking
    permission to keep a credential out of version control is theatre."""
    (tmp_path / ".git").mkdir()

    assert ensure_gitignored(tmp_path)
    assert ".tg-analytic/" in (tmp_path / ".gitignore").read_text()
    # Idempotent: a second run adds nothing.
    assert not ensure_gitignored(tmp_path)


def test_gitignore_is_appended_to_not_replaced(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("*.pyc\n")

    ensure_gitignored(tmp_path)

    assert (tmp_path / ".gitignore").read_text() == "*.pyc\n.tg-analytic/\n"


def test_no_gitignore_outside_a_git_repo(tmp_path):
    assert not ensure_gitignored(tmp_path)
    assert not (tmp_path / ".gitignore").exists()


def test_incomplete_credentials_raise_the_contract_code(tmp_path):
    with pytest.raises(SlopWriterError) as e:
        require_credentials({"TG_API_ID": "1"})
    assert e.value.code == "NO_CREDENTIALS"
    assert "my.telegram.org" in e.value.hint


def test_relogin_drops_the_session_file(tmp_path):
    """Deleting rather than calling `log_out`: the common reason to re-login is
    a session revoked from Telegram's device list, where the server-side call
    fails and leaves behind the exact dead file this is meant to clear."""
    session = tmp_path / ".tg-analytic" / "session.session"
    session.parent.mkdir(parents=True)
    session.write_text("stale")

    assert logout_session(tmp_path)
    assert not session.exists()
    assert not logout_session(tmp_path)


def test_the_account_is_described_by_every_identifier_it_has():
    """People have several Telegram accounts, and logging in as the wrong one
    is otherwise found days later through inexplicably empty result sets."""
    described = describe_account(
        FakeUser(first_name="Nikita", last_name="P", username="lancetnik", phone="1555")
    )
    assert described == "Nikita P / @lancetnik / +1555"


def test_an_account_with_no_username_still_describes_something():
    assert describe_account(FakeUser(first_name="Nikita")) == "Nikita"
    assert describe_account(FakeUser()) == "unknown account"
