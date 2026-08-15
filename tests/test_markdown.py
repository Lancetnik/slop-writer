"""Markdown -> (text, Telethon entities).

UTF-16 offset accounting is ours by choice (docs/adr/0003 — no HTML hop, no
sulguk), and until now it was verified only by looking at live posts. These are
golden tests over the markup `references/markup.md` promises.

Every assertion is on the *pair*: an offset is only correct relative to the
text that came out with it, so checking entities without the text would pass
on a body that renders wrong.
"""

import pytest

from slop_writer.markdown import render


def ents(markdown: str):
    """(text, [(entity class name, offset, length)])."""
    text, entities = render(markdown)
    return text, [(type(e).__name__, e.offset, e.length) for e in entities]


# --------------------------------------------------------------------------
# Inline spans
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src, kind",
    [
        ("**bold**", "MessageEntityBold"),
        ("__bold__", "MessageEntityBold"),
        ("*italic*", "MessageEntityItalic"),
        ("_italic_", "MessageEntityItalic"),
        ("~~strike~~", "MessageEntityStrike"),
        ("^^under^^", "MessageEntityUnderline"),
        ("||spoil||", "MessageEntitySpoiler"),
        ("`code`", "MessageEntityCode"),
    ],
)
def test_each_span_covers_exactly_its_content(src, kind):
    text, entities = ents(src)
    assert entities == [(kind, 0, len(text))]
    assert "*" not in text and "~" not in text and "|" not in text


def test_a_span_inside_a_sentence_lands_on_the_right_offset():
    text, entities = ents("say **this** loudly")
    assert text == "say this loudly"
    assert entities == [("MessageEntityBold", 4, 4)]


def test_nested_spans_both_survive():
    text, entities = ents("**bold with *italic* inside**")
    assert text == "bold with italic inside"
    assert ("MessageEntityBold", 0, 23) in entities
    assert ("MessageEntityItalic", 10, 6) in entities


def test_entities_are_sorted_by_offset_then_widest_first():
    _, entities = ents("**a *b* c** and `d`")
    offsets = [(o, -ln) for _, o, ln in entities]
    assert offsets == sorted(offsets)


def test_an_empty_span_emits_no_entity():
    text, entities = ents("****")
    assert entities == []


# --------------------------------------------------------------------------
# UTF-16 offsets — the reason this module exists
# --------------------------------------------------------------------------


def test_an_astral_emoji_counts_as_two_units():
    """🚀 is one Python character but two UTF-16 code units, which is what
    Telegram counts. Getting this wrong shifts every entity after it."""
    text, entities = ents("🚀 **bold**")
    assert text == "🚀 bold"
    assert entities == [("MessageEntityBold", 3, 4)]


def test_several_emoji_accumulate():
    text, entities = ents("🚀🔥 **x**")
    assert text == "🚀🔥 x"
    assert entities == [("MessageEntityBold", 5, 1)]


def test_a_bmp_emoji_counts_as_one():
    """★ is inside the BMP — one unit, unlike 🚀."""
    text, entities = ents("★ **x**")
    assert text == "★ x"
    assert entities == [("MessageEntityBold", 2, 1)]


def test_emoji_inside_the_span_widen_it():
    text, entities = ents("**🚀 go**")
    assert text == "🚀 go"
    assert entities == [("MessageEntityBold", 0, 5)]


def test_cyrillic_counts_as_one_unit_each():
    text, entities = ents("привет **мир**")
    assert text == "привет мир"
    assert entities == [("MessageEntityBold", 7, 3)]


# --------------------------------------------------------------------------
# Links
# --------------------------------------------------------------------------


def test_a_link_becomes_a_text_url_entity():
    text, entities = render("see [the docs](https://example.com/x) now")
    assert text == "see the docs now"
    (e,) = entities
    assert type(e).__name__ == "MessageEntityTextUrl"
    assert (e.offset, e.length, e.url) == (4, 8, "https://example.com/x")


def test_a_bare_url_keeps_its_own_text():
    text, entities = render("go to https://example.com/x")
    assert "https://example.com/x" in text
    assert entities and entities[0].url == "https://example.com/x"


def test_formatting_inside_a_link_survives():
    text, entities = ents("[**bold link**](https://example.com)")
    assert text == "bold link"
    kinds = {k for k, _, _ in entities}
    assert kinds == {"MessageEntityTextUrl", "MessageEntityBold"}


