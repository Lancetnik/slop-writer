"""The subscriber audit and the ban that acts on it.

The scoring functions are exercised as functions over plain profile dicts;
`audit_subscribers_with_client` and `ban_subscribers_with_client` over the
fake client. The fixture accounts are shaped after the 2026-09 farm that
prompted the audit — the cases worth pinning are the ones that were real.
"""

import sqlite3
from datetime import timedelta

import pytest
from telethon.errors import ChatAdminRequiredError, UserAdminInvalidError

from slop_writer.audit import (
    GENERATED_USERNAME,
    NEW_ACCOUNT_ID,
    audit_subscribers_with_client,
    online_clusters,
    score_accounts,
)
from slop_writer.db import db_path_for
from slop_writer.errors import SlopWriterError
from slop_writer.publish import MAX_BAN, ban_subscribers_with_client, prepare_ban

from .conftest import run
from .factories import (
    DATE,
    AdminLogEvent,
    AdminLogPage,
    FakeClient,
    Named,
    channel,
    user,
)

CHANNEL = "chan"
FARM = NEW_ACCOUNT_ID + 776362757


def profile(uid, **kw):
    base = {
        "user_id": uid, "name": "x", "username": None, "has_photo": True,
        "premium": False, "deleted": False, "flagged": None,
        "status": "recently", "was_online": None,
    }
    return {**base, **kw}


def farm_profile(uid, minute=0):
    return profile(
        uid, username=f"name_viktoriya_u{uid % 100}z",
        status="offline",
        was_online=(DATE + timedelta(minutes=minute)).isoformat(),
    )


def verdicts(profiles):
    return {a["user_id"]: a["verdict"] for a in score_accounts(profiles)}


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def test_a_farm_that_came_online_together_is_flagged_and_its_neighbours_are_not():
    farm = [farm_profile(FARM + i, minute=i) for i in range(3)]
    real = [profile(199592881, premium=True), profile(470189407)]
    got = verdicts(farm + real)
    assert [got[p["user_id"]] for p in farm] == ["likely"] * 3
    assert got[199592881] == got[470189407] == "clean"


def test_two_accounts_online_together_are_not_yet_a_cluster():
    two = [farm_profile(FARM + i) for i in range(2)]
    assert online_clusters(two) == set()
    assert len(online_clusters(two + [farm_profile(FARM + 9, minute=4)])) == 3


def test_a_hidden_last_seen_never_joins_a_cluster():
    """"Recently" carries no instant, so three real accounts with privacy on
    are three unknowns, not a burst."""
    assert online_clusters([profile(i) for i in range(1, 4)]) == set()


def test_a_deleted_account_is_unknowable_rather_than_clean():
    [a] = score_accounts([profile(FARM, deleted=True, has_photo=False)])
    assert (a["verdict"], a["signals"], a["score"]) == ("deleted", [], 0)


def test_telegrams_own_scam_flag_is_enough_on_its_own():
    [a] = score_accounts([profile(1, flagged="scam")])
    assert a["verdict"] == "likely"


def test_a_single_weak_signal_stays_clean():
    """Most real subscribers show one — a missing photo, an open last-seen."""
    [a] = score_accounts([profile(1, has_photo=False)])
    assert a["verdict"] == "clean"


@pytest.mark.parametrize("name", [
    "oname_5c9dv", "antonova_mila_t364", "name_viktoriya_u36z",
    "yuliyaname_ukus3", "itsninawc_t7",
])
def test_the_farm_usernames_read_as_generated(name):
    assert GENERATED_USERNAME.search(name)


@pytest.mark.parametrize("name", [
    "ivan_dev", "lucky_str1ker", "serg_ze", "themishkun", "itsannawy_mv",
])
def test_ordinary_usernames_do_not(name):
    assert not GENERATED_USERNAME.search(name)


# --------------------------------------------------------------------------
# The audit
# --------------------------------------------------------------------------


def join(ev_id, uid, minutes=0):
    return AdminLogEvent(
        ev_id, Named("ChannelAdminLogEventActionParticipantJoin"),
        user_id=uid, date=DATE + timedelta(minutes=minutes),
    )


