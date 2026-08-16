# slop-writer

An MCP server and a Claude Code skill that scrape a Telegram channel into a
per-channel SQLite DB and answer analytics questions over it. They can also
queue a future post to the channel (the one write capability; everything else
only reads).

## Language

**Project**:
The directory the server runs in — one `.tg-analytic/`, and the unit of
state. Holds any number of scraped channels, one of which is primary.

**Channel**:
The Telegram broadcast channel being analyzed (e.g. @fastnewsdev). Posts
originate here; only admins can post.

**Scraped channel**:
A channel this project holds a local database for, i.e. one scanned at least
once. The set of them is what the project can answer for without reaching
Telegram.

**Primary channel**:
The scraped channel an agent falls back to when the user names none.
Remembered by the client across sessions, never stored in the project.
_Avoid_: default channel, main channel

**Discussion group**:
The supergroup linked to the channel (`linked_chat_id`), where channel posts
auto-forward and comment threads live.
_Avoid_: comments group, chat, attached group

**Standalone group**:
A supergroup analyzed in its own right, not linked to any channel under
analysis. Has join/leave events and engagement but no threads (threads
require an originating channel post). Standalone describes the analysis, not
the supergroup: the same one is a discussion group once its channel is
analyzed, so a thread-free record says how it was scanned, not what it is.

**Post**:
A message published in the channel. Identified by its channel message id. An
album is one post, however many messages carry it — which is why a selection
is counted in posts and never in messages.

**Exhausted history**:
A scrape whose walk ran out of channel to read rather than stopping at the
count it was asked for. The property of one run, not of a channel: the same
channel is exhausted by a wide window and not by a narrow one. A run that
walks no window (a refresh of known ids) is neither.
_Avoid_: complete scrape, full scrape

**Scheduled post**:
A post queued for Telegram to publish at a future instant, not yet live. Its
id is a scheduled-message id distinct from the published-post id it later
gets, carries no engagement, and is not persisted in the DB. May only be
queued at least one hour ahead (the minimum lead time).
_Avoid_: draft, pending post

**Query batch**:
A series of independent questions answered against one snapshot of one
scraped channel. Independent is the load-bearing word: the answers are
positional, and a question the database refuses is answered by that refusal
while the rest still answer. Asking two questions together is a statement
that neither needs the other's answer, not that they succeed or fail
together.
_Avoid_: transaction, script, multi-query

**Comment**:
A message in the discussion group replying (directly or transitively) to an auto-forwarded channel post. Stored in `group_messages` with `thread_post_id` set to the originating post's id.

**Group message**:
Any non-service message in the discussion group, comments included. The
single self-contained record for group analytics and comments
(`group_messages`), written by both the channel scrape and the group scan.

**Thread**:
The set of group messages replying (directly or transitively) to one
auto-forwarded channel post. Identified by the originating post's id.

**Top-level chatter**:
Group messages outside any thread (no originating post).
_Avoid_: general messages, off-topic

**Join event**:
A dated record of a user joining the discussion group (by link, by request
approval, or added by a member / Join button). Sourced from service
messages, or from the group's admin log when scanning as an admin —
Telegram suppresses service messages during join bursts.

**Leave event**:
A dated record of a user leaving the discussion group — self-leave,
removed by an admin, or actor unknown (Telegram omits the actor e.g. when
auto-removing deleted accounts). Same two sources as Join event.
