# Subscriber audits retain removal references

An audit limited to still-present joiners missed a manually banned account
whose last-seen time matched an earlier spam wave. Keep moderation history
in `subscriber_bans`, separate from append-only profile snapshots. Successful
tool bans commit individually, before proceeding to the next account. Audit
runs import administrator removals from both saved events and the live log;
this includes kicks and is history, not a claim about current membership.

Refresh reference profiles on every audit, retaining saved observations and
reporting failures when an account cannot be resolved. Compare exact activity
times with previous removals; name matched ids in the report. A self-match
is excluded and current-wave clustering is not scored a second time for the
same activity evidence. Ban history alone adds no points.

Already removed recent joiners receive a separate report section and profile
snapshots. Ordinary self-leaves remain churn. The audit still covers the
recent log window, rather than every current subscriber. A previously removed
account can be innocent: every match remains subject to owner review.
