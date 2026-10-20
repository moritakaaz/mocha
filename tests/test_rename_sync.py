"""Unit tests: Slack channel rename sync onto Discord channels."""

import os
import tempfile
import unittest

from bot.config import ChannelRule, Config
from bot.discord_client import DiscordError
from bot.forwarder import Forwarder
from bot.slack_listener import RENAME_SYNC_INTERVAL, Bridge
from bot.store import POST, Store


class FakeSlackWeb:
    """In-memory stand-in for slack_sdk.WebClient (channel lists)."""

    def __init__(self, channels):
        self.channels = channels  # mutable list of channel dicts
        self.list_calls = 0

    def conversations_list(self, types="", limit=1000, cursor=None):
        self.list_calls += 1
        return {"channels": [dict(c) for c in self.channels],
                "response_metadata": {"next_cursor": ""}}

    def conversations_history(self, channel, limit=200, cursor=None):
        return {"messages": [], "response_metadata": {"next_cursor": ""}}

    def conversations_replies(self, channel, ts, limit=200, cursor=None):
        return {"messages": [], "response_metadata": {"next_cursor": ""}}


class FakeDiscord:
    """Discord fake with rename tracking and a guild channel list."""

    def __init__(self, guild_channels=None):
        self.guild_channels = guild_channels or []
        self.rename_calls = []
        self.rename_error = None
        self.create_calls = 0
        self.messages = {}
        self._counter = 0

    def rename_channel(self, channel_id, name):
        self.rename_calls.append((channel_id, name))
        if self.rename_error:
            raise self.rename_error
        for channel in self.guild_channels:
            if channel["id"] == channel_id:
                channel["name"] = name
        return {"id": channel_id, "name": name}

    def list_guild_channels(self, guild_id):
        return list(self.guild_channels)

    def create_guild_channel(self, guild_id, name, topic=""):
        self.create_calls += 1
        channel = {"id": f"DC_{name}", "name": name, "type": 0}
        self.guild_channels.append(channel)
        return channel

    def create_message(self, channel_id, content, files=None):
        self._counter += 1
        mid = f"D{self._counter}"
        self.messages[mid] = {"channel": channel_id, "content": content}
        return {"id": mid}


def _temp_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return Store(path), path