def leave(ev_id, uid, minutes=0):
    return AdminLogEvent(
        ev_id, Named("ChannelAdminLogEventActionParticipantLeave"),
        user_id=uid, date=DATE + timedelta(minutes=minutes),
    )


def farm_user(uid, minute=0):
    return user(uid, first="Анна🖐", username=f"itsanna_t{uid % 10}",
                photo=True, was_online=DATE + timedelta(hours=5, minutes=minute))


def audited_client(extra_users=(), extra_events=()):
    farm = [farm_user(FARM + i, minute=i) for i in range(3)]
    real = user(199592881, first="Mikhail", photo=True, recently=True,
                premium=True)
    events = [join(10 + i, u.id, i) for i, u in enumerate(farm)]
    events += [join(20, real.id, 5), *extra_events]
    # The log is newest-first, like Telegram's.
    events.sort(key=lambda e: e.date, reverse=True)
    return FakeClient(admin_log=[
        AdminLogPage(events, [*farm, real, *extra_users])
    ])


def audit(client, root):
    return run(audit_subscribers_with_client(client, channel(), CHANNEL, root))


def db_rows(root, sql):
    conn = sqlite3.connect(db_path_for(root, CHANNEL))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_the_audit_ranks_the_farm_above_the_subscriber(tmp_path):
    result = audit(audited_client(), tmp_path)
    assert [a["verdict"] for a in result.accounts] == ["likely"] * 3 + ["clean"]
    assert result.overview["joins"] == 4


def test_an_account_that_joined_and_left_is_churn_not_a_suspect(tmp_path):
    goner = user(FARM + 50, username="gone_x1")
    client = audited_client(
        extra_users=[goner],
        extra_events=[join(30, goner.id, 1), leave(31, goner.id, 2)],
    )
    result = audit(client, tmp_path)
    assert goner.id not in {a["user_id"] for a in result.accounts}
    assert result.overview["left_again"] == 1


def test_events_and_a_profile_snapshot_per_joiner_are_stored(tmp_path):
    audit(audited_client(), tmp_path)
    audit(audited_client(), tmp_path)
    # Events upsert by (id, user_id); profiles append one snapshot per run.
    assert db_rows(tmp_path, "SELECT COUNT(*) FROM subscriber_events") == [(4,)]
    assert db_rows(tmp_path, "SELECT COUNT(*) FROM subscriber_profiles") == [(8,)]
    [(signals,)] = db_rows(
        tmp_path,
        f"SELECT signals FROM subscriber_profiles WHERE user_id = {FARM} "
        "ORDER BY id DESC LIMIT 1",
    )
    assert "online_cluster" in signals.split(",")


def test_no_admin_rights_is_a_refusal_and_leaves_no_database(tmp_path):
    """The log is the audit's only source: an empty result would read as
    "nobody joined", which is the one wrong answer worth refusing."""
    client = FakeClient(admin_log_error=ChatAdminRequiredError(request=None))
    with pytest.raises(SlopWriterError) as exc:
        audit(client, tmp_path)
    assert exc.value.code == "NOT_ADMIN"
    assert not db_path_for(tmp_path, CHANNEL).exists()


# --------------------------------------------------------------------------
# Banning: validation against the audit, then per-account outcomes
# --------------------------------------------------------------------------


@pytest.fixture
def audited(tmp_path):
    audit(audited_client(), tmp_path)
    return tmp_path


def test_only_an_audited_account_can_be_banned(audited):
    with pytest.raises(SlopWriterError) as exc:
        prepare_ban(CHANNEL, [FARM, 12345], audited)
    assert exc.value.code == "INVALID_ARGUMENT"
    assert "12345" in exc.value.message


def test_a_channel_never_audited_bans_nobody(tmp_path):
    with pytest.raises(SlopWriterError) as exc:
        prepare_ban(CHANNEL, [FARM], tmp_path)
    assert exc.value.code == "INVALID_ARGUMENT"
    assert not db_path_for(tmp_path, CHANNEL).exists()


def test_an_empty_or_oversized_request_is_refused(audited):
    for ids in ([], list(range(MAX_BAN + 1))):
        with pytest.raises(SlopWriterError) as exc:
            prepare_ban(CHANNEL, ids, audited)
        assert exc.value.code == "INVALID_ARGUMENT"


