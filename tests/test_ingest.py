"""The scrape run end to end: fetch, group, persist, heal.

Everything else in the suite tests a function that takes plain values. This
file tests the part that *orders* them — which only became reachable when
`ingest_with_client` and its two entrypoint twins started taking a client
instead of opening a session. No session, no `.tg-analytic/` under the repo,
no network: the DB is a real file under `tmp_path`, the client is a fake.

The assertions land on DB rows and on the returned `ScrapeResult`, never on
rendered output — a signature change moves the fixture, not the test.
"""

import sqlite3

import pytest

from slop_writer import scrape
from slop_writer.db import db_path_for, open_db
from slop_writer.errors import SlopWriterError
from slop_writer.scrape import (
    ingest_with_client,
    refresh_posts_with_client,
    scrape_posts,
    scrape_posts_with_client,
)

from .conftest import run
from .factories import (
    CHANNEL_ID,
    DATE,
    FakeClient,
    FullChannel,
    channel,
    fake_session,
    msg,
    photo,
    public_forward,
)

CHANNEL = "@chan"
ENTITY = channel(CHANNEL_ID)


def do_scrape(client, root, **kw):
    """One `scrape` run against `root` as the project's output dir."""
    return run(scrape_posts_with_client(client, ENTITY, CHANNEL, root, **kw))


def rows(root, sql, params=()):
    conn = sqlite3.connect(db_path_for(root, CHANNEL))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def seed(root, sql, params=()):
    """Write to the channel's DB the way a previous run would have."""
    conn = open_db(root, CHANNEL)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# The run lifecycle
# --------------------------------------------------------------------------


def test_a_bad_handle_leaves_no_database_behind(tmp_path, monkeypatch):
    """The one invariant the client split pushes up into the shell: the
    session resolves the handle before the body opens the DB, so a typo'd
    channel never creates `.tg-analytic/<typo>.db`."""
    boom = SlopWriterError("Cannot resolve @typo", code="CANNOT_RESOLVE")
    monkeypatch.setattr(
        scrape, "channel_session", fake_session(FakeClient(), error=boom)
    )

    with pytest.raises(SlopWriterError) as exc:
        run(scrape_posts("@typo", tmp_path, "unused.session", latest=5))

    assert exc.value.code == "CANNOT_RESOLVE"
    assert list(tmp_path.iterdir()) == []


def test_the_connection_is_closed_when_the_fetch_fails(tmp_path, monkeypatch):
    """`finally: conn.close()` — a source that raises mid-run must not leave
    the handle open, which is what would make a long-lived server leak one
    connection per failed call."""
    opened = []
    real_open = scrape.open_db
    monkeypatch.setattr(
        scrape, "open_db",
        lambda *a, **kw: opened.append(real_open(*a, **kw)) or opened[-1],
    )

    async def exploding_source(client, entity):
        raise RuntimeError("network went away")

    with pytest.raises(RuntimeError):
        run(
            ingest_with_client(
                FakeClient(), ENTITY, CHANNEL, tmp_path, exploding_source,
                with_comments=False, with_media=False, with_channel_info=False,
            )
        )

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


def test_the_run_heals_a_phantom_its_open_could_not_see(tmp_path):
    """The second `heal_album_phantoms` call, and the only thing it catches.

    At `open_db` time the phantom is the album's *only* row, so it isn't a
    phantom pattern yet. The real caption carrier arrives during this run —
    the healing has to run again afterwards or the album stays double-counted.
    """
    seed(
        tmp_path,
        "INSERT INTO posts (id, link, date, text, grouped_id)"
        " VALUES (10, 'l', '2026-03-01T12:00:00+00:00', '', 555)",
    )
    assert rows(tmp_path, "SELECT id FROM posts") == [(10,)]

    client = FakeClient([msg(11, text="the caption", grouped_id=555)])
    do_scrape(client, tmp_path, with_media=False, with_channel_info=False)

    assert rows(tmp_path, "SELECT id FROM posts") == [(11,)]


