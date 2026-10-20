#!/usr/bin/env python3
"""Take a consistent backup of the bridge's runtime state.

The bridge keeps everything that matters in three files:

- the SQLite database (message archive, channel pairings, the
  Slack-to-Discord message map, stored webhook URLs, the forward
  queue),
- ``config.yaml`` (operator settings),
- ``.env`` (tokens — treat every backup copy as a secret).

The code itself is NOT part of the backup: it lives in version
control and can be cloned again at any time.

The database snapshot uses the sqlite3 online-backup API, which
produces a consistent copy of a WAL-mode database while the bridge
keeps running — never copy the live ``.db`` file (plus its ``-wal``
sidecar) with a plain file copy. The snapshot is verified with
``PRAGMA integrity_check`` before the script reports success.

Usage (from the repository root):

    .venv/bin/python scripts/backup.py [--keep-days 14]

Each run writes one timestamped set into ``backups/``:

    bridge-YYYYmmdd-HHMMSS.db
    config-YYYYmmdd-HHMMSS.yaml   (when config.yaml exists)
    env-YYYYmmdd-HHMMSS           (when .env exists, mode 600)

Sets older than ``--keep-days`` are deleted (pass 0 to keep all).
The script is safe to run from cron; it prints one summary line.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP_PREFIXES = ("bridge-", "config-", "env-")


def snapshot_database(db_path: Path, dest: Path) -> None:
    """Write a consistent snapshot of db_path to dest (online backup).

    The destination is switched out of WAL mode before closing, so
    the snapshot is one self-contained file (no -wal/-shm sidecars)
    that can be restored anywhere.
    """
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.commit()
    finally:
        dst.close()
        src.close()


def verify_snapshot(path: Path) -> tuple[bool, int]:
    """Return (integrity_ok, archived_message_count) for a snapshot."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        return integrity == "ok", int(count)
    finally:
        conn.close()


def prune_old_backups(out_dir: Path, keep_days: int, now: datetime) -> int:
    """Delete backup files older than keep_days. Returns files removed."""
    if keep_days <= 0:
        return 0
    cutoff = now - timedelta(days=keep_days)
    removed = 0
    for entry in out_dir.iterdir():
        if not entry.is_file() or not entry.name.startswith(BACKUP_PREFIXES):
            continue
        mtime = datetime.fromtimestamp(entry.stat().st_mtime, timezone.utc)
        if mtime < cutoff:
            entry.unlink()
            removed += 1
    return removed


def make_backup(db_path: Path, config_path: Path, env_path: Path,
                out_dir: Path, keep_days: int = 14) -> dict:
    """Create one timestamped backup set and prune old ones."""
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    written = []

    db_dest = out_dir / f"bridge-{stamp}.db"
    snapshot_database(db_path, db_dest)
    ok, messages = verify_snapshot(db_dest)
    if not ok:
        db_dest.unlink(missing_ok=True)
        raise RuntimeError(f"snapshot failed integrity check: {db_dest}")
    written.append(db_dest.name)

    if config_path.exists():
        config_dest = out_dir / f"config-{stamp}.yaml"
        shutil.copy2(config_path, config_dest)
        written.append(config_dest.name)

    if env_path.exists():
        env_dest = out_dir / f"env-{stamp}"
        shutil.copy2(env_path, env_dest)
        os.chmod(env_dest, 0o600)
        written.append(env_dest.name)

    removed = prune_old_backups(out_dir, keep_days, datetime.now(timezone.utc))
    return {"stamp": stamp, "files": written, "messages": messages,
            "pruned": removed, "out_dir": str(out_dir)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Back up the bridge database, config, and .env.")
    parser.add_argument("--db", default=str(ROOT / "data" / "bridge.db"),
                        help="path to the SQLite database")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"),
                        help="path to config.yaml")
    parser.add_argument("--env", default=str(ROOT / ".env"),
                        help="path to the .env file")
    parser.add_argument("--out", default=str(ROOT / "backups"),
                        help="directory that receives the backup set")
    parser.add_argument("--keep-days", type=int, default=14,
                        help="delete backup files older than this many days "
                             "(0 keeps everything)")
    args = parser.parse_args(argv)
    try:
        result = make_backup(Path(args.db), Path(args.config),
                             Path(args.env), Path(args.out), args.keep_days)
    except (FileNotFoundError, RuntimeError, sqlite3.Error) as exc:
        print(f"backup FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"backup ok: {result['stamp']} -> {result['out_dir']} "
          f"({len(result['files'])} files, {result['messages']} archived "
          f"messages, {result['pruned']} old files pruned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