def test_a_repeated_id_is_banned_once(audited):
    accounts = prepare_ban(CHANNEL, [FARM, FARM, FARM + 1], audited)
    assert [a["user_id"] for a in accounts] == [FARM, FARM + 1]
    assert accounts[0]["score"] > 0


def test_banning_bans_every_account_asked_for(audited):
    accounts = prepare_ban(CHANNEL, [FARM, FARM + 1], audited)
    client = FakeClient(known_users={FARM, FARM + 1})
    result = run(ban_subscribers_with_client(client, channel(), CHANNEL, accounts, audited))
    assert client.bans == [FARM, FARM + 1]
    assert [a["user_id"] for a in result.banned] == [FARM, FARM + 1]
    assert result.failed == []


def test_a_cold_session_cache_is_refilled_from_the_admin_log(audited):
    accounts = prepare_ban(CHANNEL, [FARM], audited)
    client = FakeClient(admin_log=[AdminLogPage([join(10, FARM)], [farm_user(FARM)])])
    result = run(ban_subscribers_with_client(client, channel(), CHANNEL, accounts, audited))
    assert client.bans == [FARM]
    assert result.failed == []


def test_an_account_that_can_no_longer_be_resolved_is_reported_not_raised(audited):
    accounts = prepare_ban(CHANNEL, [FARM, FARM + 1], audited)
    client = FakeClient(known_users={FARM})
    result = run(ban_subscribers_with_client(client, channel(), CHANNEL, accounts, audited))
    assert [a["user_id"] for a in result.banned] == [FARM]
    assert [a["user_id"] for a in result.failed] == [FARM + 1]


def test_one_refused_account_does_not_stop_the_rest(audited):
    """adr/0007's rule: an account Telegram refuses is that account's answer."""
    accounts = prepare_ban(CHANNEL, [FARM, FARM + 1], audited)
    client = FakeClient(
        known_users={FARM, FARM + 1},
        ban_errors={FARM: UserAdminInvalidError(request=None)},
    )
    result = run(ban_subscribers_with_client(client, channel(), CHANNEL, accounts, audited))
    assert client.bans == [FARM + 1]
    assert [a["user_id"] for a in result.failed] == [FARM]


def test_no_ban_rights_is_the_call_refusing(audited):
    accounts = prepare_ban(CHANNEL, [FARM], audited)
    client = FakeClient(
        known_users={FARM},
        ban_errors={FARM: ChatAdminRequiredError(request=None)},
    )
    with pytest.raises(SlopWriterError) as exc:
        run(ban_subscribers_with_client(client, channel(), CHANNEL, accounts, audited))
    assert exc.value.code == "NOT_ADMIN"


# Historical moderation remains a reference after the log expires.
def seed_removal(root, uid, when=DATE):
    from slop_writer.db import open_db
    from slop_writer.audit import record_ban
    conn = open_db(root, CHANNEL)
    record_ban(conn, uid, when.isoformat(), "tool")
    conn.close()


def test_one_joiner_matches_a_refreshed_previous_removal(tmp_path):
    old = farm_user(FARM)
    newcomer = user(FARM + 80, first="Оля", username="realolya_ajre",
                    photo=True, was_online=DATE + timedelta(hours=5, seconds=4))
    seed_removal(tmp_path, old.id)
    client = FakeClient(entities={old.id: old}, admin_log=[
        AdminLogPage([join(40, newcomer.id)], [newcomer])
    ])
    result = audit(client, tmp_path)
    [account] = result.accounts
    assert account["verdict"] == "likely"
    assert account["matched_banned_ids"] == [old.id]
    assert "known_pool_activity" in account["signals"]
    assert old.id in client.entity_calls
    assert result.overview["reference_count"] == 1


def test_removed_joiner_is_reported_and_cannot_match_itself(tmp_path):
    u = farm_user(FARM)
    removed = AdminLogEvent(41, Named(
        "ChannelAdminLogEventActionParticipantToggleBan",
        new_participant=Named("ChannelParticipantBanned", left=True, peer=Named("PeerUser", user_id=u.id),
                              banned_rights=Named("ChatBannedRights", view_messages=True)),
    ), user_id=1, date=DATE + timedelta(minutes=2))
    result = audit(FakeClient(admin_log=[AdminLogPage([removed, join(40, u.id)], [u])]), tmp_path)
    assert result.accounts == []
    [account] = result.overview["removed_accounts"]
    assert account["user_id"] == u.id
    assert account["matched_banned_ids"] == []
    assert db_rows(tmp_path, "SELECT user_id, source FROM subscriber_bans") == [(u.id, "admin_log")]


