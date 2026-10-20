"""Token-free end-to-end self test: python -m bot.main --selftest.

Exercises the store, normalizer, queue dedupe, and forwarder against
synthetic data and a fake Discord client (no network, no tokens).
Prints PASS and returns 0 on success.
"""

from __future__ import annotations

import os
import tempfile

from .config import Config
from .discord_client import DiscordRateLimit
from .forwarder import Forwarder, chunk_text
from .normalizer import build_message, convert_text, sender_name_for
from .store import Store, EDIT, POST


class FakeDiscord:
    """In-memory stand-in for DiscordClient."""

    def __init__(self):
        self.messages = {}  # id -> dict(channel, content, files, deleted)
        self.threads = {}   # id -> dict(parent_message, channel)
        self.webhook_calls = []
        self.rate_limit_once = False
        self._counter = 0

    def _new_id(self) -> str:
        self._counter += 1
        return f"D{self._counter}"

    def create_message(self, channel_id, content, files=None):
        if self.rate_limit_once:
            self.rate_limit_once = False
            raise DiscordRateLimit(0.0)
        mid = self._new_id()
        self.messages[mid] = {"channel": channel_id, "content": content,
                              "files": files or [], "deleted": False}
        return {"id": mid}

    def edit_message(self, channel_id, message_id, content):
        self.messages[message_id]["content"] = content
        return {"id": message_id}

    def delete_message(self, channel_id, message_id):
        self.messages[message_id]["deleted"] = True
        return {}

    def create_thread(self, channel_id, message_id, name):
        tid = self._new_id()
        self.threads[tid] = {"parent_message": message_id,
                             "channel": channel_id}
        return {"id": tid}

    def webhook_execute(self, webhook_url, content, username="",
                        avatar_url="", files=None, thread_id=None):
        self.webhook_calls.append({"webhook": webhook_url,
                                   "content": content,
                                   "username": username,
                                   "thread_id": thread_id})
        mid = self._new_id()
        self.messages[mid] = {"channel": thread_id or "webhook",
                              "content": content, "files": files or [],
                              "deleted": False}
        return {"id": mid}

    def webhook_edit(self, webhook_url, message_id, content,
                     thread_id=None):
        self.messages[message_id]["content"] = content
        return {"id": message_id}

    def webhook_delete(self, webhook_url, message_id, thread_id=None):
        self.messages[message_id]["deleted"] = True
        return {}

    def list_guild_channels(self, guild_id):
        return []

    def create_guild_channel(self, guild_id, name, topic=""):
        return {"id": f"C_{name}", "name": name}


def _temp_store() -> tuple[Store, str]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return Store(path), path


