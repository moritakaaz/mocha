"""Forwarder: drains the persistent queue into Discord.

One worker thread drains the queue; the store's selection rule
keeps posts strictly ordered per channel even when individual
items are deferred by rate limits (a deferred post blocks only
later posts of its own channel, never other channels). Discord
``429`` responses defer the
affected item (``Retry-After``, floored at one second, plus a small
random jitter) without consuming any of its failure attempts; other
failures use exponential backoff and eventually mark the item failed
(never silently dropped).

With ``impersonate`` enabled in config.yaml the forwarder also
manages its own per-channel Discord webhooks (adopting or creating
one named ``Slack Bridge`` and remembering its URL in the store),
so messages carry the Slack sender's name and avatar without any
manually pasted webhook URLs.
"""

from __future__ import annotations

import json
import logging
import random
import time

import requests

from . import store as store_mod
from .discord_client import DiscordError, DiscordRateLimit
from .normalizer import convert_text

log = logging.getLogger("bridge.forwarder")

DISCORD_MAX_CONTENT = 2000
_BASE_BACKOFF = 5.0    # seconds, doubled per attempt
_MAX_BACKOFF = 300.0   # seconds
AUTO_WEBHOOK_NAME = "Slack Bridge"  # webhook the bridge manages itself


def chunk_text(text: str, limit: int = DISCORD_MAX_CONTENT) -> list[str]:
    """Split long content into Discord-sized chunks."""
    if len(text) <= limit:
        return [text] if text else []
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks or [text[:limit]]


