"""Unit tests: the --reset-discord (re-import from scratch) feature."""

import glob
import os
import shutil
import sqlite3
import tempfile
import unittest

from bot.main import backup_database, main
from bot.store import POST, Store

TS = "1700000000.000100"


def _seed(path: str) -> int:
    """Create a store holding one fully forwarded message.

    Returns the id of the (done) queue item for that message.
    """
    store = Store(path)
    store.upsert_channel("C1", "general", "general topic")
    store.set_channel_discord("C1", "D999", "general")
    store.upsert_user("U1", "ana_user", "Ana", "Ana Kreavia",
                      "https://img/ana.png")
    store.store_message("C1", {"ts": TS, "user": "U1",
                               "text": "deploy tonight"})
    item_id, created = store.enqueue(POST, "C1", TS, {"text": "x"})
    assert created
    store.mark_done(item_id)
    store.set_discord_map("C1", TS, "M123")
    store.set_state("backfill_cursor:C1", "")
    store.set_state("backfill_done:C1", "1700000000")
    store.set_state("operator_note", "keep me")
    store.close()
    return item_id


class ResetStoreTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        _seed(self.path)
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass
        for bak in glob.glob(self.path + ".bak-*"):
            os.unlink(bak)

    def test_state_counts(self):
        counts = self.store.discord_state_counts()
        self.assertEqual(counts["discord_map"], 1)
        self.assertEqual(counts["forward_queue"], 1)
        self.assertEqual(counts["channel_links"], 1)
        self.assertEqual(counts["backfill_keys"], 2)

    def test_reset_clears_channel_webhooks(self):
        # Auto-managed webhook URLs are Discord-side state: the
        # webhook dies with its channel, so a reset must drop them.
        self.store.set_channel_webhook(
            "C1", "https://discord.com/api/webhooks/123/abc")
        counts = self.store.discord_state_counts()
        self.assertEqual(counts["channel_webhooks"], 1)
        cleared = self.store.reset_discord_state()
        self.assertEqual(cleared["channel_webhooks"], 1)
        self.assertIsNone(self.store.get_channel_webhook("C1"))

    def test_reset_clears_discord_state_only(self):
        cleared = self.store.reset_discord_state()
        self.assertEqual(cleared["discord_map"], 1)
        self.assertEqual(cleared["forward_queue"], 1)
        self.assertEqual(cleared["channel_links"], 1)
        self.assertEqual(cleared["backfill_keys"], 2)
        # Discord-side state is gone.
        self.assertIsNone(self.store.get_discord_map("C1", TS))
        self.assertEqual(self.store.queue_counts(),
                         {"pending": 0, "done": 0, "failed": 0})
        channel = self.store.get_channel("C1")
        self.assertIsNotNone(channel)
        self.assertIsNone(channel["discord_id"])
        self.assertIsNone(channel["discord_name"])
        self.assertEqual(channel["name"], "general")
        self.assertIsNone(self.store.get_state("backfill_done:C1"))
        self.assertIsNone(self.store.get_state("backfill_cursor:C1"))
        # The Slack archive and unrelated state survive untouched.
        message = self.store.get_message("C1", TS)
        self.assertIsNotNone(message)
        self.assertEqual(message["text"], "deploy tonight")
        self.assertEqual(len(self.store.search_messages("deploy")), 1)
        user = self.store.get_user("U1")
        self.assertIsNotNone(user)
        self.assertEqual(user["display_name"], "Ana")
        self.assertEqual(self.store.get_state("operator_note"),
                         "keep me")

    def test_reenqueue_after_reset_creates_new_item(self):
        old_id = self.store.conn.execute(
            "SELECT id FROM forward_queue").fetchone()["id"]
        self.store.reset_discord_state()
        new_id, created = self.store.enqueue(
            POST, "C1", TS, {"text": "fresh payload"})
        self.assertTrue(created)
        self.assertGreater(new_id, old_id)
        row = self.store.next_due()
        self.assertIsNotNone(row)
        self.assertEqual(row["id"], new_id)


class ResetCliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "bridge.db")
        _seed(self.path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _backups(self):
        return glob.glob(self.path + ".bak-*")

    def test_dry_run_changes_nothing(self):
        rc = main(["--db", self.path, "--reset-discord"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._backups(), [])
        store = Store(self.path)
        try:
            self.assertIsNotNone(store.get_discord_map("C1", TS))
            self.assertEqual(store.queue_counts()["done"], 1)
            channel = store.get_channel("C1")
            self.assertEqual(channel["discord_id"], "D999")
        finally:
            store.close()

    def test_reset_with_yes_backs_up_and_clears(self):
        rc = main(["--db", self.path, "--reset-discord", "--yes"])
        self.assertEqual(rc, 0)
        backups = self._backups()
        self.assertEqual(len(backups), 1)
        # The backup holds the pre-reset state.
        conn = sqlite3.connect(backups[0])
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM discord_map").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1)
        # The live database is reset, archive intact.
        store = Store(self.path)
        try:
            self.assertIsNone(store.get_discord_map("C1", TS))
            self.assertEqual(store.queue_counts()["done"], 0)
            self.assertIsNone(store.get_channel("C1")["discord_id"])
            self.assertIsNotNone(store.get_message("C1", TS))
        finally:
            store.close()

    def test_missing_database_is_an_error(self):
        rc = main(["--db", os.path.join(self.tmpdir, "nope.db"),
                   "--reset-discord", "--yes"])
        self.assertEqual(rc, 2)
        self.assertEqual(self._backups(), [])

    def test_backup_database_helper(self):
        backup = backup_database(self.path)
        try:
            self.assertTrue(os.path.exists(backup))
            self.assertTrue(backup.startswith(self.path + ".bak-"))
            conn = sqlite3.connect(backup)
            try:
                row = conn.execute(
                    "SELECT text FROM messages WHERE ts=?",
                    (TS,)).fetchone()
            finally:
                conn.close()
            self.assertEqual(row[0], "deploy tonight")
        finally:
            os.unlink(backup)


if __name__ == "__main__":
    unittest.main()
