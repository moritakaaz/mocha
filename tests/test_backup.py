"""Unit tests: scripts/backup.py snapshot, verify, and prune behavior."""

import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path

from bot.store import Store

_SPEC = importlib.util.spec_from_file_location(
    "bridge_backup_script",
    Path(__file__).resolve().parent.parent / "scripts" / "backup.py")
backup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backup)


class BackupTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        fd, self.db_path = tempfile.mkstemp(suffix=".db", dir=root)
        os.close(fd)
        os.unlink(self.db_path)
        self.store = Store(self.db_path)
        self.store.upsert_channel("C1", "general")
        self.store.store_message("C1", {"ts": "1.1", "user": "U1",
                                        "text": "first archived"})
        self.store.store_message("C1", {"ts": "2.2", "user": "U1",
                                        "text": "second archived"})
        self.config = root / "config.yaml"
        self.config.write_text("channels: []\n", encoding="utf-8")
        self.env = root / ".env"
        self.env.write_text("DISCORD_GUILD_ID=1\n", encoding="utf-8")
        self.out = root / "backups"

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_backup_set_and_verify(self):
        result = backup.make_backup(Path(self.db_path), self.config,
                                    self.env, self.out, keep_days=14)
        self.assertEqual(result["messages"], 2)
        names = sorted(p.name for p in self.out.iterdir())
        self.assertEqual(len(names), 3)
        db_copy = self.out / [n for n in names if n.endswith(".db")][0]
        ok, count = backup.verify_snapshot(db_copy)
        self.assertTrue(ok)
        self.assertEqual(count, 2)

    def test_env_copy_is_private(self):
        backup.make_backup(Path(self.db_path), self.config, self.env,
                           self.out, keep_days=14)
        env_copy = [p for p in self.out.iterdir()
                    if p.name.startswith("env-")][0]
        self.assertEqual(env_copy.stat().st_mode & 0o777, 0o600)

    def test_prune_removes_only_old_backups(self):
        self.out.mkdir(parents=True)
        old = self.out / "bridge-20000101-000000.db"
        old.write_bytes(b"x")
        ancient = time.time() - 30 * 24 * 3600
        os.utime(old, (ancient, ancient))
        fresh = self.out / "bridge-29990101-000000.db"
        fresh.write_bytes(b"x")
        unrelated = self.out / "notes.txt"
        unrelated.write_bytes(b"x")
        os.utime(unrelated, (ancient, ancient))
        result = backup.make_backup(Path(self.db_path), self.config,
                                    self.env, self.out, keep_days=14)
        self.assertGreaterEqual(result["pruned"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(unrelated.exists())

    def test_missing_database_raises(self):
        with self.assertRaises(FileNotFoundError):
            backup.make_backup(Path(self.db_path) / "nope",
                               self.config, self.env, self.out)


if __name__ == "__main__":
    unittest.main()