class Forwarder:
    """Queue worker. Duck-typed collaborators keep it testable."""

    def __init__(self, store, discord, config, download_file=None):
        self.store = store
        self.discord = discord
        self.config = config
        # download_file(url) -> bytes, using the Slack bot token.
        self.download_file = download_file
        self._guild_channels_cache: list[dict] | None = None
        # slack_channel_id -> auto-managed webhook URL. Avoids a
        # store/API lookup for every single message.
        self._webhook_cache: dict[str, str] = {}

    # --- channel resolution ----------------------------------------
    def _guild_text_channels(self) -> list[dict]:
        if self._guild_channels_cache is None:
            channels = self.discord.list_guild_channels(
                self.config.discord_guild_id)
            self._guild_channels_cache = [
                ch for ch in channels if ch.get("type") == 0]
        return self._guild_channels_cache

    def resolve_discord_channel(self, slack_channel_id: str) -> str | None:
        """Return the Discord channel ID paired with a Slack channel."""
        row = self.store.get_channel(slack_channel_id)
        if row and row["discord_id"]:
            return row["discord_id"]
        slack_name = row["name"] if row else slack_channel_id
        topic = row["topic"] if row else ""
        ref = self.config.discord_ref_for(slack_name) or slack_name
        if self.config.looks_like_id(ref):
            self.store.set_channel_discord(slack_channel_id, ref, ref)
            return ref
        for channel in self._guild_text_channels():
            if channel.get("name", "").lower() == ref.lower():
                self.store.set_channel_discord(
                    slack_channel_id, channel["id"], channel["name"])
                return channel["id"]
        if self.config.auto_create_channels:
            # Recreate the channel (with its topic) like Slackord does.
            created = self.discord.create_guild_channel(
                self.config.discord_guild_id, ref, topic or "")
            self._guild_channels_cache = None
            self.store.set_channel_discord(
                slack_channel_id, created["id"], created["name"])
            log.info("Created Discord channel #%s for Slack #%s",
                     ref, slack_name)
            return created["id"]
        log.warning("No Discord channel paired with Slack channel %s "
                    "(ref %s); create it or enable AUTO_CREATE_CHANNELS",
                    slack_name, ref)
        return None

    # --- webhook resolution (sender impersonation) -----------------
    def _resolve_webhook(self, slack_channel_id: str,
                         discord_channel_id: str,
                         row) -> tuple[str | None, bool]:
        """Return ``(webhook_url, is_auto_managed)`` for a channel.

        An explicit per-channel ``webhook:`` URL in config.yaml wins.
        Otherwise, when ``impersonate`` is enabled, the bridge manages
        its own webhook: a URL remembered in the store is reused;
        failing that, an existing webhook named ``Slack Bridge`` in
        the Discord channel is adopted; failing that, a new one is
        created and its URL stored. Any failure along the way yields
        no webhook (the caller falls back to a normal bot post) —
        webhook management must never break the worker.
        """
        slack_name = row["name"] if row else slack_channel_id
        explicit = self.config.webhook_for(slack_name)
        if explicit:
            return explicit, False
        if not self.config.impersonate:
            return None, False
        cached = self._webhook_cache.get(slack_channel_id)
        if cached:
            return cached, True
        try:
            stored = self.store.get_channel_webhook(slack_channel_id)
        except Exception:  # noqa: BLE001 - fall through to discovery
            stored = None
        if stored:
            self._webhook_cache[slack_channel_id] = stored
            return stored, True
        try:
            for hook in self.discord.list_channel_webhooks(
                    discord_channel_id):
                if hook.get("name") == AUTO_WEBHOOK_NAME \
                        and hook.get("url"):
                    self._remember_webhook(slack_channel_id,
                                           hook["url"])
                    return hook["url"], True
            created = self.discord.create_webhook(
                discord_channel_id, AUTO_WEBHOOK_NAME)
            url = created.get("url") if isinstance(created, dict) \
                else None
            if url:
                self._remember_webhook(slack_channel_id, url)
                log.info("Created webhook %s in Discord channel %s",
                         AUTO_WEBHOOK_NAME, discord_channel_id)
                return url, True
        except Exception as exc:  # noqa: BLE001 - degrade to bot posts
            log.warning("Webhook setup for Slack channel %s failed "
                        "(%s); posting as the bot instead",
                        slack_channel_id, exc)
        return None, False

    def _remember_webhook(self, slack_channel_id: str,
                          url: str) -> None:
        self._webhook_cache[slack_channel_id] = url
        try:
            self.store.set_channel_webhook(slack_channel_id, url)
        except Exception:  # noqa: BLE001 - cache still helps this run
            log.warning("Could not store the webhook URL for Slack "
                        "channel %s", slack_channel_id)

    def _forget_webhook(self, slack_channel_id: str) -> None:
        """Drop a dead auto-managed webhook URL (store and cache)."""
        self._webhook_cache.pop(slack_channel_id, None)
        try:
            self.store.set_channel_webhook(slack_channel_id, None)
        except Exception:  # noqa: BLE001 - best effort
            pass

    # --- payload helpers ----------------------------------------------
    def _sender_of(self, payload: dict) -> tuple[str, str]:
        return (payload.get("sender_name") or "unknown",
                payload.get("sender_avatar") or "")

    def _format_content(self, payload: dict, text: str,
                        use_webhook: bool) -> str:
        sender, _ = self._sender_of(payload)
        if use_webhook:
            return text
        prefix = f"**{sender}:**"
        return f"{prefix} {text}" if text else prefix

    def _collect_files(self, payload: dict) -> tuple[list[dict], list[str]]:
        """Download relayable files; return (attachments, text notes)."""
        attachments, notes = [], []
        max_bytes = int(self.config.max_file_mb * 1024 * 1024)
        for slack_file in payload.get("files") or []:
            name = slack_file.get("name") or "file"
            size = int(slack_file.get("size") or 0)
            if size > max_bytes or not slack_file.get("url"):
                notes.append(f"[File: {name} - too large to relay "
                             f"({size // 1024} KB, limit "
                             f"{self.config.max_file_mb:g} MB)]")
                continue
            if self.download_file is None:
                notes.append(f"[File: {name}]")
                continue
            try:
                data = self.download_file(slack_file["url"])
            except Exception as exc:  # noqa: BLE001 - relay is best-effort
                log.warning("Download of Slack file %s failed: %s",
                            name, exc)
                notes.append(f"[File: {name} - download failed]")
                continue
            attachments.append({"name": name, "data": data,
                                "mimetype": slack_file.get("mimetype")
                                or ""})
        return attachments, notes

    def _resolve_user_name(self, user_id: str) -> str | None:
        """Resolve a Slack user ID to a displayable name via the store.

        Returns the display name first, then the real name, then the
        username; None when the user is unknown, so convert_text can
        fall back to the label embedded in the Slack markup.
        """
        if not user_id:
            return None
        get_user = getattr(self.store, "get_user", None)
        if get_user is None:
            return None
        try:
            row = get_user(user_id)
        except Exception:  # noqa: BLE001 - resolution is best-effort
            return None
        if row is None:
            return None
        if hasattr(row, "keys"):  # sqlite3.Row or dict-like
            data = dict(row)
        elif isinstance(row, dict):
            data = row
        else:
            return None
        return (data.get("display_name") or data.get("real_name")
                or data.get("name"))

    def _convert_stored_text(self, text: str) -> str:
        """Convert raw Slack text from the archive to Discord markdown.

        The archive intentionally keeps the original Slack text; the
        conversion to Discord markdown happens at send time so the
        freshest stored version (including late edits) is always the
        one that gets converted.
        """
        return convert_text(text or "",
                            resolve_user=self._resolve_user_name)

    # --- queue processing ------------------------------------------------
    def process_next(self, now: float | None = None) -> bool:
        """Process one due queue item. Returns True when one was handled."""
        item = self.store.next_due(now)
        if item is None:
            return False
        try:
            payload = json.loads(item["payload_json"] or "{}")
            if item["kind"] == store_mod.POST:
                self._do_post(item, payload)
            elif item["kind"] == store_mod.EDIT:
                self._do_edit(item, payload)
            elif item["kind"] == store_mod.DELETE:
                self._do_delete(item, payload)
            else:
                raise ValueError(f"unknown queue kind {item['kind']}")
            self.store.mark_done(item["id"])
        except DiscordRateLimit as exc:
            # A 429 is Discord asking us to slow down, not a failure:
            # defer without consuming an attempt. Discord's
            # retry_after can be a fraction of a second during a
            # burst, so floor it at 1s and add jitter so items do not
            # all come due at the same instant again.
            delay = max(exc.retry_after, 1.0) + random.uniform(0, 0.5)
            will_retry = self.store.mark_retry(
                item["id"], delay, str(exc), count_attempt=False)
            log.warning("Queue item %s rate limited; %s", item["id"],
                        "will retry" if will_retry else "now failed")
        except DiscordError as exc:
            if exc.status in (404, 403) and item["kind"] in (
                    store_mod.EDIT, store_mod.DELETE):
                # Target message is gone; the sync goal is already met.
                self.store.mark_done(item["id"])
            else:
                self._backoff(item, str(exc))
        except _Defer as exc:
            self.store.mark_retry(item["id"], 5.0, str(exc))
        except _NoChannel as exc:
            # A config problem that retries cannot fix: fail loudly so
            # the owner sees it in the logs and in the queue state.
            self.store.mark_failed(item["id"], f"no channel pairing: {exc}")
            log.error("Queue item %s failed: no Discord channel paired "
                      "with Slack channel %s", item["id"], exc)
        except Exception as exc:  # noqa: BLE001 - worker must survive
            log.exception("Queue item %s failed unexpectedly", item["id"])
            self._backoff(item, str(exc))
        return True

    def _backoff(self, item, error: str) -> None:
        delay = min(_BASE_BACKOFF * (2 ** item["attempts"]), _MAX_BACKOFF)
        will_retry = self.store.mark_retry(item["id"], delay, error)
        log.warning("Queue item %s error (%s); %s", item["id"], error,
                    f"retry in {delay:.0f}s" if will_retry
                    else "marked failed")

    def run_forever(self, stop_event, poll_seconds: float = 1.0) -> None:
        log.info("Forwarder worker started")
        while not stop_event.is_set():
            try:
                if not self.process_next():
                    stop_event.wait(poll_seconds)
            except Exception:  # noqa: BLE001 - never kill the worker
                log.exception("Forwarder loop error")
                stop_event.wait(poll_seconds)
        log.info("Forwarder worker stopped")

    # --- operations -----------------------------------------------------
    def _target_for(self, item,
                    payload) -> tuple[str, bool, str | None, bool]:
        """Resolve (channel_id, use_webhook, webhook_url, is_auto).

        ``is_auto`` marks a bridge-managed webhook (impersonate
        mode); only those get the 404 self-healing fallback, because
        only their URLs are ours to forget and recreate.
        """
        channel_id = self.resolve_discord_channel(item["channel_id"])
        if channel_id is None:
            raise _NoChannel(item["channel_id"])
        row = self.store.get_channel(item["channel_id"])
        webhook, auto = self._resolve_webhook(
            item["channel_id"], channel_id, row)
        return channel_id, bool(webhook), webhook, auto

    def _do_post(self, item, payload) -> None:
        # Dedupe at send time as well: already mapped means already sent.
        if self.store.get_discord_map(item["channel_id"],
                                      item["slack_ts"]):
            return
        channel_id, use_webhook, webhook, auto = self._target_for(
            item, payload)
        sender, avatar = self._sender_of(payload)

        # Always post the latest stored text (an edit may have landed
        # while this item waited in the queue). The archive keeps raw
        # Slack text, so convert it here; the payload fallback is
        # already converted at enqueue time and stays untouched.
        stored = self.store.get_message(item["channel_id"],
                                        item["slack_ts"])
        if stored:
            raw_text = self._convert_stored_text(stored["text"])
        else:
            raw_text = payload.get("text", "")
        attachments, notes = self._collect_files(payload)
        text = raw_text
        if notes:
            text = (text + "\n" if text else "") + "\n".join(notes)

        # Thread replies go into the parent's Discord thread.
        target_channel = channel_id
        thread_id = None
        parent_ts = payload.get("thread_ts")
        if parent_ts and parent_ts != item["slack_ts"]:
            parent = self.store.get_discord_map(item["channel_id"],
                                                parent_ts)
            if parent is None:
                raise _Defer("thread parent not forwarded yet")
            thread_id = parent["discord_thread_id"]
            if not thread_id:
                thread = self.discord.create_thread(
                    channel_id, parent["discord_message_id"],
                    payload.get("thread_name") or "Thread")
                thread_id = thread["id"]
                self.store.set_thread_id(item["channel_id"], parent_ts,
                                         thread_id)
            target_channel = thread_id

        try:
            first_id = self._send_chunks(
                payload, sender, avatar, text, attachments,
                target_channel, thread_id, use_webhook, webhook)
        except DiscordError as exc:
            if not (use_webhook and auto and exc.status == 404):
                raise
            # The auto-managed webhook is gone (its channel was
            # deleted and recreated). Forget it, post this message as
            # the bot so nothing is lost, and let the next item
            # recreate the webhook.
            log.warning("Webhook for Slack channel %s answered 404; "
                        "falling back to a bot post and clearing the "
                        "stored URL", item["channel_id"])
            self._forget_webhook(item["channel_id"])
            first_id = self._send_chunks(
                payload, sender, avatar, text, attachments,
                target_channel, thread_id, False, None)
        self.store.set_discord_map(item["channel_id"], item["slack_ts"],
                                   first_id,
                                   thread_id if thread_id else None)

    def _send_chunks(self, payload, sender, avatar, text, attachments,
                     target_channel, thread_id, use_webhook,
                     webhook) -> str:
        """Post every chunk; return the first Discord message ID."""
        content = self._format_content(payload, text, use_webhook)
        chunks = chunk_text(content) or [content]
        first_id = None
        for index, chunk in enumerate(chunks):
            files = attachments if index == 0 else None
            if use_webhook:
                result = self.discord.webhook_execute(
                    webhook, chunk, username=sender, avatar_url=avatar,
                    files=files,
                    thread_id=thread_id if thread_id else None)
            else:
                result = self.discord.create_message(
                    target_channel, chunk, files=files)
            if first_id is None:
                first_id = result["id"]
        return first_id

    def _do_edit(self, item, payload) -> None:
        mapping = self.store.get_discord_map(item["channel_id"],
                                             item["slack_ts"])
        if mapping is None:
            return  # never forwarded; nothing to edit in Discord
        channel_id, use_webhook, webhook, auto = self._target_for(
            item, payload)
        stored = self.store.get_message(item["channel_id"],
                                        item["slack_ts"])
        if stored:
            text = self._convert_stored_text(stored["text"])
        else:
            # The payload text was converted at enqueue time.
            text = payload.get("text", "")
        content = self._format_content(payload, text, use_webhook)
        content = (chunk_text(content) or [""])[0]
        thread_id = mapping["discord_thread_id"]
        if not use_webhook:
            self.discord.edit_message(thread_id or channel_id,
                                      mapping["discord_message_id"],
                                      content)
            return
        try:
            self.discord.webhook_edit(webhook, mapping["discord_message_id"],
                                      content,
                                      thread_id=thread_id or None)
        except DiscordError as exc:
            if not (auto and exc.status == 404):
                raise
            # Dead auto-managed webhook (see _do_post): forget it
            # and apply the edit as the bot instead.
            log.warning("Webhook for Slack channel %s answered 404; "
                        "falling back to a bot edit and clearing the "
                        "stored URL", item["channel_id"])
            self._forget_webhook(item["channel_id"])
            content = self._format_content(payload, text, False)
            content = (chunk_text(content) or [""])[0]
            self.discord.edit_message(thread_id or channel_id,
                                      mapping["discord_message_id"],
                                      content)

    def _do_delete(self, item, payload) -> None:
        mapping = self.store.get_discord_map(item["channel_id"],
                                             item["slack_ts"])
        if mapping is None:
            return  # never forwarded; nothing to delete in Discord
        channel_id, use_webhook, webhook, auto = self._target_for(
            item, payload)
        thread_id = mapping["discord_thread_id"]
        if not use_webhook:
            self.discord.delete_message(thread_id or channel_id,
                                        mapping["discord_message_id"])
            return
        try:
            self.discord.webhook_delete(
                webhook, mapping["discord_message_id"],
                thread_id=thread_id or None)
        except DiscordError as exc:
            if not (auto and exc.status == 404):
                raise
            # Dead auto-managed webhook (see _do_post): forget it
            # and delete as the bot instead.
            log.warning("Webhook for Slack channel %s answered 404; "
                        "falling back to a bot delete and clearing "
                        "the stored URL", item["channel_id"])
            self._forget_webhook(item["channel_id"])
            self.discord.delete_message(thread_id or channel_id,
                                        mapping["discord_message_id"])


class _Defer(Exception):
    """The item cannot run yet but should be retried shortly."""


class _NoChannel(Exception):
    """No Discord channel is paired with the Slack channel."""


def make_slack_downloader(slack_bot_token: str):
    """Return a download_file(url) -> bytes callable for Slack files."""
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {slack_bot_token}"})

    def download(url: str) -> bytes:
        resp = session.get(url, timeout=60)
        resp.raise_for_status()
        return resp.content

    return download
