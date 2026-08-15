"""Discussion-group classification and thread linkage.

All of it is duck-typed over Telethon objects — dispatch on
`type(action).__name__`, everything else through `getattr` — which is what
makes plain stand-ins sufficient for the admin-log half (see
`factories.Named`). The three-way actor split on `MessageActionChatDeleteUser`
came from live data and is pinned here: calling a no-actor removal either
'self' or 'removed' silently skews churn.
"""

from datetime import timedelta

import pytest
from telethon.tl.types import (
    MessageActionChatAddUser,
    MessageActionChatDeleteUser,
    MessageActionChatJoinedByLink,
    MessageActionChatJoinedByRequest,
    MessageActionPinMessage,
)

from slop_writer.group import (
    auto_forward_post_id,
    classify_admin_log_event,
    classify_service_message,
    message_row,
    thread_post_id_for,
    unresolved_root_refs,
    upsert_group_events,
)

from .factories import DATE, AdminLogEvent, Named, msg, photo, service

CHANNEL = 1001


def kinds(events):
    return [(e.kind, e.via, e.user_id) for e in events]


# --------------------------------------------------------------------------
# Service messages
# --------------------------------------------------------------------------


def test_join_by_link():
    ev = classify_service_message(
        service(5, MessageActionChatJoinedByLink(inviter_id=99), sender_id=42)
    )
    assert kinds(ev) == [("join", "link", 42)]
    assert ev[0].id == 5
    assert ev[0].date == DATE.isoformat()


def test_join_by_request():
    ev = classify_service_message(
        service(6, MessageActionChatJoinedByRequest(), sender_id=42)
    )
    assert kinds(ev) == [("join", "request", 42)]


def test_one_add_user_message_can_carry_several_users():
    """This is why `group_events`' primary key is (id, user_id)."""
    ev = classify_service_message(
        service(7, MessageActionChatAddUser(users=[1, 2, 3]), sender_id=1)
    )
    assert kinds(ev) == [
        ("join", "added", 1),
        ("join", "added", 2),
        ("join", "added", 3),
    ]
    assert {e.id for e in ev} == {7}


def test_a_self_join_via_the_button_looks_like_adding_yourself():
    ev = classify_service_message(
        service(8, MessageActionChatAddUser(users=[42]), sender_id=42)
    )
    assert kinds(ev) == [("join", "added", 42)]


def test_leave_by_the_member_themselves():
    ev = classify_service_message(
        service(9, MessageActionChatDeleteUser(user_id=42), sender_id=42)
    )
    assert kinds(ev) == [("leave", "self", 42)]


def test_leave_at_someone_elses_hand_is_a_removal():
    ev = classify_service_message(
        service(10, MessageActionChatDeleteUser(user_id=42), sender_id=7)
    )
    assert kinds(ev) == [("leave", "removed", 42)]


def test_a_removal_with_no_actor_is_neither():
    """Telegram auto-removing a deleted account leaves no sender. Bucketing
    it as 'self' or 'removed' would skew churn either way, so it gets its
    own value."""
    ev = classify_service_message(
        service(11, MessageActionChatDeleteUser(user_id=42), sender_id=None)
    )
    assert kinds(ev) == [("leave", "unknown", 42)]


def test_non_membership_service_messages_yield_nothing():
    assert classify_service_message(service(12, MessageActionPinMessage())) == []


def test_a_message_without_an_action_yields_nothing():
    assert classify_service_message(msg(13, text="ordinary")) == []


# --------------------------------------------------------------------------
# Admin log
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action_name, expected",
    [
        ("ChannelAdminLogEventActionParticipantJoin", ("join", "added")),
        ("ChannelAdminLogEventActionParticipantJoinByInvite", ("join", "link")),
        ("ChannelAdminLogEventActionParticipantJoinByRequest", ("join", "request")),
        ("ChannelAdminLogEventActionParticipantLeave", ("leave", "self")),
    ],
)
def test_actor_shaped_admin_events(action_name, expected):
    ev = classify_admin_log_event(
        AdminLogEvent(9_000_001, Named(action_name), user_id=42)
    )
    assert kinds(ev) == [(*expected, 42)]
    assert ev[0].id == 9_000_001


