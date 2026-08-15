"""The write surface's validation half — everything that runs before a session.

`MIN_LEAD` is an agent-facing guard (docs/adr/0003): it exists so the agent
driving this cannot schedule a post too soon, and it has no flag or env
override by design. A silent break here is a post going out an hour early,
which is why the floor gets its own tests rather than riding along with the
rest of `prepare_schedule`.

The check *order* in `prepare_schedule` is load-bearing too — a request with
three problems must report the one nearest the front — so it is pinned by
tests that make several things wrong at once.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from slop_writer.errors import SlopWriterError, UsageError
from slop_writer.publish import (
    MAX_ALBUM,
    MIN_LEAD,
    check_photos,
    parse_schedule_time,
    prepare_schedule,
    render_body,
)


def iso_in(delta: timedelta, tz=UTC) -> str:
    return (datetime.now(UTC) + delta).astimezone(tz).isoformat()


SOON = timedelta(minutes=90)   # comfortably past the floor
TOO_SOON = timedelta(minutes=30)


# --------------------------------------------------------------------------
# The floor itself
# --------------------------------------------------------------------------


def test_min_lead_is_one_hour():
    """Pinned deliberately. adr/0003 makes this a constant with no override;
    if it ever changes, that is a decision, not a refactor."""
    assert MIN_LEAD == timedelta(hours=1)


def test_a_time_past_the_floor_is_accepted():
    when = parse_schedule_time(iso_in(SOON))
    assert when.tzinfo is not None
    assert when > datetime.now(UTC) + timedelta(minutes=59)


def test_a_time_inside_the_floor_is_rejected():
    with pytest.raises(SlopWriterError) as exc:
        parse_schedule_time(iso_in(TOO_SOON))
    assert exc.value.code == "INVALID_SCHEDULE_TIME"
    assert "too soon" in exc.value.message
    # A plain SlopWriterError, not UsageError: the arguments were well-formed,
    # the request was refused. The CLIs' exit codes differ on exactly this.
    assert not isinstance(exc.value, UsageError)
    assert exc.value.exit_code == 1


def test_a_past_time_is_rejected():
    with pytest.raises(SlopWriterError):
        parse_schedule_time(iso_in(timedelta(days=-1)))


def test_the_error_names_the_earliest_acceptable_time():
    with pytest.raises(SlopWriterError) as exc:
        parse_schedule_time(iso_in(TOO_SOON))
    assert "earliest" in exc.value.message


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_z_suffix_is_accepted_as_utc():
    when = parse_schedule_time(
        (datetime.now(UTC) + SOON).replace(microsecond=0).isoformat()
        .replace("+00:00", "Z")
    )
    assert when.utcoffset() == timedelta(0)


def test_a_non_utc_offset_is_kept():
    tz = timezone_plus(3)
    when = parse_schedule_time(iso_in(SOON, tz))
    assert when.utcoffset() == timedelta(hours=3)


def timezone_plus(hours: int):
    from datetime import timezone

    return timezone(timedelta(hours=hours))


def test_surrounding_whitespace_is_tolerated():
    parse_schedule_time("  " + iso_in(SOON) + "  ")


def test_a_naive_time_is_rejected():
    """Guessing the timezone could place a published post an hour off and
    silently defeat the floor."""
    naive = (datetime.now(UTC) + SOON).replace(tzinfo=None).isoformat()
    with pytest.raises(UsageError) as exc:
        parse_schedule_time(naive)
    assert exc.value.code == "INVALID_SCHEDULE_TIME"
    assert "no UTC offset" in exc.value.message
    assert exc.value.exit_code == 2


@pytest.mark.parametrize(
    "raw", ["tomorrow", "2026-13-45T99:00:00+03:00", "", "18:00"]
)
def test_unparseable_input_is_a_usage_error(raw):
    with pytest.raises(UsageError) as exc:
        parse_schedule_time(raw)
    assert exc.value.code == "INVALID_SCHEDULE_TIME"


# --------------------------------------------------------------------------
# check_photos
# --------------------------------------------------------------------------


@pytest.fixture
def images(tmp_path):
    def make(name: str) -> str:
        p = tmp_path / name
        p.write_bytes(b"not really an image, and nothing here reads it")
        return str(p)

    return make


def test_valid_photos_come_back_as_paths(images):
    out = check_photos([images("a.jpg"), images("b.PNG"), images("c.webp")])
    assert all(isinstance(p, Path) for p in out)
    assert [p.name for p in out] == ["a.jpg", "b.PNG", "c.webp"]


def test_extension_matching_is_case_insensitive(images):
    check_photos([images("SHOUTING.JPEG")])


def test_more_than_one_album_is_rejected(images):
    photos = [images(f"{i}.jpg") for i in range(MAX_ALBUM + 1)]
    with pytest.raises(UsageError) as exc:
        check_photos(photos)
    assert "at most 10" in exc.value.message


def test_exactly_one_full_album_is_fine(images):
    assert len(check_photos([images(f"{i}.jpg") for i in range(MAX_ALBUM)])) == 10


def test_a_missing_file_is_rejected(tmp_path):
    with pytest.raises(UsageError) as exc:
        check_photos([str(tmp_path / "nope.jpg")])
    assert "file not found" in exc.value.message


def test_a_directory_is_not_a_photo(tmp_path):
    with pytest.raises(UsageError):
        check_photos([str(tmp_path)])


def test_a_non_photo_extension_is_rejected_rather_than_sent_as_a_document(images):
    with pytest.raises(UsageError) as exc:
        check_photos([images("clip.gif")])
    assert "document attachments" in exc.value.message


# --------------------------------------------------------------------------
# render_body
# --------------------------------------------------------------------------


def test_an_empty_body_is_an_error_by_default():
    with pytest.raises(UsageError) as exc:
        render_body("   \n\n  ", source="stdin")
    assert "stdin renders to an empty post" in exc.value.message


def test_an_empty_body_is_allowed_for_a_photo_post():
    assert render_body("", allow_empty=True) == ("", [])


def test_markup_that_renders_to_nothing_still_counts_as_empty():
    with pytest.raises(UsageError):
        render_body("<!-- just a comment -->\n")


# --------------------------------------------------------------------------
# prepare_schedule — the order of checks
# --------------------------------------------------------------------------


def test_a_valid_request_becomes_a_draft(images):
    draft = prepare_schedule(
        "**hello**", iso_in(SOON), photo_paths=[images("a.jpg")]
    )
    assert draft.text == "hello"
    assert len(draft.entities) == 1
    assert [p.name for p in draft.photos] == ["a.jpg"]
    assert draft.caption_above is False


def test_time_is_checked_before_photos_and_body(images):
    """Three things wrong at once; the report names the cheapest and most
    often wrong of them."""
    with pytest.raises(SlopWriterError) as exc:
        prepare_schedule("", iso_in(TOO_SOON), photo_paths=["missing.gif"])
    assert "too soon" in exc.value.message


def test_photos_are_checked_before_the_body():
    with pytest.raises(UsageError) as exc:
        prepare_schedule("", iso_in(SOON), photo_paths=["missing.jpg"])
    assert "file not found" in exc.value.message


def test_a_captionless_photo_post_is_valid(images):
    draft = prepare_schedule("", iso_in(SOON), photo_paths=[images("a.jpg")])
    assert draft.text == ""
    assert draft.entities == []


def test_a_text_post_still_needs_a_body():
    with pytest.raises(UsageError) as exc:
        prepare_schedule("", iso_in(SOON), body_source="--file 'draft.md'")
    assert "--file 'draft.md' renders to an empty post" in exc.value.message


def test_caption_above_without_photos_is_rejected():
    with pytest.raises(UsageError) as exc:
        prepare_schedule("body", iso_in(SOON), caption_above=True)
    assert "only makes sense with --photo" in exc.value.message


def test_caption_above_needs_something_to_place_above(images):
    with pytest.raises(UsageError) as exc:
        prepare_schedule(
            "", iso_in(SOON), photo_paths=[images("a.jpg")], caption_above=True
        )
    assert "non-empty body" in exc.value.message


def test_caption_above_survives_onto_the_draft(images):
    draft = prepare_schedule(
        "caption", iso_in(SOON), photo_paths=[images("a.jpg")], caption_above=True
    )
    assert draft.caption_above is True
