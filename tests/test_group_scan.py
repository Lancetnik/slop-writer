"""The group scan end to end: target resolution, window bookkeeping, dedupe.

`test_group.py` covers the classifiers and the thread-linkage helpers as
functions over plain values. This file covers `scan_group_with_client`, which
is what *orders* them — the half that was live-run-only until the scan started
taking a client instead of opening its own session.
"""

import sqlite3

import pytest
from telethon.errors import ChannelPrivateError
from telethon.tl.types import MessageActionChatAddUser, MessageActionChatDeleteUser

from slop_writer.db import db_path_for, open_db
from slop_writer.errors import SlopWriterError
from slop_writer.group import scan_group_with_client

from .conftest import run
from .factories import (
    CHANNEL_ID,
    DATE,
    AdminLogEvent,
    AdminLogPage,
    FakeClient,
    FullChannel,
    Named,
    channel,
    entity_key,
    msg,
    service,
    user,
)

CHANNEL = "@chan"
GROUP_ID = 2002
GROUP = channel(GROUP_ID, title="The Chat", username="chat")


def linked_client(messages, **kw):
    """A client where `@chan` (id 1001) has 2002 as its discussion group —
    the two `GetFullChannelRequest` hops `resolve_group_target` makes."""
    return FakeClient(
        messages,
        entities={CHANNEL: channel(CHANNEL_ID), GROUP_ID: GROUP},
        full={
            CHANNEL_ID: FullChannel(title="The Channel", linked_chat_id=GROUP_ID),
            GROUP_ID: FullChannel(title="The Chat", participants=42),
        },
        **kw,
    )


def scan(client, root, **kw):
    return run(scan_group_with_client(client, CHANNEL, None, root, **kw))


def rows(root, sql, params=(), handle=CHANNEL):
    conn = sqlite3.connect(db_path_for(root, handle))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def seed(root, sql, params=(), handle=CHANNEL):
    conn = open_db(root, handle)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def root_of(post_id, group_msg_id, date=None):
    """The channel's auto-forward into the group — the thread root."""
    return msg(
        group_msg_id, text=f"post {post_id}", date=date,
        fwd_channel_id=CHANNEL_ID, fwd_channel_post=post_id,
    )


# --------------------------------------------------------------------------
# Resolving the target
# --------------------------------------------------------------------------