def test_an_invite_credits_the_invited_participant_not_the_inviter():
    action = Named(
        "ChannelAdminLogEventActionParticipantInvite",
        participant=Named("ChannelParticipant", user_id=42),
    )
    ev = classify_admin_log_event(AdminLogEvent(9_000_002, action, user_id=7))
    assert kinds(ev) == [("join", "added", 42)]


def test_an_invite_without_a_resolvable_participant_is_dropped():
    action = Named("ChannelAdminLogEventActionParticipantInvite", participant=None)
    assert classify_admin_log_event(AdminLogEvent(9_000_003, action)) == []


def test_a_ban_that_ejects_the_member_is_a_leave():
    action = Named(
        "ChannelAdminLogEventActionParticipantToggleBan",
        new_participant=Named(
            "ChannelParticipantBanned", left=True, peer=Named("PeerUser", user_id=42)
        ),
    )
    ev = classify_admin_log_event(AdminLogEvent(9_000_004, action))
    assert kinds(ev) == [("leave", "removed", 42)]


def test_a_rights_only_restriction_is_not_a_leave():
    """The member is still in the group; counting it as churn would be wrong."""
    action = Named(
        "ChannelAdminLogEventActionParticipantToggleBan",
        new_participant=Named(
            "ChannelParticipantBanned", left=False, peer=Named("PeerUser", user_id=42)
        ),
    )
    assert classify_admin_log_event(AdminLogEvent(9_000_005, action)) == []


def test_unrelated_admin_events_yield_nothing():
    assert classify_admin_log_event(
        AdminLogEvent(9_000_006, Named("ChannelAdminLogEventActionChangeTitle"))
    ) == []
    assert classify_admin_log_event(AdminLogEvent(9_000_007, None)) == []


# --------------------------------------------------------------------------
# Thread roots
# --------------------------------------------------------------------------


def test_an_auto_forward_of_our_channel_is_a_root():
    m = msg(100, fwd_channel_post=42, fwd_channel_id=CHANNEL)
    assert auto_forward_post_id(m, CHANNEL) == 42


def test_a_forward_of_someone_elses_channel_is_not_a_root():
    m = msg(100, fwd_channel_post=42, fwd_channel_id=9999)
    assert auto_forward_post_id(m, CHANNEL) is None


def test_a_standalone_group_has_no_roots_at_all():
    m = msg(100, fwd_channel_post=42, fwd_channel_id=CHANNEL)
    assert auto_forward_post_id(m, None) is None


def test_a_plain_message_is_not_a_root():
    assert auto_forward_post_id(msg(100, text="hi"), CHANNEL) is None


def test_a_forward_without_a_channel_post_is_not_a_root():
    m = msg(100, fwd_channel_id=CHANNEL)
    assert auto_forward_post_id(m, CHANNEL) is None


# --------------------------------------------------------------------------
# Thread linkage
# --------------------------------------------------------------------------


ROOTS = {200: 42}  # group-side root id -> channel post id


def test_a_direct_comment_carries_the_head_in_reply_to_msg_id():
    assert thread_post_id_for(msg(201, reply_to_msg_id=200), ROOTS) == 42


def test_a_nested_reply_carries_the_head_in_top_id():
    """reply_to_msg_id points at the sibling comment; only top_id names the
    thread."""
    m = msg(202, reply_to_msg_id=201, reply_to_top_id=200)
    assert thread_post_id_for(m, ROOTS) == 42


def test_top_level_chatter_belongs_to_no_thread():
    assert thread_post_id_for(msg(203, text="hello"), ROOTS) is None


