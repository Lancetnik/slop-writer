# Banning audited subscribers is a gated write, and only audited ones

In 2026-09 @fastnewsdev took a wave of farm accounts — "Анна🖐", "Виктория🖐",
"Работаю в IT" — and the owner wanted two things: to know which subscribers
were bots, and to remove them. The first is a read: **`audit_subscribers`**
reads the channel's admin log, stores every join and leave it still holds in
`subscriber_events`, snapshots each joiner still subscribed into
`subscriber_profiles`, and ranks them by bot signals. The second is the
package's second write to Telegram: **`publish_ban_subscribers`**.

## Decisions

**The ban lives in `publish.py` and carries the `publish_` prefix.** adr/0003
made `publish.py` the one module that can change a channel, and the MCP
surface gates writes by *name* — a permission rule matches
`mcp__slop-writer__publish_*`. A `ban_subscribers` tool would be the first
Telegram write outside that split: under the server-wide `allow`, unprompted.
The name reads slightly oddly. What matters is what it carries.

**Only an audited account can be banned.** `prepare_ban` refuses an id the
channel's `subscriber_profiles` does not hold, before any session is needed,
and bans nothing when one id is unknown. That bounds the tool to what it is
for: a model cannot reach a commenter, an admin, or an id it misremembered,
and the human's permission prompt shows ids that a table in the same session
explained. The price is that a bot older than any audit cannot be banned
through the tool; the Telegram app still can.

**A refused account is that account's answer (adr/0007).** One ban call takes
many accounts. Telegram refusing one (it is an admin, it vanished) is recorded
against it and the rest proceed. No admin rights at all is the *call*
refusing — nothing could succeed — and raises `NOT_ADMIN`.

**The score is a ranking, not a verdict.** Six signals, weighted and summed,
each a trait the farm had and the channel's real joiners did not: Telegram's
own scam/fake flag, a shared last-seen minute with other joiners
(`online_cluster`, the strongest), a recent id, an exact last-seen, a
generated username tail, no photo. Replayed against the wave's own data
(21 joiners) it flagged exactly the six farm accounts, and nothing else. The
weights live in `audit.py`; what a signal means to a reader lives in
`references/analysis.md`, which also says a human decides. No signal is
taken from the account's bio or from `GetFullUser`: the admin log's own user
objects carry everything scored, and one extra call per joiner is a flood
wait on exactly the large waves worth auditing.

**`NEW_ACCOUNT_ID` is a dated constant.** Ids grow roughly with registration
time, and 8e9 separates "registered in 2025 or later" today. It will need
moving; the alternative — a threshold relative to the window — would call the
oldest joiner of an all-bot wave "old".

**An upgrade adds the new gate.** `install` seeds the permission block on
first install only, so a rule the human removed stays removed (#15's headless
autoposting). Left at that, every existing project would get
`publish_ban_subscribers` under its `allow: [mcp__slop-writer]` with no `ask`
— the one outcome this ADR exists to prevent. `server.GATE_INTRODUCED` names
the release that first shipped each later write tool, and an upgrade from an
older release (read off the skill copy's `metadata.version` before it is
replaced) adds that rule and reports it. From that release on, a missing rule
is the human's choice again. **The entry says `0.5.0`, so this ships as
0.5.0**; releasing it as 0.4.x would re-add a deliberately removed ban rule
on every upgrade until 0.5.0.

## Considered options

- **A separate `moderation.py` write module.** Keeps "can post" and "can
  ban" apart, but the auditable property is "can change the channel", and a
  second write module is a second place a read path might import.
- **Banning by username or by any id.** Rejected with the audit bound above.
- **Kicking (ban then unban) instead of banning.** A kicked farm account can
  rejoin on the next run of the script that subscribed it.
- **`requiresUserInteraction` on the ban tool.** Rejected for the same reason
  as for posting (adr/0003): it forecloses a headless cleanup the owner may
  want to schedule, and the `ask` rule is the gate the human can move.
