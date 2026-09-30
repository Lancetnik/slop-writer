# Publishing

`publish_schedule`, `publish_reschedule`, `publish_edit` and
`publish_ban_subscribers` are the only tools that write to Telegram. The first
three need post rights on the channel; the ban needs admin rights to ban
users. Read [markup.md](markup.md) before writing a post body.

## The discipline

These reach a live channel, and a scheduled post publishes itself whether or
not anyone looks at it again.

- Publish only on an **explicit instruction** from the user. "Draft me a post"
  is not one; neither is "that looks good".
- The user's client prompts on every `publish_*` call, showing the exact body
  and time. That prompt **is** the agreement: call, then report what landed,
  in the user's own timezone. A wrong body or hour costs one `publish_edit`
  or `publish_reschedule`.
- After writing, **verify with `list_scheduled`**. Telegram returns nothing
  useful from an edit to a scheduled message, so the confirmation you get back
  is assembled from what was sent, not from what Telegram stored. It is a
  receipt for the request, not proof of the result.

## The queue is not in the database

Scheduled posts live in Telegram alone. Nothing about them is stored locally,
`run_query` cannot see them, and no analytics question touches them — they
have no engagement yet.

Their ids are their own species. The id `list_scheduled` reports identifies a
post *in the queue*; it survives an edit and a reschedule, and it is **not**
the id the post gets once it publishes. Never carry one into an analytics
query, and never pass a published post's id to a publish tool.

## Times

`list_scheduled` reports **UTC throughout**, including each entry's own
heading. People hold their plans in local time, so convert before reporting
the queue and say which timezone you converted to.

Going the other way, a publish time must carry a UTC offset — a naive one is
rejected. The hour is yours to pick, not the user's to supply: rescheduling
keeps the scheduled post's current time of day, a fresh one takes the hour the
channel usually publishes at. Name the instant you picked.

**A new publish time must be at least an hour out.** The floor has no
override, and it is not a Telegram limit — it exists so that scheduling cannot
be used to publish something effectively now. If the user wants it sooner, the
answer is a later time or a human posting it themselves, never a workaround.
Rewriting the body of an already-queued post is exempt: fixing a typo on an
imminent post must not be blocked.

## Bodies, photos and captions

A post body is Markdown, rendered straight to Telegram's formatting entities —
see [markup.md](markup.md) for what survives the trip. It is published
**verbatim**: nothing strips a draft's headers, notes or working titles, so
send the clean body and nothing else.

Attaching images turns the post into an album, and the body stops being a body
and becomes the album's **caption**. That changes two things. A caption may be
empty, so a photo-only post is legitimate. And captions are held to a much
shorter length limit than text posts — a limit Telegram enforces, not this
server, and one that depends on whether the account has Premium. A body that
was fine as a text post can be rejected as a caption; when Telegram rejects
it, nothing is queued.

Rewriting a post that has photos rewrites its caption. The photos themselves
cannot be changed — that needs a new post.

## Which write tool

- **Time changes, body stays** → `publish_reschedule`.
- **Body changes, time stays** → `publish_edit`. It **replaces** the body
  rather than appending to it, so read the post with `list_scheduled` first
  whenever you are editing text you did not write in this session.
- **Both change** → reschedule and edit are separate calls; report both once
  they land.

## Banning subscribers

`publish_ban_subscribers` removes accounts from the channel and keeps them
out. It only accepts accounts that `audit_subscribers` saw join this channel,
so audit first, and ban from that result — never from a name the user typed
or from a commenter list.

- Ban only on an **explicit instruction** naming who: "ban the likely ones"
  after the user has seen that list; "clean up the bots" before
  they have seen it does not. Show the suspects first ([analysis.md](analysis.md)).
- The permission prompt shows the ids. Report what came back — banned and
  refused — with names, since an id means nothing to the user.
- A ban is reversible only by an admin unbanning the account in Telegram;
  nothing here unbans. A wrongly banned subscriber costs the user a manual
  fix and possibly a reader, so when an account is borderline, leave it and
  say so.
- The next `audit_subscribers` run records each ban as a leave; that is the
  way to verify one landed.
