"""Shared fixtures.

Deliberately thin: no network, no session, no `.tg-analytic/`. Everything a
test needs is either an in-memory SQLite DB or a tmp_path the test owns.
"""

import asyncio
import sqlite3

import pytest

from slop_writer.db import SCHEMA


def run(coro):
    """Drive one coroutine to completion.

    Used instead of pytest-asyncio: four async functions in the whole suite
    (`complete_albums` and friends) do not justify a plugin whose `asyncio_mode`
    setting is a standing source of "tests silently skipped" reports.
    """
    return asyncio.run(coro)


@pytest.fixture
def conn():
    """An in-memory DB with `SCHEMA` applied and nothing else.

    No FTS and no `open_db`, so a test of `heal_album_phantoms` exercises the
    healer rather than the open path that also calls it.
    """
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    yield c
    c.close()