def test_history_backfills_removals_from_an_older_database(tmp_path):
    from slop_writer.db import open_db
    conn = open_db(tmp_path, CHANNEL)
    conn.execute("INSERT INTO subscriber_events VALUES (?, ?, 'leave', 'removed', ?)",
                 (999, DATE.isoformat(), FARM))
    conn.commit()
    conn.close()
    result = audit(FakeClient(entities={FARM: farm_user(FARM)}), tmp_path)
    assert result.overview["reference_count"] == 1
    assert db_rows(tmp_path, "SELECT user_id FROM subscriber_bans") == [(FARM,)]


def test_failed_refresh_keeps_saved_reference_and_reports_it(tmp_path):
    import json
    from slop_writer.db import open_db
    seed_removal(tmp_path, FARM)
    conn = open_db(tmp_path, CHANNEL)
    conn.execute("UPDATE subscriber_bans SET profile_json=?", (json.dumps(farm_profile(FARM)),))
    conn.commit()
    conn.close()
    newcomer = user(FARM + 80, photo=True, was_online=DATE + timedelta(seconds=4))
    result = audit(FakeClient(admin_log=[AdminLogPage([join(40, newcomer.id)], [newcomer])]), tmp_path)
    assert result.overview["unavailable_references"] == [FARM]
    assert result.accounts[0]["matched_banned_ids"] == [FARM]


def test_partial_ban_records_only_successful_accounts(audited):
    accounts = prepare_ban(CHANNEL, [FARM, FARM + 1], audited)
    client = FakeClient(known_users={FARM, FARM + 1},
                        ban_errors={FARM: UserAdminInvalidError(request=None)})
    run(ban_subscribers_with_client(client, channel(), CHANNEL, accounts, audited))
    assert db_rows(audited, "SELECT user_id, source FROM subscriber_bans") == [(FARM + 1, "tool")]


def test_success_before_flood_wait_is_still_recorded(audited):
    from telethon.errors import FloodWaitError
    accounts = prepare_ban(CHANNEL, [FARM, FARM + 1], audited)
    client = FakeClient(known_users={FARM, FARM + 1},
                        ban_errors={FARM + 1: FloodWaitError(request=None, capture=60)})
    with pytest.raises(FloodWaitError):
        run(ban_subscribers_with_client(client, channel(), CHANNEL, accounts, audited))
    assert db_rows(audited, "SELECT user_id FROM subscriber_bans") == [(FARM,)]


def test_reference_does_not_add_points_without_matching_activity():
    [account] = score_accounts([profile(FARM + 80)], [farm_profile(FARM)])
    assert account["score"] == 2
    assert account["matched_banned_ids"] == []


def test_current_cluster_and_reference_do_not_double_count_activity():
    profiles = [farm_profile(FARM + i) for i in range(3)]
    [account, *_] = score_accounts(profiles, [farm_profile(FARM + 80)])
    assert account["matched_banned_ids"] == [FARM + 80]
    assert "online_cluster" in account["signals"]
    assert "known_pool_activity" not in account["signals"]



def test_removal_references_are_scoped_to_the_channel(tmp_path):
    from slop_writer.db import open_db
    from slop_writer.audit import record_ban
    conn = open_db(tmp_path, "other")
    record_ban(conn, FARM, DATE.isoformat(), "tool")
    conn.close()
    result = audit(audited_client(), tmp_path)
    assert result.overview["reference_count"] == 0


def test_renderer_includes_removed_accounts_and_match_ids():
    from slop_writer.render import summarize_audit
    [removed] = score_accounts(
        [{**farm_profile(FARM), "joined": DATE.isoformat()}],
        [farm_profile(FARM + 1)],
    )
    summary = summarize_audit(CHANNEL, {
        "removed_accounts": [removed], "reference_count": 1,
    }, [])
    assert "Already removed" in summary
    assert str(FARM) in summary
    assert f"matches {FARM + 1}" in summary
