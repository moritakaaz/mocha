"""Unit tests: queue worker against a fake Discord client."""

import os
import tempfile
import unittest

from bot.config import ChannelRule, Config, load_config
from bot.discord_client import DiscordError, DiscordRateLimit
from bot.forwarder import Forwarder, chunk_text
from bot.store import DELETE, EDIT, POST, MAX_ATTEMPTS, Store


FAKE_WEBHOOK_URL = "https://discord.com/api/webhooks/123/abc"


class FakeDiscord:
    def __init__(self):
        self.messages = {}
        self.webhook_calls = []
        self.rate_limit_next = False
        self.rate_limit_times = 0  # raise 429 this many calls in a row
        self.fail_always = False   # raise a real error on every call
        self.webhooks = {}         # channel_id -> [webhook dicts]
        self.webhook_list_calls = 0
        self.webhook_create_calls = 0
        self.webhook_404 = False   # webhook ops raise a 404
        self._counter = 0

    def _new_id(self):
        self._counter += 1
        return f"D{self._counter}"

    def create_message(self, channel_id, content, files=None):
        if self.fail_always:
            raise DiscordError(500, "boom")
        if self.rate_limit_times > 0:
            self.rate_limit_times -= 1
            raise DiscordRateLimit(0.25)
        if self.rate_limit_next:
            self.rate_limit_next = False
            raise DiscordRateLimit(0.0)
        mid = self._new_id()
        self.messages[mid] = {"channel": channel_id, "content": content,
                              "deleted": False}
        return {"id": mid}

    def edit_message(self, channel_id, message_id, content):
        self.messages[message_id]["content"] = content
        return {"id": message_id}

    def delete_message(self, channel_id, message_id):
        self.messages[message_id]["deleted"] = True
        return {}

    def create_thread(self, channel_id, message_id, name):
        return {"id": "T1"}

    def webhook_execute(self, webhook_url, content, username="",
                        avatar_url="", files=None, thread_id=None):
        if self.webhook_404:
            raise DiscordError(404, "Unknown Webhook")
        self.webhook_calls.append({"url": webhook_url, "content": content,
                                   "username": username,
                                   "avatar_url": avatar_url,
                                   "thread_id": thread_id})
        mid = self._new_id()
        self.messages[mid] = {"channel": thread_id or "webhook",
                              "content": content, "deleted": False}
        return {"id": mid}

    def webhook_edit(self, webhook_url, message_id, content,
                     thread_id=None):
        if self.webhook_404:
            raise DiscordError(404, "Unknown Webhook")
        self.webhook_calls.append({"url": webhook_url, "content": content,
                                   "thread_id": thread_id, "edit": True})
        self.messages[message_id]["content"] = content
        return {"id": message_id}

    def webhook_delete(self, webhook_url, message_id, thread_id=None):
        if self.webhook_404:
            raise DiscordError(404, "Unknown Webhook")
        self.webhook_calls.append({"url": webhook_url,
                                   "thread_id": thread_id,
                                   "delete": True})
        self.messages[message_id]["deleted"] = True
        return {}

    def list_channel_webhooks(self, channel_id):
        self.webhook_list_calls += 1
        return list(self.webhooks.get(channel_id, []))

    def create_webhook(self, channel_id, name):
        self.webhook_create_calls += 1
        hook = {"id": "W1", "name": name, "url": FAKE_WEBHOOK_URL}
        self.webhooks.setdefault(channel_id, []).append(hook)
        return hook

    def list_guild_channels(self, guild_id):
        return []

    def create_guild_channel(self, guild_id, name, topic=""):
        return {"id": f"DC_{name}", "name": name}


class ForwarderTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.store = Store(self.path)
        self.store.upsert_channel("C1", "general")
        self.cfg = Config()
        self.cfg.discord_guild_id = "G1"
        self.cfg.auto_create_channels = True
        self.discord = FakeDiscord()
        self.forwarder = Forwarder(self.store, self.discord, self.cfg)

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def test_post_maps_slack_ts(self):
        self.store.enqueue(POST, "C1", "1.1",
                           {"sender_name": "Ana", "text": "hello",
                            "files": []})
        self.assertTrue(self.forwarder.process_next())
        mapping = self.store.get_discord_map("C1", "1.1")
        self.assertIsNotNone(mapping)
        message = self.discord.messages[mapping["discord_message_id"]]
        self.assertEqual(message["content"], "**Ana:** hello")
        # The auto-created channel was recorded for next time.
        self.assertEqual(self.store.get_channel("C1")["discord_id"],
                         "DC_general")

    def test_edit_uses_latest_stored_text(self):
        self.store.enqueue(POST, "C1", "1.1",
                           {"sender_name": "Ana", "text": "v1",
                            "files": []})
        self.forwarder.process_next()
        self.store.store_message("C1", {"ts": "1.1", "text": "v2"},
                                 edited=True)
        self.store.enqueue(EDIT, "C1", "1.1", {"sender_name": "Ana"})
        self.forwarder.process_next()
        mapping = self.store.get_discord_map("C1", "1.1")
        self.assertEqual(
            self.discord.messages[mapping["discord_message_id"]]
            ["content"], "**Ana:** v2")

    def test_post_converts_stored_slack_markup(self):
        # The archive keeps raw Slack text; the send-time conversion
        # must turn mentions and links into Discord markdown. The
        # payload fallback text must not win over the stored version.
        self.store.upsert_user("U200", "bob_user",
                               display_name="Bob",
                               real_name="Bob Kreavia")
        raw = ("hi <@U200> and <@U999> see "
               "<https://example.com|the doc>")
        self.store.store_message("C1", {"ts": "2.1", "user": "U100",
                                        "text": raw})
        self.store.enqueue(POST, "C1", "2.1",
                           {"sender_name": "Ana",
                            "text": "payload text must not win",
                            "files": []})
        self.assertTrue(self.forwarder.process_next())
        mapping = self.store.get_discord_map("C1", "2.1")
        content = self.discord.messages[mapping["discord_message_id"]] \
            ["content"]
        # Known user -> resolved display name; unknown user -> raw ID.
        self.assertEqual(
            content,
            "**Ana:** hi @Bob and @U999 see "
            "the doc (https://example.com)")

    def test_post_resolves_bare_channel_mention(self):
        # Slack clients can store a channel mention as a bare <#C1>
        # with no label; the send-time conversion must resolve the
        # channel name from the store instead of leaking the raw ID.
        self.store.upsert_channel("C7", "daily-standup")
        raw = "buat <#C7> sendiri dan <#C8|random> juga"
        self.store.store_message("C1", {"ts": "2.7", "user": "U100",
                                        "text": raw})
        self.store.enqueue(POST, "C1", "2.7",
                           {"sender_name": "Ana", "text": raw,
                            "files": []})
        self.assertTrue(self.forwarder.process_next())
        mapping = self.store.get_discord_map("C1", "2.7")
        content = self.discord.messages[mapping["discord_message_id"]] \
            ["content"]
        self.assertEqual(content,
                         "**Ana:** buat #daily-standup sendiri dan "
                         "#random juga")

    def test_empty_post_is_skipped_not_failed(self):
        # A Slack system notice with empty text (e.g. a huddle
        # thread picked up by backfill) can never be posted: Discord
        # rejects empty messages. The item must complete as skipped
        # instead of failing and retrying forever.
        self.store.store_message("C1", {"ts": "3.3", "user": "USLACKBOT",
                                        "text": ""})
        self.store.enqueue(POST, "C1", "3.3",
                           {"sender_name": "Slackbot", "text": "",
                            "files": []})
        self.assertTrue(self.forwarder.process_next())
        self.assertEqual(self.discord.messages, {})
        row = self.store.conn.execute(
            "SELECT status FROM forward_queue WHERE slack_ts='3.3'"
        ).fetchone()
        self.assertEqual(row[0], "done")

    def test_edit_converts_stored_slack_markup(self):
        self.store.upsert_user("U200", "bob_user",
                               display_name="Bob",
                               real_name="Bob Kreavia")
        self.store.enqueue(POST, "C1", "2.1",
                           {"sender_name": "Ana", "text": "v1",
                            "files": []})
        self.forwarder.process_next()
        raw = "ping <@U200> then read <https://example.com|this link>"
        self.store.store_message("C1", {"ts": "2.1", "text": raw},
                                 edited=True)
        self.store.enqueue(EDIT, "C1", "2.1", {"sender_name": "Ana"})
        self.assertTrue(self.forwarder.process_next())
        mapping = self.store.get_discord_map("C1", "2.1")
        content = self.discord.messages[mapping["discord_message_id"]] \
            ["content"]
        self.assertEqual(
            content,
            "**Ana:** ping @Bob then read "
            "this link (https://example.com)")

    def test_post_converts_stored_markup_via_webhook(self):
        # Webhook path shares the same send-time conversion.
        self.cfg.rules.append(ChannelRule(
            slack="general", discord="general",
            webhook="https://hooks.example/1/abc"))
        self.store.upsert_user("U200", "bob_user",
                               display_name="Bob",
                               real_name="Bob Kreavia")
        raw = "hi <@U200> see <https://example.com|the doc>"
        self.store.store_message("C1", {"ts": "2.1", "user": "U100",
                                        "text": raw})
        self.store.enqueue(POST, "C1", "2.1",
                           {"sender_name": "Ana",
                            "sender_avatar": "https://img/a.png",
                            "text": "payload text must not win",
                            "files": []})
        self.assertTrue(self.forwarder.process_next())
        call = self.discord.webhook_calls[-1]
        self.assertEqual(call["username"], "Ana")
        self.assertEqual(
            call["content"],
            "hi @Bob see the doc (https://example.com)")

    def test_payload_fallback_kept_when_no_stored_message(self):
        # Without an archived message the payload text (already
        # converted at enqueue time) is sent as-is.
        self.store.enqueue(POST, "C1", "3.1",
                           {"sender_name": "Ana",
                            "text": "already [done](https://x.test)",
                            "files": []})
        self.assertTrue(self.forwarder.process_next())
        mapping = self.store.get_discord_map("C1", "3.1")
        content = self.discord.messages[mapping["discord_message_id"]] \
            ["content"]
        self.assertEqual(content,
                         "**Ana:** already [done](https://x.test)")

    def test_delete(self):
        self.store.enqueue(POST, "C1", "1.1",
                           {"sender_name": "Ana", "text": "x",
                            "files": []})
        self.forwarder.process_next()
        mapping = self.store.get_discord_map("C1", "1.1")
        self.store.enqueue(DELETE, "C1", "1.1", {})
        self.forwarder.process_next()
        self.assertTrue(
            self.discord.messages[mapping["discord_message_id"]]
            ["deleted"])

    def test_rate_limit_is_retried(self):
        self.discord.rate_limit_next = True
        self.store.enqueue(POST, "C1", "1.1",
                           {"sender_name": "Ana", "text": "x",
                            "files": []})
        self.forwarder.process_next()  # hits the 429, stays pending
        self.assertIsNone(self.store.get_discord_map("C1", "1.1"))
        self.forwarder.process_next(now=1e15)  # due after Retry-After
        self.assertIsNotNone(self.store.get_discord_map("C1", "1.1"))

    def _queue_row(self, item_id):
        return self.store.conn.execute(
            "SELECT status, attempts, deferrals FROM forward_queue"
            " WHERE id=?", (item_id,)).fetchone()

    def test_rate_limit_storm_is_survived(self):
        # More consecutive 429s than MAX_ATTEMPTS: the message must
        # still be delivered, and no failure attempts may be spent.
        storms = 2 * MAX_ATTEMPTS
        self.discord.rate_limit_times = storms
        item_id, _ = self.store.enqueue(
            POST, "C1", "1.1", {"sender_name": "Ana", "text": "x",
                                "files": []})
        for _ in range(storms + 5):
            if not self.forwarder.process_next(now=1e15):
                break
        self.assertIsNotNone(self.store.get_discord_map("C1", "1.1"))
        row = self._queue_row(item_id)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["deferrals"], storms)
        self.assertEqual(self.store.queue_counts()["failed"], 0)

    def test_posts_keep_channel_order_across_rate_limits(self):
        # Regression test: a 429 on the head post of a channel used
        # to let every later post of that channel overtake it while
        # it waited out the deferral, scrambling the Discord order
        # (seen live during a bulk re-import).
        self.store.upsert_channel("C2", "random")
        self.discord.rate_limit_times = 3  # first 3 create calls 429
        for i, text in enumerate(["one", "two", "three"]):
            self.store.enqueue(POST, "C1", f"1.{i}",
                               {"sender_name": "Ana", "text": text,
                                "files": []})
        other_id, _ = self.store.enqueue(
            POST, "C2", "9.9", {"sender_name": "Ana", "text": "other",
                                "files": []})
        # First pass: the head post is picked and rate limited.
        self.assertTrue(self.forwarder.process_next())
        self.assertIsNone(self.store.get_discord_map("C1", "1.0"))
        # While it cools down, its channel followers must stay
        # queued; only the other channel's post may be picked (here
        # it is rate limited once as well, which still proves the
        # worker reached it instead of a blocked follower).
        self.assertTrue(self.forwarder.process_next())
        self.assertIsNone(self.store.get_discord_map("C1", "1.1"))
        self.assertIsNone(self.store.get_discord_map("C1", "1.2"))
        self.assertEqual(self._queue_row(other_id)["deferrals"], 1)
        # Drain as if every deferral had expired: the channel's
        # messages must land in enqueue order.
        for _ in range(30):
            if not self.forwarder.process_next(now=1e15):
                break
        contents = [m["content"]
                    for m in self.discord.messages.values()
                    if m["channel"] == "DC_general"]
        self.assertEqual(contents, ["**Ana:** one", "**Ana:** two",
                                    "**Ana:** three"])
        self.assertIsNotNone(self.store.get_discord_map("C2", "9.9"))
        counts = self.store.queue_counts()
        self.assertEqual(counts["pending"], 0)
        self.assertEqual(counts["failed"], 0)

    def test_real_errors_still_fail_after_max_attempts(self):
        self.discord.fail_always = True
        item_id, _ = self.store.enqueue(
            POST, "C1", "1.1", {"sender_name": "Ana", "text": "x",
                                "files": []})
        for _ in range(MAX_ATTEMPTS + 5):
            if not self.forwarder.process_next(now=1e15):
                break
        row = self._queue_row(item_id)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempts"], MAX_ATTEMPTS)
        self.assertEqual(self.store.queue_counts()["failed"], 1)
        self.assertIsNone(self.store.get_discord_map("C1", "1.1"))

    def test_missing_pairing_fails_loudly(self):
        self.cfg.auto_create_channels = False
        self.store.upsert_channel("C9", "orphan")
        self.store.enqueue(POST, "C9", "1.1",
                           {"sender_name": "Ana", "text": "x",
                            "files": []})
        self.forwarder.process_next()
        self.assertEqual(self.store.queue_counts()["failed"], 1)

    def test_chunk_text(self):
        self.assertEqual(chunk_text("abc"), ["abc"])
        self.assertEqual(len("".join(chunk_text("z" * 5000))), 5000)

    # --- automatic webhook impersonation -----------------------------
    def _post_via_auto_webhook(self, text="hello"):
        self.cfg.impersonate = True
        self.store.enqueue(POST, "C1", "1.1",
                           {"sender_name": "Ana",
                            "sender_avatar": "https://img/a.png",
                            "text": text, "files": []})
        self.assertTrue(self.forwarder.process_next())

    def test_auto_webhook_created_and_impersonates(self):
        self._post_via_auto_webhook()
        # Exactly one webhook was created in the auto-created channel,
        # and the post went through it wearing the sender's identity.
        self.assertEqual(self.discord.webhook_create_calls, 1)
        call = self.discord.webhook_calls[-1]
        self.assertEqual(call["url"], FAKE_WEBHOOK_URL)
        self.assertEqual(call["username"], "Ana")
        self.assertEqual(call["avatar_url"], "https://img/a.png")
        self.assertEqual(call["content"], "hello")  # no bold prefix
        # The URL is remembered in the store for the next post.
        self.assertEqual(self.store.get_channel_webhook("C1"),
                         FAKE_WEBHOOK_URL)

    def test_auto_webhook_reused_not_recreated(self):
        self.cfg.impersonate = True
        self.store.enqueue(POST, "C1", "1.1",
                           {"sender_name": "Ana", "text": "one",
                            "files": []})
        self.store.enqueue(POST, "C1", "1.2",
                           {"sender_name": "Ana", "text": "two",
                            "files": []})
        self.forwarder.process_next()
        self.forwarder.process_next()
        self.assertEqual(self.discord.webhook_create_calls, 1)
        urls = [c["url"] for c in self.discord.webhook_calls]
        self.assertEqual(urls, [FAKE_WEBHOOK_URL, FAKE_WEBHOOK_URL])
        self.assertEqual(self.discord.webhook_list_calls, 1)

    def test_stored_webhook_skips_discovery(self):
        self.cfg.impersonate = True
        self.store.set_channel_webhook("C1", FAKE_WEBHOOK_URL)
        self._post_via_auto_webhook()
        # No list/create calls at all: the stored URL was used.
        self.assertEqual(self.discord.webhook_list_calls, 0)
        self.assertEqual(self.discord.webhook_create_calls, 0)
        self.assertEqual(self.discord.webhook_calls[-1]["url"],
                         FAKE_WEBHOOK_URL)

    def test_existing_named_webhook_is_adopted(self):
        self.cfg.impersonate = True
        self.discord.webhooks["DC_general"] = [
            {"id": "W9", "name": "Slack Bridge",
             "url": "https://discord.com/api/webhooks/999/zzz"}]
        self._post_via_auto_webhook()
        self.assertEqual(self.discord.webhook_create_calls, 0)
        self.assertEqual(self.discord.webhook_list_calls, 1)
        self.assertEqual(self.discord.webhook_calls[-1]["url"],
                         "https://discord.com/api/webhooks/999/zzz")
        self.assertEqual(self.store.get_channel_webhook("C1"),
                         "https://discord.com/api/webhooks/999/zzz")

    def test_explicit_rule_webhook_wins_over_auto_mode(self):
        self.cfg.impersonate = True
        rule_url = "https://hooks.example/rule/abc"
        self.cfg.rules.append(ChannelRule(
            slack="general", discord="general", webhook=rule_url))
        self._post_via_auto_webhook()
        # The manual URL is used; the bridge created nothing.
        self.assertEqual(self.discord.webhook_list_calls, 0)
        self.assertEqual(self.discord.webhook_create_calls, 0)
        self.assertEqual(self.discord.webhook_calls[-1]["url"],
                         rule_url)

    def test_webhook_404_falls_back_to_bot_post(self):
        self.cfg.impersonate = True
        self.store.set_channel_webhook("C1", FAKE_WEBHOOK_URL)
        self.discord.webhook_404 = True
        self.store.enqueue(POST, "C1", "1.1",
                           {"sender_name": "Ana",
                            "sender_avatar": "https://img/a.png",
                            "text": "hello", "files": []})
        self.assertTrue(self.forwarder.process_next())
        # Dead webhook cleared; this item went out as a bot post.
        self.assertIsNone(self.store.get_channel_webhook("C1"))
        mapping = self.store.get_discord_map("C1", "1.1")
        self.assertIsNotNone(mapping)
        content = self.discord.messages[mapping["discord_message_id"]] \
            ["content"]
        self.assertEqual(content, "**Ana:** hello")
        self.assertEqual(self.store.queue_counts()["failed"], 0)
        # The next item recreates the webhook.
        self.discord.webhook_404 = False
        self.store.enqueue(POST, "C1", "1.2",
                           {"sender_name": "Ana", "text": "two",
                            "files": []})
        self.forwarder.process_next()
        self.assertEqual(self.discord.webhook_create_calls, 1)
        self.assertEqual(self.discord.webhook_calls[-1]["username"],
                         "Ana")

    def test_webhook_404_falls_back_on_edit(self):
        self.cfg.impersonate = True
        self._post_via_auto_webhook()
        self.discord.webhook_404 = True
        self.store.store_message("C1", {"ts": "1.1", "text": "v2"},
                                 edited=True)
        self.store.enqueue(EDIT, "C1", "1.1", {"sender_name": "Ana"})
        self.assertTrue(self.forwarder.process_next())
        self.assertIsNone(self.store.get_channel_webhook("C1"))
        mapping = self.store.get_discord_map("C1", "1.1")
        content = self.discord.messages[mapping["discord_message_id"]] \
            ["content"]
        self.assertEqual(content, "**Ana:** v2")

    def test_impersonate_off_is_bot_mode(self):
        # Default config: no webhook discovery, bold sender prefix.
        self.store.enqueue(POST, "C1", "1.1",
                           {"sender_name": "Ana",
                            "sender_avatar": "https://img/a.png",
                            "text": "hello", "files": []})
        self.forwarder.process_next()
        self.assertEqual(self.discord.webhook_list_calls, 0)
        self.assertEqual(self.discord.webhook_create_calls, 0)
        self.assertEqual(self.discord.webhook_calls, [])
        mapping = self.store.get_discord_map("C1", "1.1")
        self.assertEqual(
            self.discord.messages[mapping["discord_message_id"]]
            ["content"], "**Ana:** hello")

    def test_impersonate_flag_parsed_from_yaml(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as fh:
            fh.write("impersonate: true\nchannels: []\n")
            path = fh.name
        try:
            cfg = load_config(env={}, config_path=path)
            self.assertTrue(cfg.impersonate)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