# --------------------------------------------------------------------------
# One album = one post row
# --------------------------------------------------------------------------


def album(*ids, gid=555, caption_on=None):
    return [
        msg(i, grouped_id=gid, media=photo(),
            text="the caption" if i == caption_on else "")
        for i in ids
    ]


def test_a_whole_album_becomes_one_post_carrying_every_attachment(tmp_path):
    client = FakeClient(album(10, 11, 12, caption_on=11))

    result = do_scrape(client, tmp_path, with_media=False, with_channel_info=False)

    assert [p["id"] for p in result.posts] == [11]
    assert rows(tmp_path, "SELECT id, text FROM posts") == [(11, "the caption")]
    assert rows(
        tmp_path,
        "SELECT post_id, attachment_id FROM post_attachments ORDER BY attachment_id",
    ) == [(11, 10), (11, 11), (11, 12)]


def test_a_window_that_cuts_an_album_still_writes_exactly_one_post(tmp_path):
    """The phantom bug's own shape: `latest=2` hands the pipeline a suffix of
    a three-member album, and the captionless 10 would have become a post of
    its own. `complete_albums` pulls 9 back first, so it never does."""
    client = FakeClient(album(9, 10, 11, caption_on=9))

    do_scrape(client, tmp_path, latest=2, with_media=False, with_channel_info=False)

    assert rows(tmp_path, "SELECT id FROM posts") == [(9,)]
    assert rows(
        tmp_path, "SELECT attachment_id FROM post_attachments ORDER BY attachment_id"
    ) == [(9,), (10,), (11,)]
    # The window really was a suffix — 9 arrived through the probe, not the walk.
    assert client.iter_calls[0]["limit"] == 2
    assert client.iter_calls[0]["reverse"] is False


def test_an_attachment_is_taken_off_the_post_that_wrongly_held_it(tmp_path):
    """`upsert_post` clears by `attachment_id`, not just `post_id`: a pre-fix
    run filed attachment 11 under phantom post 10, and nothing else would
    remove it (this album is gone, so `heal_album_phantoms` never looks)."""
    seed(
        tmp_path,
        "INSERT INTO posts (id, link, date, text) VALUES (10, 'l', 'd', 'old')",
    )
    seed(
        tmp_path,
        "INSERT INTO post_attachments (post_id, attachment_id, link, media_type)"
        " VALUES (10, 11, 'l', 'photo')",
    )

    client = FakeClient([msg(11, text="the real post", media=photo())])
    do_scrape(client, tmp_path, with_media=False, with_channel_info=False)

    assert rows(
        tmp_path, "SELECT post_id FROM post_attachments WHERE attachment_id = 11"
    ) == [(11,)]


# --------------------------------------------------------------------------
# post_metrics is a time-series, not a row per post
# --------------------------------------------------------------------------


def test_two_runs_append_two_metric_rows_and_max_id_picks_the_later(tmp_path):
    """`references/schema.md`'s canonical CTE keys "latest snapshot" off
    `MAX(id)` rather than `MAX(scrape_date)`; this is what makes that valid."""
    kw = dict(with_media=False, with_channel_info=False)
    do_scrape(FakeClient([msg(10, text="a post", views=100)]), tmp_path, **kw)
    do_scrape(FakeClient([msg(10, text="a post", views=250)]), tmp_path, **kw)

    assert rows(tmp_path, "SELECT COUNT(*) FROM posts") == [(1,)]
    assert rows(
        tmp_path, "SELECT views FROM post_metrics WHERE post_id = 10 ORDER BY id"
    ) == [(100,), (250,)]
    assert rows(
        tmp_path,
        "SELECT views FROM post_metrics"
        " WHERE id = (SELECT MAX(id) FROM post_metrics WHERE post_id = 10)",
    ) == [(250,)]