def check_normalizer() -> None:
    # Labeled and bare links: never masked markdown links. A real
    # label becomes plain text before the URL; a label equal to the
    # URL collapses to the bare URL.
    assert convert_text("see <https://x.test/a|this doc> now") == \
        "see this doc (https://x.test/a) now"
    assert convert_text("go <https://x.test/a|https://x.test/a>") == \
        "go https://x.test/a"
    # Slack's stored label omits the scheme; it still collapses.
    assert convert_text(
        "go <https://github.com/thomasloupe/Slackord"
        "|github.com/thomasloupe/Slackord>") == \
        "go https://github.com/thomasloupe/Slackord"
    assert convert_text("visit <https://x.test>") == \
        "visit https://x.test"
    # Typed emails are stored by Slack as mailto links; they render
    # as the bare address, never as raw markup.
    assert convert_text("mail <mailto:a@b.co|a@b.co> ya") == \
        "mail a@b.co ya"
    assert convert_text("mail <mailto:a@b.co>") == "mail a@b.co"
    # Mentions with and without labels.
    got = convert_text("hi <@U123|ana> and <@U456>",
                       resolve_user=lambda uid: "Ana" if uid == "U123"
                       else None)
    assert got == "hi @Ana and @U456", got
    # Channel mention and specials.
    assert convert_text("join <#C999|general> <!channel>") == \
        "join #general @channel"
    # Bold and strike.
    assert convert_text("*bold* and ~gone~") == "**bold** and ~~gone~~"
    # Code is protected from conversion.
    assert convert_text("`<@U1>` and ```*not bold*```") == \
        "`<@U1>` and ```*not bold*```"
    # Name formats.
    user = {"name": "ana_user", "display_name": "Ana",
            "real_name": "Ana Kreavia"}
    assert sender_name_for(user, "display") == "Ana"
    assert sender_name_for(user, "real") == "Ana Kreavia"
    assert sender_name_for(user, "user") == "ana_user"
    # A built message carries files and converted text.
    msg = build_message(
        {"ts": "1700000000.000100", "user": "U1",
         "text": "Hello <@U1> *team*",
         "files": [{"name": "plan.png", "size": 1234,
                    "mimetype": "image/png",
                    "url_private": "https://files.example/p.png"}]},
        "C1", get_user=lambda uid: {"display_name": "Ana",
                                    "avatar_url": "https://img/ana.png"})
    assert msg is not None
    assert msg.text == "Hello @Ana **team**"
    assert msg.sender_name == "Ana"
    assert msg.sender_avatar == "https://img/ana.png"
    assert msg.files[0].name == "plan.png"
    # System subtypes are dropped.
    assert build_message({"ts": "1.1", "subtype": "channel_join"},
                         "C1") is None
    # Chunking.
    assert chunk_text("x" * 2000) == ["x" * 2000]
    assert len("".join(chunk_text("y" * 4500))) == 4500
    print("normalizer: PASS")


def check_store_dedupe() -> Store:
    store, _path = _temp_store()
    try:
        store.store_message("C1", {"ts": "1700000000.000100",
                                   "text": "hello"})
        id1, created1 = store.enqueue(POST, "C1", "1700000000.000100",
                                      {"text": "hello"})
        assert created1 and id1 > 0
        id2, created2 = store.enqueue(POST, "C1", "1700000000.000100",
                                      {"text": "hello"})
        assert not created2 and id2 == id1  # duplicate post deduped
        # An edit folds into the still-pending post.
        id3, created3 = store.enqueue(EDIT, "C1", "1700000000.000100",
                                      {"text": "hello!"})
        assert not created3 and id3 == id1
        # Edits dedupe against each other as well.
        store.mark_done(id1)
        id4, created4 = store.enqueue(EDIT, "C1", "1700000000.000100",
                                      {"text": "hello!"})
        assert created4
        id5, created5 = store.enqueue(EDIT, "C1", "1700000000.000100",
                                      {"text": "hello!!"})
        assert not created5 and id5 == id4
        # Retry/backoff transitions.
        row = store.next_due()
        assert row and row["id"] == id4
        assert store.mark_retry(row["id"], 5.0, "boom")
        assert store.next_due() is None  # not due yet
        row = store.next_due(now=1e15)
        assert row and row["id"] == id4
        counts = store.queue_counts()
        assert counts["pending"] == 1 and counts["done"] == 1
        # Leave a clean queue behind for the checks that reuse this store.
        store.mark_done(row["id"])
        print("store + queue dedupe: PASS")
        return store
    except Exception:
        store.close()
        raise


