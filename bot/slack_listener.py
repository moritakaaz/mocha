"""Slack listener: Socket Mode events plus archive/backfill logic.

The Slack-side event handling and archive model follow the owner's
earlier Slack archive bot (SQLite, thread-safe upserts, backfill via
``conversations.history`` / ``conversations.replies``), adapted here
for the bridge: every ingested message is also normalized and placed
on the persistent forward queue.
"""

from __future__ import annotations

import logging
import threading
import time

from . import store as store_mod
from .normalizer import build_message

log = logging.getLogger("bridge.slack")

# How often the channel-metadata sync runs while the bridge is up.
# It compares the Slack channel list with the store and mirrors
# renames onto the linked Discord channels (see sync_channel_names).
RENAME_SYNC_INTERVAL = 300.0  # seconds


class Bridge:
    """Glue between Slack events, the store, and the forward queue."""

    def __init__(self, web, store, config):
        self.web = web
        self.store = store
        self.config = config
        # Discord client, set by main() after construction. Only the
        # channel rename sync needs it; the listener itself is
        # Slack-facing, so it stays None in Slack-only tests.
        self.discord = None
        self.bot_user_id: str | None = None
        self._user_cache: dict[str, dict | None] = {}
        self._channel_cache: dict[str, str] = {}
        self._rename_sync_lock = threading.Lock()

    # -- identity / lookups ------------------------------------------
    def auth_check(self):
        res = self.web.auth_test()
        self.bot_user_id = res["user_id"]
        log.info("Authenticated to Slack as bot user %s (team %s)",
                 self.bot_user_id, res.get("team"))
        return res

    def get_user_row(self, user_id: str) -> dict | None:
        if user_id in self._user_cache:
            return self._user_cache[user_id]
        row: dict | None = None
        try:
            info = self.web.users_info(user=user_id)["user"]
            profile = info.get("profile", {})
            self.store.upsert_user(
                user_id, info.get("name"),
                profile.get("display_name") or profile.get("real_name"),
                profile.get("real_name"),
                profile.get("image_72"))
            row = {"name": info.get("name"),
                   "display_name": profile.get("display_name"),
                   "real_name": profile.get("real_name"),
                   "avatar_url": profile.get("image_72")}
        except Exception as exc:  # noqa: BLE001 - best-effort lookup
            log.warning("users_info %s failed: %s", user_id, exc)
        self._user_cache[user_id] = row
        return row

    def channel_label(self, channel_id: str) -> str:
        if channel_id in self._channel_cache:
            return self._channel_cache[channel_id]
        row = self.store.get_channel(channel_id)
        if row and row["name"]:
            self._channel_cache[channel_id] = row["name"]
            return row["name"]
        name = channel_id
        try:
            info = self.web.conversations_info(channel=channel_id)
            channel = info["channel"]
            name = channel.get("name") or channel_id
            self.store.upsert_channel(
                channel_id, name, channel.get("topic", {}).get("value"),
                bool(channel.get("is_private")))
        except Exception as exc:  # noqa: BLE001 - best-effort lookup
            log.warning("conversations_info %s failed: %s", channel_id, exc)
        self._channel_cache[channel_id] = name
        return name

    # -- member channels ----------------------------------------------
    def member_channels(self) -> list[dict]:
        channels, cursor = [], None
        while True:
            resp = self.web.conversations_list(
                types="public_channel,private_channel", limit=1000,
                cursor=cursor)
            for channel in resp.get("channels", []):
                if channel.get("is_member"):
                    channels.append(channel)
                    self.store.upsert_channel(
                        channel["id"], channel.get("name"),
                        (channel.get("topic") or {}).get("value"),
                        bool(channel.get("is_private")))
            cursor = (resp.get("response_metadata") or {}).get(
                "next_cursor")
            if not cursor:
                break
        return channels

    # -- channel rename sync --------------------------------------------
    def _list_slack_channels(self) -> list[dict]:
        """List every channel Slack reports for the workspace (paged)."""
        channels, cursor = [], None
        while True:
            resp = self.web.conversations_list(
                types="public_channel,private_channel", limit=1000,
                cursor=cursor)
            channels.extend(resp.get("channels", []))
            cursor = (resp.get("response_metadata") or {}).get(
                "next_cursor")
            if not cursor:
                break
        return channels

    def _pinned_discord_name(self, channel_id: str,
                             old_name: str | None,
                             new_name: str) -> str | None:
        """Return the Discord name a manual rule pins this channel to.

        A config rule may key the channel by ID, by its old name, or
        by its new name; any of them counts. Returns None when no rule
        pins the channel to a Discord *name* — a rule whose Discord
        ref is a channel ID pins which channel is used, not what it
        is called, so renames still apply to it.
        """
        for ref in (channel_id, old_name, new_name):
            if not ref:
                continue
            rule = self.config.rule_for_slack(ref)
            if rule is None or self.config.looks_like_id(rule.discord):
                continue
            return rule.discord.lstrip("#").strip()
        return None

    def sync_channel_names(self, discord=None) -> int:
        """Mirror Slack channel renames onto their Discord channels.

        Slack is the source of truth for channel names, but there is
        no rename event this bridge subscribes to (detecting renames
        this way needs no extra Slack app subscription), so the
        workspace channel list is compared with the store: a known
        channel whose linked Discord channel carries a different
        name is renamed in Discord to match, and both stored names
        are updated. A manual config rule pinning the channel to a
        different Discord name wins — only the stored Slack-side
        name is refreshed then.

        Detection keys off the stored ``discord_name`` (which only
        the bridge ever changes) rather than the Slack-side ``name``,
        because the startup backfill refreshes the latter anyway.

        Runs once at startup (from run_backfill_all) and then every
        RENAME_SYNC_INTERVAL seconds from the main loop. Failures are
        logged as warnings and retried on the next pass; a pass never
        raises and never blocks message flow. New channels are not
        discovered here — auto-join and backfill own discovery.
        Returns the number of Discord channels renamed.
        """
        discord = discord if discord is not None else self.discord
        if discord is None:
            return 0
        if not self._rename_sync_lock.acquire(blocking=False):
            return 0  # a previous pass is still running
        try:
            return self._sync_channel_names(discord)
        finally:
            self._rename_sync_lock.release()

    def _sync_channel_names(self, discord) -> int:
        try:
            known = {row["id"]: row
                     for row in self.store.list_channels()}
        except Exception:  # noqa: BLE001 - a sync must never crash us
            log.exception("Rename sync could not read the channel "
                          "store")
            return 0
        try:
            listed = self._list_slack_channels()
        except Exception as exc:  # noqa: BLE001
            log.warning("Rename sync could not list Slack channels: "
                        "%s", exc)
            return 0
        renamed = 0
        for channel in listed:
            try:
                if self._sync_one_channel_name(discord, known, channel):
                    renamed += 1
            except Exception:  # noqa: BLE001 - keep the pass going
                log.exception("Rename sync failed for channel %s",
                              channel.get("id"))
        if renamed:
            log.info("Rename sync: renamed %d Discord channel(s) to "
                     "match Slack", renamed)
        return renamed

    def _sync_one_channel_name(self, discord, known,
                               channel: dict) -> bool:
        """Sync one channel's names; True when Discord was renamed."""
        channel_id = channel.get("id")
        new_name = channel.get("name")
        row = known.get(channel_id) if channel_id else None
        if row is None or not new_name:
            return False  # unknown here; discovery is not this sync's job
        old_name = row["name"]
        # Always refresh the stored Slack-side metadata.
        self.store.upsert_channel(
            channel_id, new_name,
            (channel.get("topic") or {}).get("value"),
            bool(channel.get("is_private")))
        self._channel_cache[channel_id] = new_name
        discord_id = row["discord_id"]
        if not discord_id:
            return False  # not linked to a Discord channel (yet)
        current = row["discord_name"]
        if current and current.lower() == new_name.lower():
            return False  # already in sync
        pinned = self._pinned_discord_name(channel_id, old_name,
                                           new_name)
        if pinned and pinned.lower() != new_name.lower():
            log.info("Slack channel #%s renamed to #%s, but config "
                     "pins it to Discord #%s; leaving the Discord "
                     "name alone", old_name, new_name, pinned)
            return False
        try:
            discord.rename_channel(discord_id, new_name)
        except Exception as exc:  # noqa: BLE001 - retried next pass
            log.warning("Could not rename Discord channel %s to #%s "
                        "(Slack channel renamed from #%s): %s; will "
                        "retry on the next sync",
                        discord_id, new_name, old_name, exc)
            return False
        self.store.set_channel_discord(channel_id, discord_id,
                                       new_name)
        log.info("Slack channel #%s renamed to #%s; renamed the "
                 "linked Discord channel %s to match",
                 old_name, new_name, discord_id)
        return True

    # -- auto-join public channels --------------------------------------
    def _auto_join_enabled(self) -> bool:
        return bool(getattr(self.config, "auto_join_public_channels",
                            True))

    @staticmethod
    def _is_missing_scope(exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        error = ""
        if isinstance(response, dict):
            error = str(response.get("error") or "")
        return "missing_scope" in error or "missing_scope" in str(exc)

    def _warn_missing_scope_once(self) -> None:
        if not getattr(self, "_auto_join_warned", False):
            self._auto_join_warned = True
            log.warning(
                "Auto-join is unavailable: the Slack app is missing the "
                "channels:join scope, so the bot cannot join public "
                "channels by itself. Add the scope, or invite the bot "
                "to each channel manually with /invite.")
            # Remember so later sweeps skip the doomed API call too.
            self._auto_join_scope_missing = True

    def join_public_channel(self, channel_id: str,
                            name: str = "") -> bool:
        """Join one public channel (needs the channels:join scope)."""
        try:
            self.web.conversations_join(channel=channel_id)
        except Exception as exc:  # noqa: BLE001 - logged, never raised
            if self._is_missing_scope(exc):
                self._warn_missing_scope_once()
            else:
                log.warning("Auto-join failed for channel %s: %s",
                            name or channel_id, exc)
            return False
        log.info("Auto-joined public channel #%s (%s)",
                 name or self.channel_label(channel_id), channel_id)
        return True

    def auto_join_public_channels(self) -> None:
        """Join every public channel the bot is not a member of yet.

        Private channels are never joined here: Slack only lets a bot
        enter a private channel when a member invites it. Failures are
        logged and skipped so a missing scope or one bad channel can
        never stop the bridge from starting.
        """
        if not self._auto_join_enabled():
            log.info("Auto-join of public channels is disabled "
                     "(AUTO_JOIN_PUBLIC_CHANNELS is off)")
            return
        if getattr(self, "_auto_join_scope_missing", False):
            log.debug("Auto-join skipped: channels:join scope missing")
            return
        channels, cursor = [], None
        try:
            while True:
                resp = self.web.conversations_list(
                    types="public_channel", limit=1000, cursor=cursor)
                channels.extend(resp.get("channels", []))
                cursor = (resp.get("response_metadata") or {}).get(
                    "next_cursor")
                if not cursor:
                    break
        except Exception as exc:  # noqa: BLE001
            if self._is_missing_scope(exc):
                self._warn_missing_scope_once()
            else:
                log.warning("Auto-join could not list public channels: "
                            "%s", exc)
            return
        joined = 0
        for channel in channels:
            if channel.get("is_member"):
                continue
            if not self.join_public_channel(
                    channel["id"], channel.get("name") or ""):
                if getattr(self, "_auto_join_warned", False):
                    break  # missing scope: later joins would fail too
                continue
            joined += 1
        if joined:
            log.info("Auto-join finished: joined %d public channel(s)",
                     joined)

    # -- ingest: archive + normalize + enqueue --------------------------
    def ingest_message(self, channel_id: str, msg: dict) -> bool:
        """Archive one Slack message and queue it for Discord."""
        self.store.store_message(channel_id, msg)
        bridge_msg = build_message(
            msg, channel_id, get_user=self.get_user_row,
            name_format=self.config.name_format,
            resolve_channel=self.channel_label)
        if bridge_msg is None:
            return False
        payload = {
            "sender_name": bridge_msg.sender_name,
            "sender_avatar": bridge_msg.sender_avatar,
            "text": bridge_msg.text,
            "thread_ts": bridge_msg.thread_ts,
            "files": [{"name": f.name, "size": f.size,
                       "mimetype": f.mimetype, "url": f.url}
                      for f in bridge_msg.files],
        }
        self.store.enqueue(store_mod.POST, channel_id, bridge_msg.ts,
                           payload)
        return True

    # -- event handling --------------------------------------------------
    def handle_event(self, event: dict) -> None:
        event_type = event.get("type")
        if event_type == "message":
            self._handle_message(event)
        elif event_type == "member_joined_channel":
            if event.get("user") == self.bot_user_id:
                channel = event.get("channel")
                log.info("Bot joined channel %s; backfilling", channel)
                self.store.upsert_channel(channel)
                from .backfill import backfill_channel
                threading.Thread(
                    target=backfill_channel,
                    args=(self.web, self.store, channel,
                          lambda msg: self.ingest_message(channel, msg)),
                    daemon=True).start()
        elif event_type == "channel_created":
            info = event.get("channel") or {}
            channel = info.get("id")
            if channel:
                self.store.upsert_channel(channel, info.get("name"))
                # channel_created only fires for public channels; a bot
                # join produces member_joined_channel, which then
                # triggers backfill of the new channel.
                if self._auto_join_enabled() and not info.get(
                        "is_private"):
                    self.join_public_channel(channel, info.get("name"))

    def _handle_message(self, event: dict) -> None:
        subtype = event.get("subtype")
        channel = event.get("channel")
        if not channel:
            return
        if subtype == "message_changed":
            msg = event.get("message") or {}
            if msg.get("ts"):
                self.store.store_message(channel, msg, edited=True)
                self.store.enqueue(store_mod.EDIT, channel, msg["ts"],
                                   {"sender_name": ""})
            return
        if subtype == "message_deleted":
            deleted_ts = event.get("deleted_ts")
            if deleted_ts:
                self.store.mark_deleted(channel, deleted_ts)
                self.store.enqueue(store_mod.DELETE, channel, deleted_ts,
                                   {})
            return
        user = event.get("user")
        if user and user == self.bot_user_id:
            return  # never bridge the bot's own messages
        self.ingest_message(channel, event)

    # -- socket mode plumbing --------------------------------------------
    def start_socket_mode(self):
        from slack_sdk import WebClient
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse

        web = WebClient(token=self.config.slack_bot_token)
        self.web = web
        client = SocketModeClient(
            app_token=self.config.slack_app_token, web_client=web)
        client.socket_mode_request_listeners.append(
            self._make_request_handler())
        self.auth_check()
        self.auto_join_public_channels()
        client.connect()
        log.info("Socket Mode connected; bridging Slack to Discord")
        return client

    def _make_request_handler(self):
        from slack_sdk.socket_mode.response import SocketModeResponse

        def _on_request(client, req):
            try:
                if req.type == "events_api":
                    client.send_socket_mode_response(
                        SocketModeResponse(envelope_id=req.envelope_id))
                    self.handle_event(req.payload.get("event", {}))
                elif req.type in ("disconnect", "hello"):
                    log.info("Socket Mode event: %s", req.type)
            except Exception:  # noqa: BLE001 - never kill the listener
                log.exception("Error handling Slack request")

        return _on_request

    def run_backfill_all(self) -> None:
        from .backfill import backfill_channel
        try:
            # Catch channel renames made while the bridge was down.
            # This runs before member_channels() refreshes the stored
            # Slack-side names, so the sync can still see the old
            # names (a manual rule may be keyed by one). The sync
            # never raises.
            self.sync_channel_names()
            channels = self.member_channels()
            log.info("Backfill starting: member of %d channel(s)",
                     len(channels))
            for channel in channels:
                try:
                    channel_id = channel["id"]
                    backfill_channel(
                        self.web, self.store, channel_id,
                        lambda msg, c=channel_id:
                        self.ingest_message(c, msg))
                except Exception:  # noqa: BLE001
                    log.warning("Backfill failed for %s",
                                channel.get("name"))
                time.sleep(0.2)
            log.info("Backfill pass finished; queue depth: %s",
                     self.store.queue_counts())
        except Exception:  # noqa: BLE001
            log.exception("Backfill crashed")