def test_a_reply_under_an_unknown_head_is_unresolved_not_wrong():
    assert thread_post_id_for(msg(204, reply_to_msg_id=999), ROOTS) is None


def test_unresolved_heads_are_collected_for_a_targeted_fetch():
    msgs = [
        msg(201, reply_to_msg_id=200),   # known root
        msg(205, reply_to_msg_id=999),   # out-of-window root
        msg(206, reply_to_top_id=888),   # out-of-window root, nested
        msg(207, text="chatter"),        # no reply at all
    ]
    assert unresolved_root_refs(msgs, ROOTS) == {999, 888}


def test_nothing_unresolved_when_every_head_is_known():
    assert unresolved_root_refs([msg(201, reply_to_msg_id=200)], ROOTS) == set()


# --------------------------------------------------------------------------
# Row shapes
# --------------------------------------------------------------------------


def test_message_row_flattens_a_comment():
    m = msg(
        301,
        text="nice post",
        reply_to_msg_id=200,
        reactions=3,
        stars=7,
        sender_id=42,
        media=photo(),
    )
    row = message_row(m, thread_post=99, is_root=False)
    assert row[0] == 301
    assert row[1] == DATE.isoformat()
    assert row[2] == "nice post"
    assert row[6] == 200
    assert row[7] == 99
    assert row[8] == 0
    # Paid reactions are folded into the same total the schema stores.
    assert row[9] == 10
    assert row[10] == "photo"


def test_message_row_marks_a_root():
    row = message_row(msg(302, text=""), thread_post=99, is_root=True)
    assert row[8] == 1


# --------------------------------------------------------------------------
# Event writes
# --------------------------------------------------------------------------


def test_events_with_a_known_user_upsert_on_rescan(conn):
    from slop_writer.group import GroupEvent

    events = [GroupEvent(1, DATE.isoformat(), "join", "link", 42)]
    upsert_group_events(conn, events, {42: ("Ann", "ann")})
    upsert_group_events(conn, events, {42: ("Ann Renamed", "ann2")})

    rows = conn.execute(
        "SELECT id, kind, via, user_id, user_name, user_username, author"
        " FROM group_events"
    ).fetchall()
    assert rows == [(1, "join", "link", 42, "Ann Renamed", "ann2", "ann2")]


def test_events_with_an_unknown_user_do_not_pile_up(conn):
    """SQLite treats NULLs in a composite PK as distinct, so ON CONFLICT
    cannot dedupe these — the writer pre-deletes them instead. Without that,
    every re-scan would add another copy of the same event."""
    from slop_writer.group import GroupEvent

    events = [GroupEvent(2, DATE.isoformat(), "leave", "unknown", None)]
    upsert_group_events(conn, events, {})
    upsert_group_events(conn, events, {})
    upsert_group_events(conn, events, {})

    assert conn.execute(
        "SELECT COUNT(*) FROM group_events WHERE id = 2"
    ).fetchone()[0] == 1


def test_the_author_column_falls_back_through_username_name_then_id(conn):
    from slop_writer.group import GroupEvent

    upsert_group_events(
        conn,
        [
            GroupEvent(10, DATE.isoformat(), "join", "link", 1),
            GroupEvent(11, DATE.isoformat(), "join", "link", 2),
            GroupEvent(12, DATE.isoformat(), "join", "link", 3),
        ],
        {1: ("Ann", "ann"), 2: ("Bob", None)},
    )
    authors = dict(conn.execute("SELECT id, author FROM group_events"))
    assert authors == {10: "ann", 11: "Bob", 12: "3"}


def test_add_user_events_sharing_an_id_all_land(conn):
    from slop_writer.group import GroupEvent

    date = (DATE + timedelta(seconds=1)).isoformat()
    upsert_group_events(
        conn,
        [GroupEvent(20, date, "join", "added", uid) for uid in (1, 2, 3)],
        {},
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM group_events WHERE id = 20"
    ).fetchone()[0] == 3