def test_the_comment_count_survives_a_run_that_skips_comments(tmp_path):
    """`--no-comments` must read the count off the post itself. Writing 0
    would poison the series for every run that did fetch them."""
    post = msg(10, text="a post", replies=7)
    client = FakeClient([post])

    do_scrape(
        client, tmp_path,
        with_comments=False, with_media=False, with_channel_info=False,
    )

    assert rows(tmp_path, "SELECT comments_count FROM post_metrics") == [(7,)]
    assert rows(tmp_path, "SELECT COUNT(*) FROM group_messages") == [(0,)]


# --------------------------------------------------------------------------
# replace_thread_comments — the scoped DELETE
# --------------------------------------------------------------------------


def test_rescraping_a_thread_touches_only_that_threads_comments(tmp_path):
    """The DELETE is scoped to `thread_post_id = ? AND is_thread_root = 0`.
    Anything wider would eat the group scan's rows: thread roots and
    top-level chatter live in the same table (docs/adr/0002)."""
    for sql in (
        # this post's root, written by a `group` scan
        "INSERT INTO group_messages (id, date, text, user_id, thread_post_id,"
        " is_thread_root) VALUES (500, 'd', 'root', 1, 100, 1)",
        # a comment on this post that has since been deleted on Telegram
        "INSERT INTO group_messages (id, date, text, user_id, thread_post_id,"
        " is_thread_root) VALUES (501, 'd', 'stale', 2, 100, 0)",
        # top-level chatter, no thread at all
        "INSERT INTO group_messages (id, date, text, user_id, thread_post_id,"
        " is_thread_root) VALUES (502, 'd', 'chatter', 3, NULL, 0)",
        # a comment on a different post
        "INSERT INTO group_messages (id, date, text, user_id, thread_post_id,"
        " is_thread_root) VALUES (503, 'd', 'elsewhere', 4, 200, 0)",
    ):
        seed(tmp_path, sql)

    post = msg(100, text="a post", replies=1)
    fresh = msg(504, text="a new comment", sender_id=9, reply_to_msg_id=500)
    client = FakeClient([post], comments={100: [fresh]})

    do_scrape(client, tmp_path, with_media=False, with_channel_info=False)

    assert rows(tmp_path, "SELECT id FROM group_messages ORDER BY id") == [
        (500,), (502,), (503,), (504,),
    ]
    assert rows(
        tmp_path, "SELECT thread_post_id, is_thread_root FROM group_messages"
        " WHERE id = 504"
    ) == [(100, 0)]
    assert rows(tmp_path, "SELECT comments_count FROM post_metrics") == [(1,)]


# --------------------------------------------------------------------------
# The optional round-trips: media, forwards, channel info
# --------------------------------------------------------------------------


def test_a_cached_photo_is_not_downloaded_again(tmp_path):
    client = FakeClient([msg(10, text="a post", media=photo())])

    do_scrape(client, tmp_path, with_channel_info=False)
    do_scrape(client, tmp_path, with_channel_info=False)

    assert client.downloads == [10]
    assert (tmp_path / "media" / "10.jpg").exists()
    assert rows(tmp_path, "SELECT photo_path FROM post_attachments") == [
        (str(tmp_path / "media" / "10.jpg"),)
    ]


def test_a_post_forwarded_from_elsewhere_records_its_source(tmp_path):
    source_id = 4004
    client = FakeClient(
        [msg(10, text="quoting them", fwd_channel_id=source_id, fwd_channel_post=77)],
        entities={source_id: channel(source_id, title="Source", username="src")},
        full={source_id: FullChannel(title="Source", about="about", participants=900)},
    )

    result = do_scrape(client, tmp_path, with_media=False)

    assert rows(tmp_path, "SELECT forwarder_from_channel FROM posts") == [
        ("https://t.me/src",)
    ]
    assert rows(
        tmp_path, "SELECT link, name, subscribers FROM public_channels"
    ) == [("https://t.me/src", "Source", 900)]
    assert [c["link"] for c in result.channels] == ["https://t.me/src"]


