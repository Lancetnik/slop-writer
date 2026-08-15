"""The album invariant: one album = one `posts` row.

This is the reason the suite exists. The phantom bug was silent — Telegram
reports views and forwards on *every* album member, so an extra row carried
plausible numbers and every SUM/AVG over `posts` counted the album twice.
Both halves are covered here: `complete_albums` stops new phantoms being
written, `heal_album_phantoms` clears the ones already on disk.
"""

import pytest

from slop_writer.db import heal_album_phantoms
from slop_writer.scrape import ALBUM_MAX_ITEMS, complete_albums

from .conftest import run
from .factories import FakeClient, msg

# --------------------------------------------------------------------------
# heal_album_phantoms — the rows already in the DB
# --------------------------------------------------------------------------


def add_post(conn, post_id, *, text="", grouped_id=None):
    conn.execute(
        "INSERT INTO posts (id, link, date, text, grouped_id) VALUES (?, ?, ?, ?, ?)",
        (post_id, f"https://t.me/c/1/{post_id}", "2026-03-01T12:00:00+00:00",
         text, grouped_id),
    )


def add_metrics(conn, post_id, views=100):
    conn.execute(
        "INSERT INTO post_metrics (post_id, scrape_date, views) VALUES (?, ?, ?)",
        (post_id, "2026-03-01T12:00:00+00:00", views),
    )


def add_attachment(conn, post_id, attachment_id):
    conn.execute(
        "INSERT INTO post_attachments (post_id, attachment_id, link, media_type)"
        " VALUES (?, ?, ?, 'photo')",
        (post_id, attachment_id, f"https://t.me/c/1/{attachment_id}"),
    )


def add_share(conn, post_id, forwarder="https://t.me/other"):
    conn.execute(
        "INSERT INTO public_shares (post_id, forwarder_link, msg_link, first_seen)"
        " VALUES (?, ?, ?, ?)",
        (post_id, forwarder, f"{forwarder}/9", "2026-03-01T12:00:00+00:00"),
    )


def add_comment(conn, comment_id, thread_post_id):
    conn.execute(
        "INSERT INTO group_messages (id, date, text, user_id, thread_post_id,"
        " is_thread_root) VALUES (?, ?, 'nice', 7, ?, 0)",
        (comment_id, "2026-03-01T12:05:00+00:00", thread_post_id),
    )


def post_ids(conn):
    return [r[0] for r in conn.execute("SELECT id FROM posts ORDER BY id")]


def test_phantom_collapses_onto_the_caption_carrier(conn):
    """The classic shape: a window that started inside an album left the
    captionless member behind as a post of its own."""
    add_post(conn, 10, text="", grouped_id=555)          # the phantom
    add_post(conn, 11, text="the caption", grouped_id=555)  # the real post
    add_metrics(conn, 10)
    add_metrics(conn, 11)
    add_attachment(conn, 10, 10)
    add_attachment(conn, 11, 11)
    add_share(conn, 10)
    add_comment(conn, 900, thread_post_id=10)

    heal_album_phantoms(conn)

    assert post_ids(conn) == [11]
    assert conn.execute(
        "SELECT COUNT(*) FROM post_metrics WHERE post_id = 10"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM post_attachments WHERE post_id = 10"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM public_shares WHERE post_id = 10"
    ).fetchone()[0] == 0
    # The comment thread moves onto the surviving post rather than dangling.
    assert conn.execute(
        "SELECT thread_post_id FROM group_messages WHERE id = 900"
    ).fetchone()[0] == 11
    # The survivor keeps everything of its own.
    assert conn.execute(
        "SELECT COUNT(*) FROM post_metrics WHERE post_id = 11"
    ).fetchone()[0] == 1


def test_captionless_album_keeps_its_lowest_id(conn):
    """No member carries text, so there is no caption carrier to prefer."""
    add_post(conn, 20, text="", grouped_id=777)
    add_post(conn, 21, text="", grouped_id=777)
    add_post(conn, 22, text="", grouped_id=777)

    heal_album_phantoms(conn)

    assert post_ids(conn) == [20]


def test_head_is_the_caption_carrier_even_when_it_is_not_first(conn):
    add_post(conn, 30, text="", grouped_id=888)
    add_post(conn, 31, text="", grouped_id=888)
    add_post(conn, 32, text="caption arrives last", grouped_id=888)
    add_comment(conn, 901, thread_post_id=30)

    heal_album_phantoms(conn)

    assert post_ids(conn) == [32]
    assert conn.execute(
        "SELECT thread_post_id FROM group_messages WHERE id = 901"
    ).fetchone()[0] == 32


def test_two_text_carrying_rows_are_left_alone(conn, caplog):
    """Not the phantom pattern — two real posts that happen to share a
    grouped_id are a shape this function must not guess about."""
    add_post(conn, 40, text="first", grouped_id=999)
    add_post(conn, 41, text="second", grouped_id=999)

    with caplog.at_level("WARNING"):
        heal_album_phantoms(conn)

    assert post_ids(conn) == [40, 41]
    assert "not the phantom pattern" in caplog.text


def test_a_healthy_album_next_to_a_suspicious_one(conn):
    """One album opting out must not stop the other from being healed."""
    add_post(conn, 50, text="one", grouped_id=111)
    add_post(conn, 51, text="two", grouped_id=111)     # left alone
    add_post(conn, 60, text="", grouped_id=222)
    add_post(conn, 61, text="caption", grouped_id=222)  # healed

    heal_album_phantoms(conn)

    assert post_ids(conn) == [50, 51, 61]


