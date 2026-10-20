"""Unit tests: auto-join of public Slack channels."""

import os
import tempfile
import unittest

from bot.config import Config, load_config
from bot.slack_listener import Bridge
from bot.store import Store


class FakeSlackError(Exception):
    """Mimics slack_sdk.errors.SlackApiError for scope errors."""

    def __init__(self, error):
        super().__init__(f"An error occurred: {error}")
        self.response = {"error": error}


class FakeSlackWeb:
    """In-memory stand-in for slack_sdk.WebClient."""

    def __init__(self, channels, join_error=None):
        self._channels = channels  # list of dicts
        self.join_error = join_error
        self.joined = []
        self.list_calls = 0

    def conversations_list(self, types="", limit=1000, cursor=None):
        self.list_calls += 1
        return {"channels": self._channels,
                "response_metadata": {"next_cursor": ""}}

    def conversations_join(self, channel):
        self.joined.append(channel)
        if self.join_error:
            raise self.join_error
        return {"ok": True, "channel": {"id": channel}}


def _temp_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return Store(path), path


class AutoJoinTestCase(unittest.TestCase):
    def setUp(self):
        self.store, self.path = _temp_store()
        self.channels = [
            {"id": "C1", "name": "general", "is_member": True},
            {"id": "C2", "name": "ship-it", "is_member": False},
            {"id": "C3", "name": "daily-standup", "is_member": False},
        ]

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def _bridge(self, web=None, enabled=True):
        cfg = Config()
        cfg.auto_join_public_channels = enabled
        return Bridge(web or FakeSlackWeb(self.channels), self.store, cfg)

    def test_joins_non_members_skips_members(self):
        web = FakeSlackWeb(self.channels)
        bridge = self._bridge(web)
        bridge.auto_join_public_channels()
        self.assertEqual(web.joined, ["C2", "C3"])
        self.assertGreaterEqual(web.list_calls, 1)

    def test_disabled_does_not_list_channels(self):
        web = FakeSlackWeb(self.channels)
        bridge = self._bridge(web, enabled=False)
        bridge.auto_join_public_channels()
        self.assertEqual(web.list_calls, 0)
        self.assertEqual(web.joined, [])

    def test_missing_scope_degrades_to_single_warning(self):
        web = FakeSlackWeb(self.channels,
                           join_error=FakeSlackError("missing_scope"))
        bridge = self._bridge(web)
        bridge.auto_join_public_channels()  # must not raise
        self.assertTrue(bridge._auto_join_warned)
        # The first failed join makes the loop stop: no more attempts.
        self.assertEqual(web.joined, ["C2"])
        bridge.auto_join_public_channels()  # still silent, still no raise
        self.assertEqual(web.joined, ["C2"])

    def test_other_join_error_keeps_going(self):
        web = FakeSlackWeb(self.channels)
        calls = []

        def flaky(channel):
            calls.append(channel)
            if channel == "C2":
                raise Exception("boom")

        web.conversations_join = flaky
        bridge = self._bridge(web)
        bridge.auto_join_public_channels()
        self.assertEqual(calls, ["C2", "C3"])  # C3 still attempted

    def test_channel_created_joins_new_public_channel(self):
        web = FakeSlackWeb([])
        bridge = self._bridge(web)
        bridge.handle_event({"type": "channel_created",
                             "channel": {"id": "C9", "name": "newchan"}})
        self.assertEqual(web.joined, ["C9"])
        self.assertEqual(self.store.get_channel("C9")["name"], "newchan")

    def test_channel_created_skips_private_channel(self):
        web = FakeSlackWeb([])
        bridge = self._bridge(web)
        bridge.handle_event({"type": "channel_created",
                             "channel": {"id": "C9", "name": "secret",
                                         "is_private": True}})
        self.assertEqual(web.joined, [])


class AutoJoinConfigTestCase(unittest.TestCase):
    def test_env_default_is_on(self):
        self.assertTrue(load_config(env={}).auto_join_public_channels)

    def test_env_can_turn_it_off(self):
        cfg = load_config(env={"AUTO_JOIN_PUBLIC_CHANNELS": "false"})
        self.assertFalse(cfg.auto_join_public_channels)


if __name__ == "__main__":
    unittest.main()
