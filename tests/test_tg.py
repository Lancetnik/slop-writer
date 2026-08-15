"""`resolve_peer` — the one gate every Telegram path goes through, and the
place two very different failures used to answer with the same code.

A handle that names nothing and a handle this account may not see are both
"get_entity raised"; they are not the same problem and they do not have the
same fix. #35 split them, because an agent holding a perfectly good handle was
being told to look for a typo. Everything else about the function is already
exercised through the scrape and scan lifecycles — this file is only the
classification.
"""

import pytest
from telethon.errors import (
    ChannelPrivateError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)

from slop_writer.errors import SlopWriterError
from slop_writer.tg import resolve_peer

from .conftest import run
from .factories import CHANNEL_ID, FakeClient, channel, user

CHANNEL = "@chan"


def resolving(raised: Exception | None = None, entity=None):
    """A client whose `@chan` is either an entity or a refusal."""
    return FakeClient(entities={CHANNEL: raised if raised else entity})


def test_a_peer_this_account_may_not_see_is_not_a_bad_handle():
    """`ChannelPrivateError` is Telegram confirming the peer exists and
    refusing it — the single most direct statement of NOT_A_MEMBER there is."""
    with pytest.raises(SlopWriterError) as exc:
        run(resolve_peer(resolving(ChannelPrivateError(None)), CHANNEL))

    assert exc.value.code == "NOT_A_MEMBER"


def test_the_membership_hint_does_not_send_the_caller_hunting_for_a_typo():
    """The hint is the actionable half, and the whole point of the split: a
    reader who goes back to check the handle wastes the turn."""
    with pytest.raises(SlopWriterError) as exc:
        run(resolve_peer(resolving(ChannelPrivateError(None)), CHANNEL))

    assert "typo" not in (exc.value.hint or "")
    assert "join" in (exc.value.hint or "").lower()


@pytest.mark.parametrize(
    "raised",
    [
        UsernameNotOccupiedError(None),
        UsernameInvalidError(None),
        ValueError("Cannot find any entity corresponding to '@chan'"),
    ],
    ids=["not-occupied", "invalid", "bare-value-error"],
)
def test_a_handle_that_names_nothing_stays_cannot_resolve(raised):
    """The other half. These three are what a typo actually looks like coming
    out of Telethon, and none of them says anything about membership."""
    with pytest.raises(SlopWriterError) as exc:
        run(resolve_peer(resolving(raised), CHANNEL))

    assert exc.value.code == "CANNOT_RESOLVE"


def test_a_resolvable_channel_is_returned_unwrapped():
    """The split must not have made the success path conditional on anything."""
    entity = channel(CHANNEL_ID)
    assert run(resolve_peer(resolving(entity=entity), CHANNEL)) is entity


def test_a_handle_that_resolves_to_a_person_is_still_refused():
    """`NOT_A_CHANNEL` sits after the two resolve failures and is unrelated to
    both — pinned here so a future edit to the except clauses cannot swallow
    it."""
    with pytest.raises(SlopWriterError) as exc:
        run(resolve_peer(resolving(entity=user(7)), CHANNEL))

    assert exc.value.code == "NOT_A_CHANNEL"
