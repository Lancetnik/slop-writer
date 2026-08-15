# Post markup reference

Read this before writing a post body for `publish_schedule` or `publish_edit`.
A body is Markdown, walked straight to Telegram's formatting entities — there
is **no HTML step**, so HTML tags are not interpreted (write Markdown, not
`<b>`).

## Supported markup

| Markdown | Renders as | Notes |
| --- | --- | --- |
| `**bold**` | bold | |
| `*italic*` or `_italic_` | italic | |
| `~~strike~~` | strikethrough | |
| `^^underline^^` | underline | mistune `insert` syntax — **not** `<u>` |
| `\|\|spoiler\|\|` | hidden text | custom inline rule (Telegram's own spoiler syntax) |
| `` `code` `` | inline monospace | |
| ```` ```lang\n…\n``` ```` | code block | language label preserved (e.g. ` ```python `) |
| `[text](https://url)` | inline link | |
| `> line` | blockquote | consecutive `>` lines join into one quote |
| `- a` / `* a` | bulleted list | rendered with `•` |
| `1. a` | numbered list | |
| nested list (indent 2 spaces) | nested list | |
| `# Heading` … `###### ` | **bold line** | Telegram has no heading entity |
| Markdown table (`\| a \| b \|`) | aligned **monospace** block | Telegram has no table entity |

Formats **nest/overlap** correctly — e.g. `**bold with a [link](url)**`, a
blockquote containing `**bold**` and `||spoiler||`, or a list item with mixed
styles. Offsets are tracked in UTF-16 units, so emoji and Cyrillic stay aligned.

## Passed through literally

- `#hashtags` — kept as text; Telegram auto-links them client-side. (This is
  why the parser is mistune, not Python-Markdown: `#word` must **not** become a
  heading.)
- Emoji — any unicode emoji, inline anywhere.
- A lone `<` or `>` in prose (e.g. `5 < 10`) — stays literal.

## Not available in a Telegram message

These exist in Telegram's Instant-View `RichText`, never in a sent message, so
there is no Markdown for them and they cannot be produced:

- subscript / superscript
- highlight / marker (`==mark==` has no message entity)

Tables and headings have no native entity either; they are emulated (monospace
block, bold line) as noted above. Raw HTML is not parsed.

## Tips

- A list may follow a paragraph **without** a blank line in between.
- For tabular data, a Markdown table or a fenced code block both render as a
  monospace block — pick whichever reads better.
- A body is published **verbatim** — nothing strips front-matter, a draft's
  working title, or trailing notes. Send the clean body and nothing else.