def check_forwarder(store: Store) -> None:
    cfg = Config()
    cfg.discord_guild_id = "G1"
    cfg.auto_create_channels = True
    store.upsert_channel("C1", "general")
    fake = FakeDiscord()
    fwd = Forwarder(store, fake, cfg, download_file=lambda url: b"DATA")

    # Post a plain message.
    _, created = store.enqueue(POST, "C1", "1700000001.000100",
                               {"sender_name": "Ana", "text": "Hi team",
                                "files": []})
    assert created
    assert fwd.process_next()
    mapping = store.get_discord_map("C1", "1700000001.000100")
    assert mapping is not None
    assert fake.messages[mapping["discord_message_id"]]["content"] == \
        "**Ana:** Hi team"

    # Post with a file attachment: it is uploaded.
    _, created = store.enqueue(POST, "C1", "1700000002.000100",
                               {"sender_name": "Ana", "text": "plan",
                                "files": [{"name": "plan.png", "size": 10,
                                           "mimetype": "image/png",
                                           "url": "https://f/1"}]})
    assert created
    assert fwd.process_next()
    mapping = store.get_discord_map("C1", "1700000002.000100")
    files = fake.messages[mapping["discord_message_id"]]["files"]
    assert files and files[0]["data"] == b"DATA"

    # Edit the message in Slack: the queued edit lands in Discord.
    store.store_message("C1", {"ts": "1700000001.000100",
                               "text": "Hi team (edited)"}, edited=True)
    store.enqueue(EDIT, "C1", "1700000001.000100", {"sender_name": "Ana"})
    assert fwd.process_next()
    mapping = store.get_discord_map("C1", "1700000001.000100")
    assert fake.messages[mapping["discord_message_id"]]["content"] == \
        "**Ana:** Hi team (edited)"

    # A rate limit is honored: the item stays pending and then succeeds.
    fake.rate_limit_once = True
    _, created = store.enqueue(POST, "C1", "1700000003.000100",
                               {"sender_name": "Ana", "text": "third",
                                "files": []})
    assert created
    assert fwd.process_next()  # consumes the rate limit, stays pending
    assert store.get_discord_map("C1", "1700000003.000100") is None
    while fwd.process_next(now=1e15):  # due again after Retry-After
        pass
    assert store.get_discord_map("C1", "1700000003.000100") is not None

    # Delete a message.
    store.enqueue(EDIT, "C1", "1700000002.000100", {"text": "x"})
    store.mark_deleted("C1", "1700000002.000100")
    from .store import DELETE
    store.enqueue(DELETE, "C1", "1700000002.000100", {})
    while fwd.process_next(now=1e15):
        pass
    mapping = store.get_discord_map("C1", "1700000002.000100")
    assert fake.messages[mapping["discord_message_id"]]["deleted"]

    # Webhook mode impersonates the sender's name.
    cfg.rules = []
    from .config import ChannelRule
    cfg.rules.append(ChannelRule(slack="general", discord="general",
                                 webhook="https://discord.com/api/"
                                         "webhooks/1/abc"))
    _, created = store.enqueue(POST, "C1", "1700000004.000100",
                               {"sender_name": "Ana",
                                "sender_avatar": "https://img/a.png",
                                "text": "via webhook", "files": []})
    assert created
    while fwd.process_next(now=1e15):
        pass
    assert fake.webhook_calls
    last = fake.webhook_calls[-1]
    assert last["username"] == "Ana"
    assert last["content"] == "via webhook"
    print("forwarder (fake Discord): PASS")


def check_thread(store: Store) -> None:
    cfg = Config()
    cfg.discord_guild_id = "G1"
    cfg.auto_create_channels = True
    fake = FakeDiscord()
    fwd = Forwarder(store, fake, cfg)
    store.upsert_channel("C2", "threads")

    # Parent message is forwarded first...
    store.enqueue(POST, "C2", "1700000010.000100",
                  {"sender_name": "Ana", "text": "parent", "files": []})
    assert fwd.process_next()
    parent = store.get_discord_map("C2", "1700000010.000100")
    # ...then the reply lands in a thread created from the parent.
    store.enqueue(POST, "C2", "1700000011.000100",
                  {"sender_name": "Rian", "text": "reply",
                   "thread_ts": "1700000010.000100", "files": []})
    assert fwd.process_next()
    reply = store.get_discord_map("C2", "1700000011.000100")
    assert reply["discord_thread_id"] is not None
    assert reply["discord_message_id"] in fake.messages
    thread = fake.threads[reply["discord_thread_id"]]
    assert thread["parent_message"] == parent["discord_message_id"]
    print("thread bridging: PASS")


def run_selftest() -> int:
    store, path = _temp_store()
    try:
        check_normalizer()
        store.close()
        store = check_store_dedupe()
        check_forwarder(store)
        check_thread(store)
        store.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except OSError:
                pass
    print("SELFTEST PASS: normalizer, store, queue dedupe, forwarder, "
          "threads all OK")
    return 0
