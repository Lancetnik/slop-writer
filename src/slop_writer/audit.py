"""The subscriber audit: who joined the channel lately, and who of them reads
like a bot farm.

The source is the **channel's** admin log — the one place Telegram names the
accounts behind a join, and only for an admin, for ~48 hours. Each run appends
what the log still holds to `subscriber_events` and a profile snapshot per
joiner to `subscriber_profiles`, so a regular cadence keeps a history the log
itself discards.

The suspicion score is a sum of named signals, each one a trait the farm that
prompted this module (2026-09, a wave of "Анна🖐 / Работаю в IT" accounts) had
and the channel's real subscribers did not. It is a *ranking for a human to
review*, never a verdict: every signal has innocent owners, and only their
co-occurrence is telling. The weights are here, beside the signals they score;
what each one means to a reader is `references/analysis.md`'s job.

A read path: nothing here can write to Telegram. Banning lives in `publish`
(adr/0003, adr/0008), which reads this module's table and never the reverse.
"""

import logging
import re
from bisect import bisect_left, bisect_right
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.functions.channels import GetAdminLogRequest
from telethon.tl.types import ChannelAdminLogEventsFilter

from .db import db_path_for, open_db
from .errors import SlopWriterError
from .group import GroupEvent, classify_admin_log_event
from .tg import channel_session

log = logging.getLogger(__name__)

#: User ids are handed out roughly in registration order, and ids past 8e9
#: began appearing in 2025. A dated threshold, and knowingly so: it separates
#: "registered this year" from the rest today and will need moving as the id
#: space grows. Every account in the 2026-09 farm sat above it; none of the
#: channel's real joiners in the same window did.
NEW_ACCOUNT_ID = 8_000_000_000

#: A username ending in an underscore and a short random tail carrying a
#: digit — `name_viktoriya_u36z`, `oname_5c9dv`. Farm software appends one
#: when the plain name is taken. The digit requirement is what keeps
#: `ivan_dev` out.
GENERATED_USERNAME = re.compile(r"_(?=[a-z0-9]{0,5}\d)[a-z0-9]{2,6}\Z")

#: Accounts last seen within this many seconds of each other, `ONLINE_CLUSTER`
#: or more of them, came online together — which people do not and a farm
#: operator's script does. The strongest single signal in the 2026-09 wave.
ONLINE_WINDOW = 300
ONLINE_CLUSTER = 3

#: signal -> weight. The score is the sum over the signals an account shows.
WEIGHTS = {
    "telegram_flag": 5,     # Telegram itself marked the account scam or fake
    "online_cluster": 3,    # last seen together with other joiners
    "new_account": 2,       # id above NEW_ACCOUNT_ID
    "open_last_seen": 1,    # exact last-seen time visible to strangers
    "generated_username": 1,
    "no_photo": 1,
}

#: Score at or above which an account is a likely bot / worth a look.
LIKELY = 4
POSSIBLE = 2


@dataclass
class AuditResult:
    """Shapes `render.summarize_audit` consumes."""
    channel: str
    overview: dict
    accounts: list[dict]


def _admin_required(channel: str, cause: Exception) -> SlopWriterError:
    return SlopWriterError(
        f"Cannot read {channel}'s admin log: {cause}",
        hint="The subscriber audit reads the channel's admin log, which only "
        "an admin sees. Audit a channel this account administers.",
        code="NOT_ADMIN",
    )


async def fetch_membership_log(
    client: TelegramClient, entity, channel: str
) -> tuple[list[GroupEvent], dict[int, object]]:
    """Every join/leave the channel's admin log still holds, plus the users
    it names, keyed by id.

    Unlike the group scan, a refusal here is the whole answer: the log is
    the audit's only source, so no admin rights raises instead of degrading
    to an empty result that would read as "nobody joined"."""
    events_filter = ChannelAdminLogEventsFilter(
        join=True, leave=True, invite=True, ban=True, kick=True,
    )
    events: list[GroupEvent] = []
    users: dict[int, object] = {}
    max_id = 0
    while True:
        try:
            res = await client(
                GetAdminLogRequest(
                    channel=entity, q="", max_id=max_id, min_id=0,
                    limit=100, events_filter=events_filter,
                )
            )
        except FloodWaitError:
            raise
        except RPCError as e:
            raise _admin_required(channel, e) from None
        if not res.events:
            break
        for ev in res.events:
            events.extend(classify_admin_log_event(ev))
        users.update({u.id: u for u in res.users})
        max_id = res.events[-1].id
    return events, users


def _was_online(user) -> datetime | None:
    return getattr(getattr(user, "status", None), "was_online", None)


def _status_name(user) -> str | None:
    status = getattr(user, "status", None)
    if status is None:
        return None
    return type(status).__name__.removeprefix("UserStatus").lower()


def profile_of(user) -> dict:
    """The fields of a Telegram `User` the audit stores and scores."""
    first = getattr(user, "first_name", "") or ""
    last = getattr(user, "last_name", "") or ""
    was_online = _was_online(user)
    if getattr(user, "scam", False):
        flagged = "scam"
    elif getattr(user, "fake", False):
        flagged = "fake"
    else:
        flagged = None
    return {
        "user_id": user.id,
        "name": (first + " " + last).strip() or None,
        "username": getattr(user, "username", None),
        "has_photo": getattr(user, "photo", None) is not None,
        "premium": bool(getattr(user, "premium", False)),
        "deleted": bool(getattr(user, "deleted", False)),
        "flagged": flagged,
        "status": _status_name(user),
        "was_online": was_online.isoformat() if was_online else None,
    }