def test_standalone_posts_and_single_member_albums_are_untouched(conn):
    add_post(conn, 70, text="plain post", grouped_id=None)
    add_post(conn, 71, text="", grouped_id=None)  # a captionless *photo* post
    add_post(conn, 72, text="", grouped_id=333)   # only member of its album

    heal_album_phantoms(conn)

    assert post_ids(conn) == [70, 71, 72]


def test_healing_is_idempotent(conn):
    add_post(conn, 80, text="", grouped_id=444)
    add_post(conn, 81, text="caption", grouped_id=444)

    heal_album_phantoms(conn)
    heal_album_phantoms(conn)

    assert post_ids(conn) == [81]


def test_empty_db_is_a_no_op(conn):
    heal_album_phantoms(conn)
    assert post_ids(conn) == []


# --------------------------------------------------------------------------
# complete_albums — stopping the phantom from being written in the first place
# --------------------------------------------------------------------------


def album(*ids, gid=555, caption_on=None):
    """Album members as the pipeline sees them: exactly one carries text."""
    return [
        msg(i, grouped_id=gid, text="the caption" if i == caption_on else "")
        for i in ids
    ]


def test_contiguous_window_with_neighbours_on_both_sides_skips_the_probe():
    """The window itself proves the album whole: an id below and an id above
    the group were fetched and are not members, so nothing can be missing."""
    group = album(10, 11, caption_on=10)
    client = FakeClient()

    out = run(complete_albums(client, None, [group], {9, 10, 11, 12}, True))

    assert client.calls == []
    assert [m.id for m in out[0]] == [10, 11]


def test_window_cut_at_the_low_edge_pulls_the_missing_members_back():
    """The scrape started mid-album: ids 10 and 11 came back, the caption
    carrier at 9 did not. Without this the captionless 10 becomes a post."""
    group = album(10, 11)
    head = msg(9, grouped_id=555, text="the caption")
    room = ALBUM_MAX_ITEMS - 2
    wanted = tuple(i for i in range(10 - room, 10) if i > 0)
    client = FakeClient([head])

    out = run(complete_albums(client, None, [group], {10, 11, 12}, True))

    assert client.calls == [list(wanted)]
    assert sorted(m.id for m in out[0]) == [9, 10, 11]


def test_window_cut_at_the_high_edge_probes_upwards():
    group = album(10, 11, caption_on=10)
    tail = msg(12, grouped_id=555)
    wanted = tuple(range(12, 12 + (ALBUM_MAX_ITEMS - 2)))
    client = FakeClient([tail])

    out = run(complete_albums(client, None, [group], {9, 10, 11}, True))

    assert client.calls == [list(wanted)]
    assert sorted(m.id for m in out[0]) == [10, 11, 12]


def test_neighbours_of_another_album_are_not_absorbed():
    """`get_messages` returns whatever sits at those ids; only members
    carrying *this* album's grouped_id may join the group."""
    group = album(10, 11)
    stranger = msg(9, grouped_id=666, text="a different album")
    wanted = tuple(i for i in range(10 - (ALBUM_MAX_ITEMS - 2), 10) if i > 0)
    client = FakeClient([stranger])

    out = run(complete_albums(client, None, [group], {10, 11, 12}, True))

    assert sorted(m.id for m in out[0]) == [10, 11]


def test_refresh_probes_both_sides_because_arbitrary_ids_prove_nothing():
    """`window_contiguous=False` is the `refresh_posts` arm: ids 9 and 12 were
    fetched, but a caller who asked for scattered ids has not shown that the
    album stops there."""
    group = album(10, 11, caption_on=10)
    client = FakeClient()

    run(complete_albums(client, None, [group], {9, 10, 11, 12}, False))

    assert len(client.calls) == 1
    asked = client.calls[0]
    assert any(i < 10 for i in asked) and any(i > 11 for i in asked)
    # Already-fetched ids are never re-requested.
    assert 9 not in asked and 12 not in asked


def test_probe_never_asks_for_a_non_positive_id():
    """An album at the very start of a channel would otherwise request id 0
    and below."""
    group = album(2, 3, caption_on=2)
    client = FakeClient()

    run(complete_albums(client, None, [group], {2, 3}, True))

    assert all(i > 0 for i in client.calls[0])
    assert 1 in client.calls[0]


def test_a_full_album_is_never_probed():
    group = album(*range(10, 20), caption_on=10)
    client = FakeClient()

    out = run(complete_albums(client, None, [group], set(range(10, 20)), True))

    assert client.calls == []
    assert len(out[0]) == ALBUM_MAX_ITEMS


def test_standalone_posts_are_passed_through():
    group = [msg(10, text="a plain post")]
    client = FakeClient()

    out = run(complete_albums(client, None, [group], {10}, True))

    assert client.calls == []
    assert out == [group]


@pytest.mark.parametrize("contiguous", [True, False])
def test_groups_keep_their_order_and_count(contiguous):
    groups = [[msg(1, text="a")], [msg(5, text="b")], [msg(9, text="c")]]
    client = FakeClient()

    out = run(complete_albums(client, None, groups, {1, 5, 9}, contiguous))

    assert [g[0].id for g in out] == [1, 5, 9]
