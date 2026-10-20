"""SQLite store: message archive, Discord mapping, persistent queue.

One database file holds everything the bridge needs to stay
idempotent across restarts:

- ``channels`` / ``users`` / ``messages`` (+ FTS5): the local archive.
  Slack-side event, archive and search logic is adapted from the
  owner's earlier Slack archive bot.
- ``discord_map``: Slack ``(channel_id, ts)`` -> Discord message ID.
  This is the dedupe key that prevents double posting.
- ``forward_queue``: persistent post/edit/delete work items with
  retry state, so a restart never loses work.
- ``sync_state``: small key/value store (backfill cursors, flags).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time

log = logging.getLogger("bridge.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY, name TEXT, topic TEXT,
    is_private INTEGER DEFAULT 0, discord_id TEXT, discord_name TEXT,
    webhook_url TEXT,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, name TEXT, display_name TEXT, real_name TEXT,
    avatar_url TEXT, updated_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
    channel_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    thread_ts TEXT,
    user_id TEXT,
    text TEXT,
    posted_at REAL,
    edited INTEGER DEFAULT 0,
    deleted INTEGER DEFAULT 0,
    raw TEXT,
    PRIMARY KEY (channel_id, ts)
);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
    USING fts5(text, content='messages', content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text)
        VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text)
        VALUES ('delete', old.rowid, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TABLE IF NOT EXISTS discord_map (
    channel_id TEXT NOT NULL,
    slack_ts TEXT NOT NULL,
    discord_message_id TEXT NOT NULL,
    discord_thread_id TEXT,
    PRIMARY KEY (channel_id, slack_ts)
);
CREATE TABLE IF NOT EXISTS forward_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    slack_ts TEXT NOT NULL,
    payload_json TEXT DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    deferrals INTEGER DEFAULT 0,
    next_retry_at REAL DEFAULT 0,
    last_error TEXT,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_queue_pending
    ON forward_queue(status, next_retry_at, id);
CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY, value TEXT
);
"""

# Workload states
PENDING, DONE, FAILED = "pending", "done", "failed"
# Queue item kinds
POST, EDIT, DELETE = "post", "edit", "delete"

MAX_ATTEMPTS = 8  # a queue item is marked failed after this many failures

# Consecutive rate-limit (429) deferrals tolerated before an item is
# failed as a safety valve. This is deliberately far above
# MAX_ATTEMPTS: a 429 is Discord asking to slow down, not a failure,
# and a normal burst during a big backfill can defer a message for
# minutes without anything being wrong.
MAX_DEFERRALS = 60