# --------------------------------------------------------------------------
# Blocks
# --------------------------------------------------------------------------


def test_a_heading_is_emulated_as_bold():
    """Telegram has no heading entity."""
    text, entities = ents("# Title\n\nbody")
    assert text == "Title\n\nbody"
    assert entities == [("MessageEntityBold", 0, 5)]


def test_a_hashtag_line_stays_literal():
    """The reason this project uses mistune rather than Python-Markdown:
    `#tag` must not become an <h1>."""
    text, entities = ents("post body\n\n#python #telegram")
    assert text.endswith("#python #telegram")
    assert entities == []


def test_a_fenced_block_carries_its_language():
    text, entities = render("```python\nprint(1)\n```")
    assert text == "print(1)"
    (e,) = entities
    assert type(e).__name__ == "MessageEntityPre"
    assert (e.offset, e.length, e.language) == (0, 8, "python")


def test_a_fence_without_a_language_gets_an_empty_one():
    _, entities = render("```\nplain\n```")
    assert entities[0].language == ""


def test_a_blockquote_wraps_its_whole_body():
    text, entities = ents("> quoted line")
    assert text == "quoted line"
    assert entities == [("MessageEntityBlockquote", 0, 11)]


def test_paragraphs_are_separated_by_a_blank_line():
    text, _ = render("one\n\ntwo")
    assert text == "one\n\ntwo"


def test_a_soft_break_becomes_a_newline():
    text, _ = render("one\ntwo")
    assert text == "one\ntwo"


# --------------------------------------------------------------------------
# Lists
# --------------------------------------------------------------------------


def test_a_bullet_list_gets_bullets():
    text, _ = render("- one\n- two")
    assert text == "• one\n• two"


def test_an_ordered_list_is_numbered():
    text, _ = render("1. one\n2. two\n3. three")
    assert text == "1. one\n2. two\n3. three"


def test_an_ordered_list_honours_its_start():
    text, _ = render("5. five\n6. six")
    assert text.startswith("5. five\n6. six")


def test_a_nested_list_is_indented():
    text, _ = render("- outer\n  - inner")
    assert "  • inner" in text


def test_formatting_inside_a_list_item_keeps_its_offset():
    text, entities = ents("- plain\n- **bold**")
    assert text == "• plain\n• bold"
    assert entities == [("MessageEntityBold", 10, 4)]


def test_a_list_may_interrupt_a_paragraph():
    """CommonMark-ish behaviour this project picked mistune for: no blank
    line required before the list."""
    text, _ = render("intro:\n- one\n- two")
    assert "• one" in text and "• two" in text


# --------------------------------------------------------------------------
# Tables — Telegram has no table entity, so they become aligned monospace
# --------------------------------------------------------------------------


def test_a_table_becomes_one_pre_block():
    src = "| a | bb |\n| --- | --- |\n| 1 | 2 |"
    text, entities = render(src)
    assert len(entities) == 1
    assert type(entities[0]).__name__ == "MessageEntityPre"
    assert entities[0].offset == 0
    assert entities[0].length == len(text.encode("utf-16-le")) // 2


def test_table_columns_are_padded_to_the_widest_cell():
    src = "| a | b |\n| --- | --- |\n| longer | x |"
    text, _ = render(src)
    lines = text.split("\n")
    assert lines[0] == "a      | b"
    assert lines[1] == "-------+--"
    assert lines[2] == "longer | x"


def test_formatting_inside_a_cell_is_flattened():
    src = "| a |\n| --- |\n| **bold** |"
    text, entities = render(src)
    assert "bold" in text and "**" not in text
    assert [type(e).__name__ for e in entities] == ["MessageEntityPre"]


# --------------------------------------------------------------------------
# Degenerate input
# --------------------------------------------------------------------------


@pytest.mark.parametrize("src", ["", "   ", "\n\n\n"])
def test_nothing_in_nothing_out(src):
    assert render(src) == ("", [])


def test_an_unclosed_spoiler_stays_literal():
    text, entities = ents("||never closed")
    assert "||never closed" == text
    assert entities == []


def test_no_entity_ever_runs_past_the_text():
    text, entities = render(
        "# Title\n\n**bold** and `code` and [link](https://e.com)\n\n"
        "> quote 🚀\n\n- item\n\n```py\nx = 1\n```"
    )
    limit = len(text.encode("utf-16-le")) // 2
    assert entities
    for e in entities:
        assert e.offset >= 0
        assert e.offset + e.length <= limit
