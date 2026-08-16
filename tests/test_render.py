"""The renderers, as functions over plain dicts.

`render.py` returns a string rather than printing one (#16), which is what
makes it testable at all — and what makes these assertions about a *return
shape*, not about a script's stdout.

Only two properties are pinned here, both found by an A/B run of the CLI
against the MCP server: the heading has to keep saying which channel it is
about, and the group summary has to say where its join/leave counts came from.
Everything else about the layout stays free to move.
"""

from slop_writer.render import _handle, summarize_group, summarize_scrape


def test_a_bare_handle_renders_with_its_sigil():
    """The server normalizes `@chan` to `chan` on the way in (#15) because the
    two are one channel and the DB filename comes off the stripped form. The
    heading is display, not identity, so it gets the `@` back — otherwise the
    two callers print different headings for the same scan."""
    assert _handle("fastnewsdev") == "@fastnewsdev"


def test_a_handle_that_already_has_one_is_left_alone():
    """The CLI passes what the user typed. Idempotence is what lets one
    renderer serve both callers."""
    assert _handle("@fastnewsdev") == "@fastnewsdev"


def test_what_is_not_a_handle_is_not_decorated():
    """A group label is not always a username: a title, an invite link and a
    numeric id all reach the same heading, and prefixing those asserts
    something false."""
    assert _handle("FastNews | Chat") == "FastNews | Chat"
    assert _handle("https://t.me/joinchat/abc") == "https://t.me/joinchat/abc"
    assert _handle("-1001234567890") == "-1001234567890"
    assert _handle("abcd") == "abcd"  # 4 chars: below Telegram's minimum


def _scrape(history_exhausted) -> str:
    posts = [
        {"id": i, "date": "2026-08-16T10:00:00", "link": f"https://t.me/c/1/{i}",
         "text": "hi", "views": 10}
        for i in (41, 42)
    ]
    return summarize_scrape("chan", posts, [], history_exhausted)


def test_a_walk_that_reached_the_end_of_history_says_the_channel_is_whole():
    assert "whole channel" in _scrape(True)


def test_a_walk_that_stopped_at_its_count_says_more_history_remains():
    """The count alone reads as "that is all there is", which is how a scrape
    that filled its window got taken for a fully-scraped channel. The ids bound
    what was covered, so the next window has somewhere to start."""
    out = _scrape(False)
    assert "one window, not the whole channel" in out
    assert "41–42" in out


def test_a_run_that_walked_no_window_claims_neither():
    """Absent is not False — a refresh cannot see past the ids it was given."""
    out = _scrape(None)
    assert "whole channel" not in out and "one window" not in out


def _group(**overview) -> str:
    return summarize_group(
        "somegroup",
        {"title": "Some group", "link": "https://t.me/somegroup", **overview},
        messages=[{"id": 1, "date": "2026-08-16T10:00:00", "is_thread_root": 0}],
        events=[{"kind": "join", "via": "added", "date": "2026-08-16T10:00:00"}],
        threads=[],
    )


def test_counts_read_from_the_admin_log_say_so():
    assert "admin log" in _group(admin_log=True)


def test_counts_without_the_admin_log_are_labelled_a_floor():
    """The defect this covers was not a wrong number — it was a right number
    the reader could not qualify. Service messages alone undercount exactly
    when a CTA post works, because Telegram suppresses them during a join
    burst, and the summary looked identical either way."""
    out = _group(admin_log=False)
    assert "floor" in out
    assert "service messages only" in out


def test_an_unknown_source_claims_nothing():
    """Absent is not False. A caller that built the overview by hand does not
    know which case it is in, and a default of "service messages only" would
    be an assertion rather than a fallback."""
    out = _group()
    assert "Event source" not in out