class RenameSyncTestCase(unittest.TestCase):
    def setUp(self):
        self.store, self.path = _temp_store()
        # The live case: Slack #general (C1) linked to Discord D100,
        # both still carrying the old name in the store.
        self.store.upsert_channel("C1", "general")
        self.store.set_channel_discord("C1", "D100", "general")
        self.web = FakeSlackWeb([
            {"id": "C1", "name": "chit-chat", "is_member": True}])
        self.discord = FakeDiscord(guild_channels=[
            {"id": "D100", "name": "general", "type": 0}])
        self.cfg = Config()

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def _bridge(self):
        return Bridge(self.web, self.store, self.cfg)

    # (a) a detected rename renames the Discord channel exactly once
    def test_rename_detected_renames_discord_and_updates_store(self):
        bridge = self._bridge()
        renamed = bridge.sync_channel_names(self.discord)
        self.assertEqual(renamed, 1)
        self.assertEqual(self.discord.rename_calls,
                         [("D100", "chit-chat")])
        row = self.store.get_channel("C1")
        self.assertEqual(row["name"], "chit-chat")
        self.assertEqual(row["discord_name"], "chit-chat")
        self.assertEqual(self.discord.guild_channels[0]["name"],
                         "chit-chat")
        # Idempotent: a second pass renames nothing.
        self.assertEqual(bridge.sync_channel_names(self.discord), 0)
        self.assertEqual(len(self.discord.rename_calls), 1)

    def test_no_rename_when_names_already_match(self):
        self.web.channels[0]["name"] = "general"
        bridge = self._bridge()
        self.assertEqual(bridge.sync_channel_names(self.discord), 0)
        self.assertEqual(self.discord.rename_calls, [])

    def test_unknown_slack_channel_is_not_discovered_by_sync(self):
        # Requirement: the sync handles renames of known channels
        # only; brand-new channels belong to auto-join/backfill.
        self.web.channels.append(
            {"id": "C2", "name": "brand-new", "is_member": True})
        bridge = self._bridge()
        bridge.sync_channel_names(self.discord)
        self.assertIsNone(self.store.get_channel("C2"))

    def test_sync_without_discord_client_is_a_noop(self):
        bridge = self._bridge()
        self.assertIsNone(bridge.discord)
        self.assertEqual(bridge.sync_channel_names(), 0)
        self.assertEqual(self.web.list_calls, 0)

    # (b) a manual rule pinning a different Discord name wins
    def test_pinned_mapping_by_old_name_blocks_rename(self):
        self.cfg.rules.append(ChannelRule(slack="general",
                                          discord="lounge"))
        self.store.set_channel_discord("C1", "D100", "lounge")
        self.discord.guild_channels[0]["name"] = "lounge"
        bridge = self._bridge()
        self.assertEqual(bridge.sync_channel_names(self.discord), 0)
        self.assertEqual(self.discord.rename_calls, [])
        row = self.store.get_channel("C1")
        # Only the Slack-side name moves; the Discord name stays.
        self.assertEqual(row["name"], "chit-chat")
        self.assertEqual(row["discord_name"], "lounge")

    def test_pinned_mapping_by_channel_id_blocks_rename(self):
        self.cfg.rules.append(ChannelRule(slack="C1",
                                          discord="lounge"))
        self.store.set_channel_discord("C1", "D100", "lounge")
        bridge = self._bridge()
        self.assertEqual(bridge.sync_channel_names(self.discord), 0)
        self.assertEqual(self.discord.rename_calls, [])
        self.assertEqual(self.store.get_channel("C1")["discord_name"],
                         "lounge")

    def test_id_pinned_mapping_does_not_block_rename(self):
        # A rule pinning the Discord channel by ID pins *which*
        # channel is used, not its name: the rename still applies.
        self.cfg.rules.append(ChannelRule(slack="general",
                                          discord="123456789"))
        bridge = self._bridge()
        self.assertEqual(bridge.sync_channel_names(self.discord), 1)
        self.assertEqual(self.discord.rename_calls,
                         [("D100", "chit-chat")])

    # (c) a rename made while the bridge was down is applied at startup
    def test_startup_backfill_applies_missed_rename(self):
        bridge = self._bridge()
        bridge.discord = self.discord
        bridge.run_backfill_all()  # must not raise, must rename first
        self.assertEqual(self.discord.rename_calls,
                         [("D100", "chit-chat")])
        row = self.store.get_channel("C1")
        self.assertEqual(row["name"], "chit-chat")
        self.assertEqual(row["discord_name"], "chit-chat")

    # (e) a Discord failure is swallowed and retried on the next pass
    def test_rename_failure_is_swallowed_and_retried(self):
        self.discord.rename_error = DiscordError(403,
                                                  "Missing Permissions")
        bridge = self._bridge()
        self.assertEqual(bridge.sync_channel_names(self.discord), 0)
        self.assertEqual(len(self.discord.rename_calls), 1)
        row = self.store.get_channel("C1")
        # Slack-side name moved on; the Discord name is still stale,
        # so the next pass retries the rename.
        self.assertEqual(row["name"], "chit-chat")
        self.assertEqual(row["discord_name"], "general")
        # Message flow is unaffected by the failed rename.
        cfg = Config()
        cfg.discord_guild_id = "G1"
        cfg.auto_create_channels = True
        forwarder = Forwarder(self.store, self.discord, cfg)
        self.store.enqueue(POST, "C1", "1.1",
                           {"sender_name": "Ana", "text": "hi",
                            "files": []})
        self.assertTrue(forwarder.process_next())
        mapping = self.store.get_discord_map("C1", "1.1")
        self.assertIsNotNone(mapping)
        self.assertEqual(self.store.queue_counts()["failed"], 0)
        # Once Discord cooperates, the next pass completes the rename.
        self.discord.rename_error = None
        self.assertEqual(bridge.sync_channel_names(self.discord), 1)
        self.assertEqual(self.store.get_channel("C1")["discord_name"],
                         "chit-chat")

    def test_interval_constant_is_sensible(self):
        self.assertGreaterEqual(RENAME_SYNC_INTERVAL, 60)
        self.assertLessEqual(RENAME_SYNC_INTERVAL, 3600)


class PostRenameResolutionTestCase(unittest.TestCase):
    """(d) After a Slack rename, before any sync ran, a message must
    still post to the SAME Discord channel via the stored link."""

    def setUp(self):
        self.store, self.path = _temp_store()
        # Store already carries the NEW Slack name (refreshed by a
        # lookup), the stored link still points at D100, and its
        # stored Discord name is still the old one.
        self.store.upsert_channel("C1", "chit-chat")
        self.store.set_channel_discord("C1", "D100", "general")

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def test_post_after_rename_uses_stored_link_no_duplicate(self):
        cfg = Config()
        cfg.discord_guild_id = "G1"
        cfg.auto_create_channels = True  # even with auto-create on
        discord = FakeDiscord(guild_channels=[
            {"id": "D100", "name": "general", "type": 0},
            # A decoy already carrying the new name: name-based
            # resolution would wrongly pick or duplicate this one.
            {"id": "D999", "name": "chit-chat", "type": 0},
        ])
        forwarder = Forwarder(self.store, discord, cfg)
        self.assertEqual(forwarder.resolve_discord_channel("C1"),
                         "D100")
        self.store.enqueue(POST, "C1", "1.1",
                           {"sender_name": "Ana", "text": "hi",
                            "files": []})
        self.assertTrue(forwarder.process_next())
        mapping = self.store.get_discord_map("C1", "1.1")
        self.assertIsNotNone(mapping)
        message = discord.messages[mapping["discord_message_id"]]
        self.assertEqual(message["channel"], "D100")
        self.assertEqual(discord.create_calls, 0)


if __name__ == "__main__":
    unittest.main()
