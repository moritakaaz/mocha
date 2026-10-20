"""Backfill engine: pull visible Slack history, oldest to newest.

At startup and whenever the bot joins a new channel, the visible
history (``conversations.history`` plus ``conversations.replies`` for
threads) is ingested. Dedupe in the store and queue makes repeated
backfills safe — the bridge equivalent of Slackord's resume behavior.

Honest limit: on the Slack free plan the API only returns roughly the
last 90 days of history.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("bridge.backfill")

_PAGE_LIMIT = 200
_PAGE_PAUSE = 0.5  # seconds between Slack API pages


def backfill_channel(web, store, channel_id: str, ingest) -> int:
    """Backfill one channel. ``ingest(msg)`` handles one Slack message.

    Returns the number of top-level messages seen.
    """
    store.set_state(f"backfill_cursor:{channel_id}", "")
    seen = 0
    roots: list[str] = []
    collected: list[dict] = []
    cursor = None
    while True:
        resp = web.conversations_history(
            channel=channel_id, limit=_PAGE_LIMIT, cursor=cursor)
        for msg in resp.get("messages", []):
            collected.append(msg)
            seen += 1
            if msg.get("reply_count"):
                roots.append(msg["ts"])
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        store.set_state(f"backfill_cursor:{channel_id}", cursor or "")
        if not cursor:
            break
        time.sleep(_PAGE_PAUSE)
    for root_ts in roots:
        cursor = None
        while True:
            resp = web.conversations_replies(
                channel=channel_id, ts=root_ts, limit=_PAGE_LIMIT,
                cursor=cursor)
            for msg in resp.get("messages", []):
                if msg.get("ts") != root_ts:
                    collected.append(msg)
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
            time.sleep(_PAGE_PAUSE)
    collected.sort(key=lambda m: float(m["ts"]))
    for msg in collected:
        try:
            ingest(msg)
        except Exception:  # noqa: BLE001 - one bad message must not stop us
            log.exception("Backfill ingest failed for message %s",
                          msg.get("ts"))
    store.set_state(f"backfill_done:{channel_id}", str(time.time()))
    log.info("Backfill channel %s: %d messages seen", channel_id, seen)
    return seen
