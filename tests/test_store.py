"""Unit tests: archive storage and Discord mapping."""

import os
import tempfile
import unittest

from bot.store import DELETE, EDIT, POST, MAX_ATTEMPTS, MAX_DEFERRALS, Store


def _queue_row(store, item_id):
    return store.conn.execute(
        "SELECT status, attempts, deferrals FROM forward_queue"
        " WHERE id=?", (item_id,)).fetchone()


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def test_store_and_search(self):
        self.store.store_message("C1", {"ts": "1.1", "user": "U1",
                                        "text": "deploy tonight"})
        hits = self.store.search_messages("deploy")
        self.assertEqual(len(hits), 1)

    def test_deleted_not_searchable(self):
        self.store.store_message("C1", {"ts": "2.2", "text": "secret"})
        self.store.mark_deleted("C1", "2.2")
        self.assertEqual(self.store.search_messages("secret"), [])

    def test_channel_webhook_roundtrip(self):
        placeholder = "https://discord.com/api/webhooks/123/abc"
        self.assertIsNone(self.store.get_channel_webhook("C1"))
        self.store.set_channel_webhook("C1", placeholder)
        self.assertEqual(self.store.get_channel_webhook("C1"),
                         placeholder)
        self.store.set_channel_webhook("C1", None)
        self.assertIsNone(self.store.get_channel_webhook("C1"))

    def test_discord_map_roundtrip(self):
        self.store.set_discord_map("C1", "1.1", "D42")
        mapping = self.store.get_discord_map("C1", "1.1")
        self.assertEqual(mapping["discord_message_id"], "D42")
        self.assertIsNone(mapping["discord_thread_id"])
        self.store.set_thread_id("C1", "1.1", "T7")
        self.assertEqual(
            self.store.get_discord_map("C1", "1.1")["discord_thread_id"],
            "T7")

    def test_enqueue_dedupe_post(self):
        id1, created1 = self.store.enqueue(POST, "C1", "1.1", {})
        id2, created2 = self.store.enqueue(POST, "C1", "1.1", {})
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(id1, id2)

    def test_enqueue_post_skipped_when_mapped(self):
        self.store.set_discord_map("C1", "1.1", "D1")
        _id, created = self.store.enqueue(POST, "C1", "1.1", {})
        self.assertFalse(created)

    def test_edit_folds_into_pending_post(self):
        post_id, _ = self.store.enqueue(POST, "C1", "1.1", {"text": "a"})
        edit_id, created = self.store.enqueue(EDIT, "C1", "1.1",
                                              {"text": "b"})
        self.assertFalse(created)
        self.assertEqual(edit_id, post_id)

    def test_delete_dedupe(self):
        id1, c1 = self.store.enqueue(DELETE, "C1", "1.1", {})
        id2, c2 = self.store.enqueue(DELETE, "C1", "1.1", {})
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(id1, id2)

    def test_mark_done_and_next_due(self):
        self.store.enqueue(POST, "C1", "1.1", {})
        item = self.store.next_due()
        self.assertIsNotNone(item)
        self.store.mark_done(item["id"])
        self.assertIsNone(self.store.next_due())
        self.assertEqual(self.store.queue_counts()["done"], 1)

    def test_retry_then_failed(self):
        self.store.enqueue(POST, "C1", "1.1", {})
        item = self.store.next_due()
        # Retry: scheduled again (not due immediately).
        self.assertTrue(self.store.mark_retry(item["id"], 60.0, "err"))
        self.assertIsNone(self.store.next_due())
        # After too many attempts the item is failed, not pending.
        for _ in range(20):
            self.store.mark_retry(item["id"], 60.0, "err")
        self.assertEqual(self.store.queue_counts()["failed"], 1)

    def test_rate_limit_deferral_never_fails(self):
        # A 429 is "slow down", not a failure: deferrals must never
        # consume the failure budget, no matter how many arrive in a
        # burst.
        self.store.enqueue(POST, "C1", "1.1", {})
        item = self.store.next_due()
        for _ in range(2 * MAX_ATTEMPTS):
            self.assertTrue(
                self.store.mark_retry(item["id"], 0.2, "429",
                                      count_attempt=False))
        row = _queue_row(self.store, item["id"])
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["deferrals"], 2 * MAX_ATTEMPTS)
        self.assertEqual(self.store.queue_counts()["failed"], 0)
        # A real retry interleaved with deferrals resets the deferral
        # streak (a real Discord response proves the 429 storm broke).
        self.assertTrue(self.store.mark_retry(item["id"], 60.0, "err"))
        row = _queue_row(self.store, item["id"])
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["deferrals"], 0)

    def test_rate_limit_deferral_cap_is_a_safety_valve(self):
        # Only a pathological endless 429 loop trips the much higher
        # deferral cap.
        self.store.enqueue(POST, "C1", "1.1", {})
        item = self.store.next_due()
        for _ in range(MAX_DEFERRALS):
            self.assertTrue(
                self.store.mark_retry(item["id"], 0.2, "429",
                                      count_attempt=False))
        self.assertFalse(
            self.store.mark_retry(item["id"], 0.2, "429",
                                  count_attempt=False))
        row = _queue_row(self.store, item["id"])
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempts"], 0)  # never spent attempts

    def test_next_due_keeps_post_order_per_channel(self):
        # A deferred head post must hold back later posts of its own
        # channel (otherwise a bulk re-import scrambles the order),
        # while other channels and non-post kinds proceed freely.
        first, _ = self.store.enqueue(POST, "C1", "1.1", {})
        second, _ = self.store.enqueue(POST, "C1", "1.2", {})
        other, _ = self.store.enqueue(POST, "C2", "9.9", {})
        edit, _ = self.store.enqueue(EDIT, "C1", "7.7", {})
        item = self.store.next_due()
        self.assertEqual(item["id"], first)
        # The head post waits out a long rate-limit cool-down.
        self.store.mark_retry(first, 3600.0, "429",
                              count_attempt=False)
        # The same-channel follower is held; the other channel wins.
        item = self.store.next_due()
        self.assertEqual(item["id"], other)
        self.store.mark_done(other)
        # Edits are not part of the post ordering and still flow.
        item = self.store.next_due()
        self.assertEqual(item["id"], edit)
        self.store.mark_done(edit)
        # Nothing else may be handed out while the head post pends.
        self.assertIsNone(self.store.next_due())
        # Once the head post resolves, the follower is released.
        self.store.mark_done(first)
        item = self.store.next_due()
        self.assertEqual(item["id"], second)

    def test_next_due_failed_head_releases_channel(self):
        first, _ = self.store.enqueue(POST, "C1", "1.1", {})
        second, _ = self.store.enqueue(POST, "C1", "1.2", {})
        self.store.mark_failed(first, "boom")
        item = self.store.next_due()
        self.assertEqual(item["id"], second)

    def test_sync_state(self):
        self.assertIsNone(self.store.get_state("k"))
        self.store.set_state("k", "v")
        self.assertEqual(self.store.get_state("k"), "v")
        self.store.set_state("k", "v2")
        self.assertEqual(self.store.get_state("k"), "v2")

    def test_channel_and_user_upserts(self):
        self.store.upsert_channel("C1", "general", "topic")
        self.store.upsert_channel("C1", None, None)
        row = self.store.get_channel("C1")
        self.assertEqual(row["name"], "general")
        self.assertEqual(row["topic"], "topic")
        self.store.set_channel_discord("C1", "D100", "general")
        self.assertEqual(self.store.get_channel("C1")["discord_id"],
                         "D100")
        self.store.upsert_user("U1", "ana", "Ana")
        self.assertEqual(self.store.get_user("U1")["display_name"], "Ana")
        self.assertIsNone(self.store.get_user("U9"))


if __name__ == "__main__":
    unittest.main()
