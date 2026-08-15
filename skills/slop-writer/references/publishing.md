# Publishing

`publish_schedule`, `publish_reschedule` and `publish_edit` are the only tools
that write to Telegram. They need post rights on the channel. Read
[markup.md](markup.md) before writing a post body.

## The discipline

These reach a live channel, and a scheduled post publishes itself whether or
not anyone looks at it again.

- Publish only on an **explicit instruction** from the user. "Draft me a post"
  is not one; neither is "that looks good".
- Show the **exact body** and the **exact time**, in the user's own timezone,
  and get their go-ahead before the call.
- The user's client prompts on every `publish_*` call. That prompt is a
  backstop against a mistake, not the agreement — do not treat clicking it as
  the confirmation you were supposed to ask for.
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

Going the other way, a publish time must carry a UTC offset. A bare wall-clock
time is rejected rather than guessed at, which is deliberate: "Friday at six"
is ambiguous, and the failure is cheaper than publishing to the wrong hour.
Resolve it with the user, not with an assumption.

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
- **Both change** → reschedule and edit are separate calls; confirm both with
  the user before either.