def test_a_channel_without_a_discussion_group_creates_no_database(tmp_path):
    """Resolve before `open_db`, the group-side half of the invariant — and
    unlike the scrape path it lives inside the scan, because which entity to
    open a DB for is what `resolve_group_target` decides."""
    client = FakeClient(
        [],
        entities={CHANNEL: channel(CHANNEL_ID)},
        full={CHANNEL_ID: FullChannel(title="The Channel", linked_chat_id=None)},
    )

    with pytest.raises(SlopWriterError, match="no linked discussion group"):
        scan(client, tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_a_typo_creates_no_database(tmp_path):
    client = FakeClient([], entities={}, full={})

    with pytest.raises(SlopWriterError) as exc:
        run(scan_group_with_client(client, "@typo", None, tmp_path))

    assert exc.value.code == "CANNOT_RESOLVE"
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# Membership — the requirement the tool descriptions state and the scan spent
# three calls failing to name (#35)
# --------------------------------------------------------------------------


def test_a_linked_group_this_account_is_not_in_is_not_a_resolve_failure(tmp_path):
    """The case the code was written for. The channel resolves, Telegram hands
    over the discussion group's *id*, and then refuses the group itself — so
    the handle is provably fine and only membership is missing."""
    client = linked_client([])
    client.entities[GROUP_ID] = ChannelPrivateError(None)

    with pytest.raises(SlopWriterError) as exc:
        scan(client, tmp_path)

    assert exc.value.code == "NOT_A_MEMBER"
    assert list(tmp_path.iterdir()) == []


def test_a_linked_group_missing_from_the_session_cache_reads_the_same_way(tmp_path):
    """Telethon's answer for a peer it has no access hash for is a bare
    `ValueError` — indistinguishable from a typo *in general*, but not here:
    `linked_chat_id` came from Telegram one call earlier, so the group exists
    and an account that were in it would have seen it."""
    client = linked_client([])
    del client.entities[GROUP_ID]

    with pytest.raises(SlopWriterError) as exc:
        scan(client, tmp_path)

    assert exc.value.code == "NOT_A_MEMBER"
    assert list(tmp_path.iterdir()) == []


def test_a_group_that_refuses_its_own_full_channel_is_a_membership_failure(tmp_path):
    """`GetFullChannelRequest` on the group is the second call past
    `resolve_peer`, and it can refuse where `get_entity` did not."""
    client = linked_client([])
    client.full[GROUP_ID] = ChannelPrivateError(None)

    with pytest.raises(SlopWriterError) as exc:
        scan(client, tmp_path)

    assert exc.value.code == "NOT_A_MEMBER"


def test_a_group_that_resolves_but_refuses_its_history_is_still_not_a_member(tmp_path):
    """Resolving is not reading. An entity cached from an earlier membership
    resolves fine and then refuses `iter_messages` — the third site, and the
    only one that fires after `open_db`, so this one *does* leave the file."""
    client = linked_client([], iter_error=ChannelPrivateError(None))

    with pytest.raises(SlopWriterError) as exc:
        scan(client, tmp_path)

    assert exc.value.code == "NOT_A_MEMBER"


def test_the_membership_failure_names_membership_and_not_admin_rights(tmp_path):
    """The distinction the tool descriptions make ("requires membership in the
    group (not admin)") has to survive into the error, or the agent reads it as
    NOT_ADMIN and gives up on a group it could simply join."""
    client = linked_client([])
    client.entities[GROUP_ID] = ChannelPrivateError(None)

    with pytest.raises(SlopWriterError) as exc:
        scan(client, tmp_path)

    assert "not admin rights" in (exc.value.hint or "")


def test_a_standalone_group_reports_no_threads(tmp_path):
    client = FakeClient(
        [msg(600, text="hello", sender_id=7)],
        entities={"@grp": GROUP},
        full={GROUP_ID: FullChannel(title="The Chat", participants=42)},
    )

    result = run(scan_group_with_client(client, None, "@grp", tmp_path))

    assert result.overview["standalone"] is True
    assert result.overview["members"] == 42
    assert result.overview["link"] == "https://t.me/chat"
    assert result.threads == []
    assert rows(tmp_path, "SELECT id FROM group_messages", handle="@grp") == [(600,)]


# --------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------


def test_comments_are_linked_to_the_post_their_root_forwards(tmp_path):
    client = linked_client(
        [
            root_of(100, 500),
            msg(501, text="first", sender_id=7, reply_to_msg_id=500),
            msg(502, text="nested", sender_id=8, reply_to_msg_id=501,
                reply_to_top_id=500),
            msg(503, text="unrelated chatter", sender_id=9),
        ]
    )

    result = scan(client, tmp_path)

    assert rows(
        tmp_path,
        "SELECT id, thread_post_id, is_thread_root FROM group_messages ORDER BY id",
    ) == [(500, 100, 1), (501, 100, 0), (502, 100, 0), (503, None, 0)]
    assert [(t["post_id"], t["replies"], t["commenters"]) for t in result.threads] == [
        (100, 2, 2)
    ]
    # The post itself has not been scraped, so the link is built from the handle.
    assert result.threads[0]["scraped"] is False
    assert result.threads[0]["post_link"] == "https://t.me/chan/100"


def test_the_scanned_window_is_snapshotted_before_back_fetched_roots(tmp_path):
    """The bug the comment in `scan_group_with_client` describes.

    `latest=2` scans ids 600-601, whose root (500) fell outside the window and
    gets back-fetched. If `scanned_ids` were taken *after* that append, `lo`
    would drop to 500 and the thread-stats query would sweep in row 550 — a
    previous scan's comment on a different post — and report a thread this run
    never touched.
    """
    seed(
        tmp_path,
        "INSERT INTO group_messages (id, date, text, user_id, thread_post_id,"
        " is_thread_root) VALUES (550, '2026-02-01T00:00:00+00:00', 'older',"
        " 3, 90, 0)",
    )
    client = linked_client(
        [
            root_of(100, 500),
            msg(600, text="a comment", sender_id=7, reply_to_msg_id=500),
            msg(601, text="another", sender_id=8, reply_to_msg_id=500),
        ]
    )

    result = scan(client, tmp_path, latest=2)

    assert result.overview["id_range"] == "600..601"
    assert [t["post_id"] for t in result.threads] == [100]
    # The root really did arrive out of window, through a targeted fetch.
    assert client.calls == [[500]]
    assert rows(
        tmp_path, "SELECT thread_post_id FROM group_messages WHERE id = 500"
    ) == [(100,)]


def test_an_out_of_window_head_that_is_not_a_root_is_left_alone(tmp_path):
    """`unresolved_root_refs` over-collects on purpose: a reply to top-level
    chatter names a head too. The fetch must not turn it into a thread."""
    client = linked_client(
        [msg(601, text="a reply", sender_id=7, reply_to_msg_id=590)]
        + [msg(590, text="plain chatter", sender_id=8)]
    )

    result = scan(client, tmp_path, latest=1)

    assert client.calls == [[590]]
    assert result.threads == []
    assert rows(tmp_path, "SELECT id, thread_post_id FROM group_messages") == [
        (601, None)
    ]


def test_an_empty_window_reports_a_dash_range(tmp_path):
    result = scan(linked_client([]), tmp_path)

    assert result.overview["id_range"] == "—"
    assert result.messages == [] and result.threads == []
    assert db_path_for(tmp_path, CHANNEL).exists()


# --------------------------------------------------------------------------
# Membership events, and the admin-log dedupe
# --------------------------------------------------------------------------


def join_event(id, uid, date=None):
    return AdminLogEvent(
        id, Named("ChannelAdminLogEventActionParticipantJoin"),
        user_id=uid, date=date or DATE,
    )


def test_service_messages_become_membership_events(tmp_path):
    client = linked_client(
        [
            service(700, MessageActionChatAddUser(users=[7, 8]), sender_id=7),
            service(701, MessageActionChatDeleteUser(user_id=9), sender_id=9),
        ]
    )

    result = scan(client, tmp_path)

    assert rows(
        tmp_path, "SELECT id, kind, via, user_id FROM group_events ORDER BY id, user_id"
    ) == [(700, "join", "added", 7), (700, "join", "added", 8),
          (701, "leave", "self", 9)]
    assert {(e["kind"], e["via"]) for e in result.events} == {
        ("join", "added"), ("leave", "self")
    }


def test_an_admin_event_already_seen_as_a_service_message_is_dropped(tmp_path):
    """Same join, two sources, unrelated ids — so the (id, user_id) PK cannot
    dedupe it. The (user_id, kind) match inside 10 minutes is what does."""
    client = linked_client(
        [service(700, MessageActionChatAddUser(users=[7]), sender_id=7)],
        admin_log=[
            AdminLogPage(
                [join_event(999001, 7, DATE.replace(minute=5))],
                [user(7, first="Ann", username="ann")],
            )
        ],
    )

    scan(client, tmp_path)

    assert rows(tmp_path, "SELECT id, user_id FROM group_events") == [(700, 7)]


def test_an_admin_event_matching_an_earlier_runs_row_is_dropped(tmp_path):
    """The other arm of the dedupe: the service message was captured by a
    *previous* scan, so it isn't in this run's `service_events` at all."""
    seed(
        tmp_path,
        "INSERT INTO group_events (id, date, kind, via, user_id)"
        " VALUES (700, '2026-03-01T12:00:00+00:00', 'join', 'added', 7)",
    )
    client = linked_client(
        [],
        admin_log=[
            AdminLogPage([join_event(999001, 7, DATE.replace(minute=5))], [user(7)])
        ],
    )

    scan(client, tmp_path)

    assert rows(tmp_path, "SELECT id, user_id FROM group_events") == [(700, 7)]


def test_a_rejoin_outside_the_ten_minute_window_is_a_second_event(tmp_path):
    seed(
        tmp_path,
        "INSERT INTO group_events (id, date, kind, via, user_id)"
        " VALUES (700, '2026-03-01T12:00:00+00:00', 'join', 'added', 7)",
    )
    client = linked_client(
        [],
        admin_log=[
            AdminLogPage(
                [join_event(999001, 7, DATE.replace(minute=30))],
                [user(7, first="Ann", last="Lee", username="ann")],
            )
        ],
    )

    scan(client, tmp_path)

    assert rows(
        tmp_path, "SELECT id, user_id FROM group_events ORDER BY id"
    ) == [(700, 7), (999001, 7)]
    # The log's own user objects name the subject; no extra get_entity hop —
    # only the two the target resolution makes.
    assert rows(
        tmp_path, "SELECT user_name, user_username FROM group_events WHERE id = 999001"
    ) == [("Ann Lee", "ann")]
    assert [entity_key(p) for p in client.entity_calls] == [CHANNEL, GROUP_ID]


def test_a_subject_the_admin_log_did_not_name_is_resolved_individually(tmp_path):
    """Added users never sent a message, so nothing else caches their name."""
    client = linked_client(
        [service(700, MessageActionChatAddUser(users=[7]), sender_id=42)],
    )
    client.entities[7] = user(7, first="Ann", last="Lee", username="ann")

    scan(client, tmp_path)

    assert rows(
        tmp_path, "SELECT user_name, user_username FROM group_events"
    ) == [("Ann Lee", "ann")]


def test_an_unresolvable_subject_is_stored_without_a_name(tmp_path):
    client = linked_client(
        [service(700, MessageActionChatAddUser(users=[7]), sender_id=42)]
    )

    scan(client, tmp_path)

    assert rows(
        tmp_path, "SELECT user_id, user_name, user_username FROM group_events"
    ) == [(7, None, None)]


def test_no_admin_rights_leaves_the_service_messages_standing(tmp_path):
    """A non-admin account gets an error from the log; the scan must degrade
    rather than fail, because service messages still carry most joins."""
    client = linked_client(
        [service(700, MessageActionChatAddUser(users=[7]), sender_id=7)],
        admin_log_error=RuntimeError("CHAT_ADMIN_REQUIRED"),
    )

    result = scan(client, tmp_path)

    assert rows(tmp_path, "SELECT id, user_id FROM group_events") == [(700, 7)]
    assert len(result.events) == 1
    # And the reader is told, because 1 join from service messages alone is a
    # floor: Telegram suppresses them during exactly the bursts worth counting.
    assert result.overview["admin_log"] is False


def test_a_readable_admin_log_is_reported_as_read(tmp_path):
    """The counterpart: empty is ambiguous on its own. A group with no
    membership changes and a group whose log this account cannot read both
    produce zero admin events, and only one of the two makes the counts a
    total."""
    client = linked_client([], admin_log=[])

    result = scan(client, tmp_path)

    assert result.overview["admin_log"] is True


def test_the_admin_log_is_walked_until_it_returns_nothing(tmp_path):
    client = linked_client(
        [],
        admin_log=[
            AdminLogPage([join_event(999001, 7)], [user(7, username="a")]),
            AdminLogPage([join_event(999002, 8)], [user(8, username="b")]),
        ],
    )

    scan(client, tmp_path)

    assert rows(
        tmp_path, "SELECT id, user_id FROM group_events ORDER BY id"
    ) == [(999001, 7), (999002, 8)]
    # Two pages plus the empty one that ends the walk.
    assert sum(
        1 for r in client.requests if type(r).__name__ == "GetAdminLogRequest"
    ) == 3


# --------------------------------------------------------------------------
# The per-run snapshot
# --------------------------------------------------------------------------


def test_group_metrics_are_appended_once_per_run(tmp_path):
    scan(linked_client([msg(600, text="hi", sender_id=7)]), tmp_path)
    scan(linked_client([msg(601, text="again", sender_id=7)]), tmp_path)

    assert rows(
        tmp_path, "SELECT group_link, group_title, members FROM group_metrics ORDER BY id"
    ) == [("https://t.me/chat", "The Chat", 42)] * 2


def test_a_rescan_updates_a_message_rather_than_duplicating_it(tmp_path):
    scan(linked_client([msg(600, text="original", sender_id=7)]), tmp_path)
    scan(linked_client([msg(600, text="edited", sender_id=7, reactions=3)]), tmp_path)

    assert rows(tmp_path, "SELECT id, text, reactions FROM group_messages") == [
        (600, "edited", 3)
    ]
