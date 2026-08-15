"""Telegram's broadcast-stats API: subscriber dynamics and views by hour.

Both paths need admin rights on a channel big enough for Telegram to compute
statistics, and both start from one `GetBroadcastStats` call — hence one
module. Only the subscriber path persists anything; views-by-hour is a console
read (the graph is a rolling window Telegram recomputes, not a series worth
accumulating).
"""

import json
import logging
import sqlite3
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from telethon import TelegramClient
from telethon.tl.functions.stats import (
    GetBroadcastStatsRequest,
    LoadAsyncGraphRequest,
)
from telethon.tl.types import StatsGraph, StatsGraphAsync

from .db import db_path_for, open_db
from .errors import SlopWriterError
from .tg import channel_session

log = logging.getLogger(__name__)


@dataclass
class SubscriberResult:
    """Shapes `render.summarize_subscribers` consumes."""
    channel: str
    rows: dict[str, dict]


@dataclass
class ViewsResult:
    """Shapes `render.summarize_views` consumes."""
    channel: str
    hours: list
    views: list
    period_start: str
    period_end: str


async def load_graph(client: TelegramClient, graph) -> dict | None:
    """Resolve a StatsGraph / StatsGraphAsync into its decoded JSON payload."""
    if isinstance(graph, StatsGraphAsync):
        try:
            graph = await client(LoadAsyncGraphRequest(token=graph.token))
        except Exception as e:
            log.error("failed to load async graph (%s)", e)
            return None
    if isinstance(graph, StatsGraph):
        return json.loads(graph.json.data)
    return None


def graph_series(graph: dict) -> tuple[list, dict[str, list]]:
    """Split a decoded stats graph into (x_values, {series_label: values})."""
    x: list = []
    series: dict[str, list] = {}
    names = graph.get("names", {})
    for col in graph["columns"]:
        key, values = col[0], col[1:]
        if key == "x":
            x = values
        else:
            series[names.get(key, key)] = values
    return x, series


def match_series(series: dict[str, list], *keywords: str) -> list | None:
    """Find the series whose label contains any of the keywords."""
    for label, values in series.items():
        if any(k in label.lower() for k in keywords):
            return values
    return None


@asynccontextmanager
async def stats_session(channel: str, session_file: str):
    """Connected client + the channel's BroadcastStats, lifecycle owned here.

    Replaces the old `open_stats`, which handed a live client across the seam
    for the caller to remember to disconnect."""
    async with channel_session(session_file, channel) as (client, entity):
        log.info("authenticated, fetching stats for %s", channel)
        try:
            stats = await client(GetBroadcastStatsRequest(channel=entity))
        except Exception as e:
            raise SlopWriterError(
                f"failed to get stats ({e})",
                hint="You must be an admin of a channel that is large enough "
                "for Telegram to compute statistics.",
                code="NOT_ADMIN",
            ) from None
        yield client, stats


def ms_to_date(ts_ms) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, UTC).date().isoformat()


def _load_subscriber_rows(conn: sqlite3.Connection) -> dict[str, dict]:
    """Reconstruct {date: row} where row carries base fields + 'sources' dict."""
    rows: dict[str, dict] = {}
    for date, total, joins, leaves in conn.execute(
        "SELECT date, total, joins, leaves FROM subscribers"
    ):
        rows[date] = {
            "date": date,
            "total": total,
            "joins": joins,
            "leaves": leaves,
            "sources": {},
        }
    for date, source, joins in conn.execute(
        "SELECT date, source, joins FROM subscriber_sources"
    ):
        if date in rows:
            rows[date]["sources"][source] = joins
    return rows


async def fetch_subscribers(
    channel: str, output_dir: Path, session_file: str
) -> SubscriberResult:
    async with stats_session(channel, session_file) as (client, stats):
        followers = await load_graph(client, stats.followers_graph)
        growth = await load_graph(client, stats.growth_graph)
        sources = await load_graph(client, stats.new_followers_by_source_graph)

    if not followers:
        raise SlopWriterError(
            "no followers graph available for this channel",
            code="NO_DATA",
        )

    x, series = graph_series(followers)
    joined = match_series(series, "join") or [None] * len(x)
    left = match_series(series, "left", "leav", "unsub") or [None] * len(x)

    totals: dict = {}
    if growth:
        gx, gseries = graph_series(growth)
        total_values = next(iter(gseries.values()), [])
        totals = dict(zip(gx, total_values))

    # New followers per source: source_label -> {date -> value}.
    source_data: dict[str, dict[str, object]] = {}
    if sources:
        sx, sseries = graph_series(sources)
        for label, values in sseries.items():
            source_data[label] = {ms_to_date(ts): v for ts, v in zip(sx, values)}

    base_rows: list[tuple] = []
    for i, ts_ms in enumerate(x):
        date = ms_to_date(ts_ms)
        leave = left[i]
        # Telegram reports "left" as a negative delta; emit a positive count.
        if isinstance(leave, (int, float)):
            leave = abs(leave)
        total = totals.get(ts_ms)
        base_rows.append((date, total, joined[i], leave))

    source_rows: list[tuple] = []
    for label, by_date in source_data.items():
        for date, value in by_date.items():
            if value in (None, ""):
                continue
            source_rows.append((date, label, value))

    with closing(open_db(output_dir, channel)) as conn:
        conn.executemany(
            """
            INSERT INTO subscribers (date, total, joins, leaves)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                total  = COALESCE(excluded.total,  subscribers.total),
                joins  = COALESCE(excluded.joins,  subscribers.joins),
                leaves = COALESCE(excluded.leaves, subscribers.leaves)
            """,
            base_rows,
        )
        conn.executemany(
            """
            INSERT INTO subscriber_sources (date, source, joins)
            VALUES (?, ?, ?)
            ON CONFLICT(date, source) DO UPDATE SET
                joins = excluded.joins
            """,
            source_rows,
        )
        conn.commit()
        rows = _load_subscriber_rows(conn)

    log.info(
        "stored %d daily rows, %d source rows in %s",
        len(base_rows),
        len(source_rows),
        db_path_for(output_dir, channel),
    )

    return SubscriberResult(channel, rows)


async def fetch_views_by_hour(channel: str, session_file: str) -> ViewsResult:
    async with stats_session(channel, session_file) as (client, stats):
        graph = await load_graph(client, stats.top_hours_graph)
        period = stats.period

    if not graph:
        raise SlopWriterError(
            "no top-hours graph available for this channel",
            code="NO_DATA",
        )

    hours, series = graph_series(graph)
    views = next(iter(series.values()), [])
    return ViewsResult(
        channel,
        hours,
        views,
        period.min_date.date().isoformat(),
        period.max_date.date().isoformat(),
    )