class Store:
    """Thread-safe wrapper around one SQLite connection."""

    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path, timeout=30,
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            # Migration: databases created before the rate-limit fix
            # lack the deferrals counter (see mark_retry).
            cols = [row["name"] for row in self.conn.execute(
                "PRAGMA table_info(forward_queue)")]
            if "deferrals" not in cols:
                self.conn.execute(
                    "ALTER TABLE forward_queue ADD COLUMN"
                    " deferrals INTEGER DEFAULT 0")
            # Migration: databases created before automatic webhook
            # impersonation lack the per-channel webhook URL column.
            chan_cols = [row["name"] for row in self.conn.execute(
                "PRAGMA table_info(channels)")]
            if "webhook_url" not in chan_cols:
                self.conn.execute(
                    "ALTER TABLE channels ADD COLUMN webhook_url TEXT")
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # --- channels / users ------------------------------------------
    def upsert_channel(self, channel_id: str, name: str | None = None,
                       topic: str | None = None,
                       is_private: bool | None = None) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO channels (id, name, topic, is_private,
                       updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       name=COALESCE(excluded.name, channels.name),
                       topic=COALESCE(excluded.topic, channels.topic),
                       is_private=COALESCE(excluded.is_private,
                                            channels.is_private),
                       updated_at=excluded.updated_at""",
                (channel_id, name, topic,
                 None if is_private is None else int(is_private),
                 time.time()))
            self.conn.commit()

    def set_channel_discord(self, channel_id: str, discord_id: str,
                            discord_name: str | None = None) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO channels (id, discord_id, discord_name,
                       updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       discord_id=excluded.discord_id,
                       discord_name=COALESCE(excluded.discord_name,
                                             channels.discord_name)""",
                (channel_id, discord_id, discord_name, time.time()))
            self.conn.commit()

    def get_channel(self, channel_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM channels WHERE id=?", (channel_id,)
            ).fetchone()

    def get_channel_webhook(self, channel_id: str) -> str | None:
        """Return the auto-managed webhook URL stored for a channel."""
        with self._lock:
            row = self.conn.execute(
                "SELECT webhook_url FROM channels WHERE id=?",
                (channel_id,)).fetchone()
        return row["webhook_url"] if row else None

    def set_channel_webhook(self, channel_id: str,
                            webhook_url: str | None) -> None:
        """Store (or clear, with ``None``) a channel's webhook URL."""
        with self._lock:
            self.conn.execute(
                """INSERT INTO channels (id, webhook_url, updated_at)
                   VALUES (?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       webhook_url=excluded.webhook_url""",
                (channel_id, webhook_url, time.time()))
            self.conn.commit()

    def channel_by_name(self, name: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM channels WHERE LOWER(name)=?",
                (name.lstrip("#").lower(),)).fetchone()

    def upsert_user(self, user_id: str, name: str | None = None,
                    display_name: str | None = None,
                    real_name: str | None = None,
                    avatar_url: str | None = None) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO users (id, name, display_name, real_name,
                       avatar_url, updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       name=COALESCE(excluded.name, users.name),
                       display_name=COALESCE(excluded.display_name,
                                              users.display_name),
                       real_name=COALESCE(excluded.real_name,
                                          users.real_name),
                       avatar_url=COALESCE(excluded.avatar_url,
                                           users.avatar_url),
                       updated_at=excluded.updated_at""",
                (user_id, name, display_name, real_name, avatar_url,
                 time.time()))
            self.conn.commit()

    def get_user(self, user_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    # --- messages (archive) -----------------------------------------
    def store_message(self, channel_id: str, msg: dict,
                      edited: bool = False,
                      deleted: bool = False) -> bool:
        """Insert or update one Slack message dict in the archive."""
        ts = msg.get("ts")
        if not ts:
            return False
        text = msg.get("text") or ""
        user_id = msg.get("user") or msg.get("bot_id") or ""
        try:
            posted = float(ts)
        except (TypeError, ValueError):
            posted = None
        with self._lock:
            self.conn.execute(
                """INSERT INTO messages
                   (channel_id, ts, thread_ts, user_id, text, posted_at,
                    edited, deleted, raw)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(channel_id, ts) DO UPDATE SET
                       text=excluded.text,
                       edited=MAX(messages.edited, excluded.edited),
                       deleted=MAX(messages.deleted, excluded.deleted),
                       raw=excluded.raw""",
                (channel_id, ts, msg.get("thread_ts"), user_id, text,
                 posted, 1 if edited else 0, 1 if deleted else 0,
                 json.dumps(msg)[:20000]))
            self.conn.commit()
        return True

    def mark_deleted(self, channel_id: str, ts: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE messages SET deleted=1 WHERE channel_id=? AND ts=?",
                (channel_id, ts))
            self.conn.commit()

    def get_message(self, channel_id: str,
                    ts: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM messages WHERE channel_id=? AND ts=?",
                (channel_id, ts)).fetchone()

    def search_messages(self, query: str, limit: int = 10) -> list:
        terms = [t for t in query.split() if t]
        if not terms:
            return []
        fts_q = " ".join('"' + t.replace('"', '""') + '"' for t in terms)
        with self._lock:
            try:
                return self.conn.execute(
                    """SELECT m.* FROM messages_fts f
                       JOIN messages m ON m.rowid = f.rowid
                       WHERE messages_fts MATCH ? AND m.deleted = 0
                       ORDER BY m.posted_at DESC LIMIT ?""",
                    (fts_q, limit)).fetchall()
            except sqlite3.OperationalError:
                like = "%" + "%".join(terms) + "%"
                return self.conn.execute(
                    """SELECT * FROM messages
                       WHERE text LIKE ? AND deleted = 0
                       ORDER BY posted_at DESC LIMIT ?""",
                    (like, limit)).fetchall()

    # --- Discord mapping ---------------------------------------------
    def set_discord_map(self, channel_id: str, slack_ts: str,
                        discord_message_id: str,
                        discord_thread_id: str | None = None) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO discord_map
                   (channel_id, slack_ts, discord_message_id,
                    discord_thread_id)
                   VALUES (?,?,?,?)
                   ON CONFLICT(channel_id, slack_ts) DO UPDATE SET
                       discord_message_id=excluded.discord_message_id,
                       discord_thread_id=COALESCE(
                           excluded.discord_thread_id,
                           discord_map.discord_thread_id)""",
                (channel_id, slack_ts, discord_message_id,
                 discord_thread_id))
            self.conn.commit()

    def get_discord_map(self, channel_id: str,
                        slack_ts: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                """SELECT * FROM discord_map
                   WHERE channel_id=? AND slack_ts=?""",
                (channel_id, slack_ts)).fetchone()

    def set_thread_id(self, channel_id: str, slack_ts: str,
                      thread_id: str) -> None:
        with self._lock:
            self.conn.execute(
                """UPDATE discord_map SET discord_thread_id=?
                   WHERE channel_id=? AND slack_ts=?""",
                (thread_id, channel_id, slack_ts))
            self.conn.commit()

    # --- forward queue -------------------------------------------------
    def enqueue(self, kind: str, channel_id: str, slack_ts: str,
                payload: dict | None = None) -> tuple[int, bool]:
        """Add a queue item, deduped. Returns ``(id, created)``.

        Dedupe rules:
        - ``post``: skipped when a post item already exists (any status)
          or the message is already mapped to Discord.
        - ``edit``: folds into a still-pending ``post`` (that post reads
          the latest stored text at send time); deduped against an
          already pending or done ``edit``.
        - ``delete``: deduped against an already pending ``delete``.
        """
        with self._lock:
            if kind == POST:
                if self.conn.execute(
                        "SELECT 1 FROM discord_map"
                        " WHERE channel_id=? AND slack_ts=?",
                        (channel_id, slack_ts)).fetchone():
                    return (0, False)
            if kind == EDIT:
                row = self.conn.execute(
                    """SELECT id FROM forward_queue
                       WHERE kind=? AND channel_id=? AND slack_ts=?
                         AND status=? ORDER BY id LIMIT 1""",
                    (POST, channel_id, slack_ts, PENDING)).fetchone()
                if row:
                    return (row["id"], False)
            existing = self.conn.execute(
                """SELECT id, status FROM forward_queue
                   WHERE kind=? AND channel_id=? AND slack_ts=?
                   ORDER BY id LIMIT 1""",
                (kind, channel_id, slack_ts)).fetchone()
            if existing and existing["status"] in (PENDING, DONE):
                return (existing["id"], False)
            cur = self.conn.execute(
                """INSERT INTO forward_queue
                   (kind, channel_id, slack_ts, payload_json, status,
                    attempts, next_retry_at, created_at)
                   VALUES (?,?,?,?,?,0,0,?)""",
                (kind, channel_id, slack_ts,
                 json.dumps(payload or {}), PENDING, time.time()))
            self.conn.commit()
            return (cur.lastrowid, True)

    def next_due(self, now: float | None = None) -> sqlite3.Row | None:
        """Return the oldest pending item that is due for processing.

        Posts are strictly ordered per channel: a ``post`` item is
        never handed out while an earlier ``post`` item for the same
        channel (smaller queue id) is still pending — even when that
        earlier item is only waiting out a rate-limit deferral and is
        not itself due yet. Without this rule, a deferred message is
        overtaken by every later message of its channel during the
        cool-down window and the Discord channel shows the
        conversation scrambled (exactly what a bulk re-import hits,
        because 429s are common there). The constraint lifts as soon
        as the earlier item leaves ``pending`` (done or failed), so a
        stuck head message can delay its own channel but never wedge
        it forever, and other channels are never affected. Edits and
        deletes are not ordered this way; they keep the plain
        oldest-due selection.
        """
        now = time.time() if now is None else now
        with self._lock:
            return self.conn.execute(
                """SELECT q.* FROM forward_queue q
                   WHERE q.status=? AND q.next_retry_at<=?
                     AND NOT (q.kind=? AND EXISTS (
                         SELECT 1 FROM forward_queue earlier
                         WHERE earlier.kind=?
                           AND earlier.channel_id=q.channel_id
                           AND earlier.id<q.id
                           AND earlier.status=?))
                   ORDER BY q.id LIMIT 1""",
                (PENDING, now, POST, POST, PENDING)).fetchone()

    def mark_done(self, item_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE forward_queue SET status=?, last_error=NULL"
                " WHERE id=?", (DONE, item_id))
            self.conn.commit()

    def mark_failed(self, item_id: int, error: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE forward_queue SET status=?, attempts=attempts+1,"
                " last_error=? WHERE id=?", (FAILED, error, item_id))
            self.conn.commit()

    def mark_retry(self, item_id: int, delay_seconds: float,
                   error: str, count_attempt: bool = True) -> bool:
        """Schedule a retry; returns False when the item becomes failed.

        ``count_attempt=True`` (the default) is for real errors: the
        item's attempt counter grows and the item fails after
        MAX_ATTEMPTS.

        ``count_attempt=False`` is for rate-limit (429) deferrals.
        Discord is asking us to slow down, not reporting a failure, so
        a deferral must never consume the failure budget: the attempt
        counter is untouched and this path alone can never fail the
        item before MAX_DEFERRALS *consecutive* deferrals. That cap is
        only a safety valve against a pathological endless 429 loop;
        it is high enough that a normal multi-minute rate-limit storm
        during a big backfill survives. A real (counted) retry resets
        the deferral streak, because an actual Discord response proves
        the 429 storm broke.
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT attempts, deferrals FROM forward_queue WHERE id=?",
                (item_id,)).fetchone()
            now = time.time()
            if not count_attempt:
                deferrals = (row["deferrals"] if row else 0) + 1
                if deferrals > MAX_DEFERRALS:
                    self.conn.execute(
                        "UPDATE forward_queue SET status=?, deferrals=?,"
                        " last_error=? WHERE id=?",
                        (FAILED, deferrals,
                         f"rate-limited {deferrals}x in a row: {error}",
                         item_id))
                    self.conn.commit()
                    return False
                self.conn.execute(
                    "UPDATE forward_queue SET deferrals=?, next_retry_at=?,"
                    " last_error=? WHERE id=?",
                    (deferrals, now + delay_seconds, error, item_id))
                self.conn.commit()
                return True
            attempts = (row["attempts"] if row else 0) + 1
            if attempts >= MAX_ATTEMPTS:
                self.conn.execute(
                    "UPDATE forward_queue SET status=?, attempts=?,"
                    " last_error=? WHERE id=?",
                    (FAILED, attempts, error, item_id))
                self.conn.commit()
                return False
            self.conn.execute(
                "UPDATE forward_queue SET attempts=?, deferrals=0,"
                " next_retry_at=?, last_error=? WHERE id=?",
                (attempts, now + delay_seconds, error, item_id))
            self.conn.commit()
            return True

    def queue_counts(self) -> dict:
        """Return pending/done/failed item counts."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM forward_queue"
                " GROUP BY status").fetchall()
        counts = {PENDING: 0, DONE: 0, FAILED: 0}
        for row in rows:
            counts[row["status"]] = row["n"]
        return counts

    # --- sync state ----------------------------------------------------
    def get_state(self, key: str) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM sync_state WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO sync_state (key, value) VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value))
            self.conn.commit()

    # --- Discord-side reset (re-import from scratch) -------------------
    def discord_state_counts(self) -> dict:
        """Count the Discord-side state a reset would clear.

        Read-only; used for the dry-run summary of
        ``--reset-discord``. The Slack archive (``channels``,
        ``users``, ``messages``) is never counted here beyond the
        channel Discord links that would be dropped.
        """
        with self._lock:
            def _count(sql: str) -> int:
                return self.conn.execute(sql).fetchone()[0]
            return {
                "discord_map": _count(
                    "SELECT COUNT(*) FROM discord_map"),
                "forward_queue": _count(
                    "SELECT COUNT(*) FROM forward_queue"),
                "channel_links": _count(
                    "SELECT COUNT(*) FROM channels"
                    " WHERE discord_id IS NOT NULL"),
                "channel_webhooks": _count(
                    "SELECT COUNT(*) FROM channels"
                    " WHERE webhook_url IS NOT NULL"),
                "backfill_keys": _count(
                    "SELECT COUNT(*) FROM sync_state"
                    " WHERE substr(key, 1, 16) = 'backfill_cursor:'"
                    " OR substr(key, 1, 14) = 'backfill_done:'"),
            }

    def reset_discord_state(self) -> dict:
        """Forget all Discord-side state so the next start re-imports.

        - ``discord_map`` is emptied: the dedupe that stops double
          posting is gone, so the startup backfill may re-post every
          archived message.
        - ``forward_queue`` is emptied: stale payloads hold expiring
          Slack file URLs, and the startup backfill re-enqueues fresh
          payloads from Slack anyway.
        - Channel rows keep their Slack identity but lose their
          Discord link (``discord_id`` / ``discord_name``), so the
          forwarder re-discovers or re-creates same-name channels.
        - Auto-managed webhook URLs are cleared as well: webhooks die
          with their Discord channel, so the forwarder recreates
          them on demand at the next start.
        - Backfill bookkeeping keys in ``sync_state`` are dropped.
          (Backfill always full-scans history and resets its cursor
          at the start of a run, so these keys never gate a re-scan;
          clearing them just leaves a tidy state. Unrelated keys are
          kept.)

        The Slack archive itself — ``messages``, ``users``, and the
        channel rows — is never touched. Returns the counts cleared.
        """
        with self._lock:
            counts = {}
            counts["discord_map"] = self.conn.execute(
                "DELETE FROM discord_map").rowcount
            counts["forward_queue"] = self.conn.execute(
                "DELETE FROM forward_queue").rowcount
            counts["channel_links"] = self.conn.execute(
                "UPDATE channels SET discord_id=NULL, discord_name=NULL"
                " WHERE discord_id IS NOT NULL").rowcount
            counts["channel_webhooks"] = self.conn.execute(
                "UPDATE channels SET webhook_url=NULL"
                " WHERE webhook_url IS NOT NULL").rowcount
            counts["backfill_keys"] = self.conn.execute(
                "DELETE FROM sync_state"
                " WHERE substr(key, 1, 16) = 'backfill_cursor:'"
                " OR substr(key, 1, 14) = 'backfill_done:'").rowcount
            self.conn.commit()
        return counts