def test_public_forwards_become_shares_and_a_metric(tmp_path):
    sharer_id = 5005
    client = FakeClient(
        [msg(10, text="a post", forwards=3)],
        entities={sharer_id: channel(sharer_id, title="Sharer", username="shr")},
        full={sharer_id: FullChannel(title="Sharer", participants=12)},
        forwards={10: [public_forward(88, sharer_id)]},
    )

    do_scrape(client, tmp_path, with_media=False)

    assert rows(
        tmp_path, "SELECT forwarder_link, msg_link FROM public_shares"
    ) == [("https://t.me/shr", "https://t.me/shr/88")]
    assert rows(tmp_path, "SELECT public_forwards_count FROM post_metrics") == [(1,)]


def test_channel_info_is_skipped_when_asked(tmp_path):
    """`--no-channel-info` drops the per-forwarder round-trip; the channel row
    is still written, just without name or subscriber count."""
    sharer_id = 5005
    client = FakeClient(
        [msg(10, text="a post", forwards=3)],
        entities={sharer_id: channel(sharer_id, username="shr")},
        forwards={10: [public_forward(88, sharer_id)]},
    )

    do_scrape(client, tmp_path, with_media=False, with_channel_info=False)

    assert rows(tmp_path, "SELECT link, name FROM public_channels") == [
        ("https://t.me/shr", None)
    ]
    assert [type(r).__name__ for r in client.requests] == [
        "GetMessagePublicForwardsRequest"
    ]


# --------------------------------------------------------------------------
# The selection window, and the id-refresh entrypoint
# --------------------------------------------------------------------------


def test_latest_walks_newest_first_and_offset_id_is_inclusive(tmp_path):
    posts = [msg(i, text=f"post {i}") for i in (10, 11, 12, 13)]

    newest = do_scrape(FakeClient(posts), tmp_path, latest=2,
                       with_media=False, with_channel_info=False)
    assert sorted(p["id"] for p in newest.posts) == [12, 13]

    client = FakeClient(posts)
    paged = do_scrape(client, tmp_path, limit=2, offset_id=11,
                      with_media=False, with_channel_info=False)
    # offset_id=11 means "start at 11", so 11 itself must come back.
    assert sorted(p["id"] for p in paged.posts) == [11, 12]
    assert client.iter_calls[0]["reverse"] is True
    assert client.iter_calls[0]["offset_id"] == 10


def test_a_refresh_skips_ids_the_channel_no_longer_has(tmp_path, caplog):
    client = FakeClient([msg(10, text="still here", views=5)])

    with caplog.at_level("WARNING"):
        result = run(
            refresh_posts_with_client(
                client, ENTITY, CHANNEL, [10, 99], tmp_path,
                with_media=False, with_channel_info=False,
            )
        )

    assert [p["id"] for p in result.posts] == [10]
    assert "[99]" in caplog.text
    assert rows(tmp_path, "SELECT id FROM posts") == [(10,)]


def test_an_empty_window_writes_nothing_but_still_opens_the_db(tmp_path):
    result = do_scrape(FakeClient([]), tmp_path, latest=5,
                       with_media=False, with_channel_info=False)

    assert result.posts == [] and result.channels == []
    assert db_path_for(tmp_path, CHANNEL).exists()
    assert rows(tmp_path, "SELECT COUNT(*) FROM post_metrics") == [(0,)]


def test_the_returned_summary_is_ordered_and_carries_the_engagement(tmp_path):
    client = FakeClient(
        [
            msg(11, text="second #tag", views=20, forwards=1, reactions=4, stars=2),
            msg(10, text="first", views=10),
        ]
    )

    result = do_scrape(client, tmp_path, with_media=False, with_channel_info=False)

    assert [p["id"] for p in result.posts] == [10, 11]
    assert result.channel == CHANNEL
    second = result.posts[1]
    assert second["reactions"] == 4 and second["stars"] == 2
    assert second["tags"] == ["tag"]
    assert second["link"] == "https://t.me/chan/11"
    assert second["date"] == DATE.isoformat()