def online_clusters(profiles: list[dict]) -> set[int]:
    """Ids of accounts last seen within `ONLINE_WINDOW` of at least
    `ONLINE_CLUSTER - 1` others.

    Only exact last-seen times take part: a hidden one ("recently") carries
    no instant to compare, which is also why this signal and
    `open_last_seen` travel together."""
    timed = sorted(
        (datetime.fromisoformat(p["was_online"]).timestamp(), p["user_id"])
        for p in profiles
        if p.get("was_online")
    )
    stamps = [t for t, _ in timed]
    return {
        uid
        for t, uid in timed
        if bisect_right(stamps, t + ONLINE_WINDOW)
        - bisect_left(stamps, t - ONLINE_WINDOW) >= ONLINE_CLUSTER
    }


def signals_of(profile: dict, clustered: set[int]) -> list[str]:
    """The named signals one account shows, in `WEIGHTS` order."""
    found = {
        "telegram_flag": profile.get("flagged") is not None,
        "online_cluster": profile["user_id"] in clustered,
        "new_account": profile["user_id"] >= NEW_ACCOUNT_ID,
        "open_last_seen": profile.get("was_online") is not None,
        "generated_username": bool(
            profile.get("username")
            and GENERATED_USERNAME.search(profile["username"].lower())
        ),
        "no_photo": not profile.get("has_photo"),
    }
    return [name for name in WEIGHTS if found[name]]


def verdict_of(profile: dict, score: int) -> str:
    """`deleted` stands apart: a deleted account has no profile left to
    score, so its zero means "unknowable", not "clean"."""
    if profile.get("deleted"):
        return "deleted"
    if score >= LIKELY:
        return "likely"
    if score >= POSSIBLE:
        return "possible"
    return "clean"


def score_accounts(profiles: list[dict]) -> list[dict]:
    """Each profile with its `signals`, `score` and `verdict` added."""
    clustered = online_clusters(profiles)
    scored = []
    for p in profiles:
        signals = [] if p.get("deleted") else signals_of(p, clustered)
        score = sum(WEIGHTS[s] for s in signals)
        scored.append(
            {**p, "signals": signals, "score": score,
             "verdict": verdict_of(p, score)}
        )
    return scored


def _store(conn, events: list[GroupEvent], accounts: list[dict], audit_date: str):
    conn.executemany(
        """
        INSERT INTO subscriber_events (id, date, kind, via, user_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id, user_id) DO UPDATE SET
            date = excluded.date, kind = excluded.kind, via = excluded.via
        """,
        [(e.id, e.date, e.kind, e.via, e.user_id)
         for e in events if e.user_id is not None],
    )
    conn.executemany(
        """
        INSERT INTO subscriber_profiles (
            user_id, audit_date, name, username, has_photo, premium, deleted,
            flagged, status, was_online, signals, score
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (a["user_id"], audit_date, a["name"], a["username"],
             int(a["has_photo"]), int(a["premium"]), int(a["deleted"]),
             a["flagged"], a["status"], a["was_online"],
             ",".join(a["signals"]), a["score"])
            for a in accounts
        ],
    )
    conn.commit()


async def audit_subscribers_with_client(
    client: TelegramClient, entity, channel: str, output_dir: Path
) -> AuditResult:
    """One audit over an already-connected client and a resolved channel.

    Scores the accounts that **joined** within the log and have not left
    since: someone who came and went is churn, not a subscriber to remove.
    The ones who left are counted, and their events stored, all the same."""
    audit_date = datetime.now(UTC).isoformat()
    events, users = await fetch_membership_log(client, entity, channel)

    # Latest event per user decides whether they are still here.
    last: dict[int, GroupEvent] = {}
    joined_at: dict[int, str | None] = {}
    for e in sorted(events, key=lambda e: e.date or ""):
        if e.user_id is None:
            continue
        last[e.user_id] = e
        if e.kind == "join":
            joined_at[e.user_id] = e.date
    present = [
        uid for uid, e in last.items() if e.kind == "join" and uid in users
    ]
    profiles = [
        {**profile_of(users[uid]), "joined": joined_at[uid]} for uid in present
    ]
    accounts = sorted(
        score_accounts(profiles), key=lambda a: (-a["score"], a["joined"] or "")
    )

    with closing(open_db(output_dir, channel)) as conn:
        _store(conn, events, accounts, audit_date)
    log.info(
        "audited %d joiner(s), stored %d event(s) in %s",
        len(accounts), len(events), db_path_for(output_dir, channel),
    )

    dates = sorted(e.date for e in events if e.date)
    overview = {
        "window": (dates[0], dates[-1]) if dates else None,
        "joins": sum(1 for e in events if e.kind == "join"),
        "leaves": sum(1 for e in events if e.kind == "leave"),
        "left_again": sum(
            1 for uid, e in last.items()
            if e.kind == "leave" and uid in joined_at
        ),
    }
    return AuditResult(channel, overview, accounts)


async def audit_subscribers(
    channel: str, output_dir: Path, session_file: str
) -> AuditResult:
    """`audit_subscribers_with_client` with a session opened around it —
    the handle resolves before anything opens the database."""
    async with channel_session(session_file, channel) as (client, entity):
        return await audit_subscribers_with_client(
            client, entity, channel, output_dir
        )
