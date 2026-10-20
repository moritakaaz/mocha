"""Entry point: python -m bot.main

Reads configuration from the environment (optionally a .env file in
the working directory), then runs the Socket Mode listener, the
backfill pass, and the forward-queue worker.

Flags:
  --selftest       run the token-free end-to-end self test and exit
  --reset-discord  forget all Discord-side state (dry run unless --yes
                   is also given) so the next normal start re-imports
                   the full Slack history from scratch; needs no
                   tokens and no network
  --yes            confirm a state-changing command such as
                   --reset-discord
  --config PATH    path to config.yaml (default: BRIDGE_CONFIG_PATH or
                   config.yaml)
  --db PATH        path to the SQLite database (default:
                   BRIDGE_DB_PATH or data/bridge.db)
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sqlite3
import sys
import threading
from datetime import datetime, timezone

log = logging.getLogger("bridge")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Slack to Discord bridge (one-way, real-time)")
    parser.add_argument("--selftest", action="store_true",
                        help="run the token-free self test and exit")
    parser.add_argument("--reset-discord", action="store_true",
                        help="forget all Discord-side state so the "
                             "next start re-imports from scratch "
                             "(dry run unless --yes is given)")
    parser.add_argument("--yes", action="store_true",
                        help="confirm a state-changing command such "
                             "as --reset-discord")
    parser.add_argument("--config", default=None,
                        help="path to config.yaml")
    parser.add_argument("--db", default=None,
                        help="path to the SQLite database")
    parser.add_argument("--env", default=".env",
                        help="path to the .env file (default: .env)")
    return parser.parse_args(argv)


def backup_database(db_path: str) -> str:
    """Copy the SQLite database to ``<db>.bak-<UTC timestamp>``.

    Uses the sqlite3 online-backup API, which yields a consistent
    snapshot even for WAL-mode databases. Returns the backup path.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = f"{db_path}.bak-{stamp}"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(backup_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    log.info("Database backed up to %s", backup_path)
    return backup_path


def reset_discord_command(args: argparse.Namespace) -> int:
    """Run ``--reset-discord``: stop bridge first, no tokens needed.

    Without ``--yes`` this is a dry run: it prints what would be
    cleared and changes nothing. With ``--yes`` it writes a timestamped
    backup of the database first, then forgets all Discord-side state
    (mapping, queue, channel links, backfill bookkeeping). The Slack
    archive is never touched.
    """
    from .config import load_env_file
    from .store import Store

    load_env_file(args.env)
    db_path = args.db or os.environ.get("BRIDGE_DB_PATH",
                                        "data/bridge.db")
    if not os.path.exists(db_path):
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        print("Nothing to reset. Start the bridge once first, or pass "
              "--db PATH.", file=sys.stderr)
        return 2

    store = Store(db_path)
    counts = store.discord_state_counts()
    store.close()
    print(f"Discord-side state in {db_path}:")
    print(f"  - {counts['discord_map']} forwarded-message mapping(s) "
          "in discord_map")
    print(f"  - {counts['forward_queue']} forward_queue item(s)")
    print(f"  - {counts['channel_links']} channel Discord link(s)")
    print(f"  - {counts['channel_webhooks']} channel webhook URL(s)")
    print(f"  - {counts['backfill_keys']} backfill state key(s)")
    print("The Slack archive (messages, users, channel rows) is NOT "
          "touched by this reset.")

    if not args.yes:
        print("\nDry run only: nothing was changed. Stop the bridge, "
              "delete the Discord channels (or point at a fresh server), "
              "then re-run with --yes to back up the database and "
              "apply the reset.")
        return 0

    backup_path = backup_database(db_path)
    store = Store(db_path)
    cleared = store.reset_discord_state()
    store.close()

    print(f"\nBackup written to {backup_path}")
    print("Reset complete:")
    print(f"  - {cleared['discord_map']} mapping(s) removed")
    print(f"  - {cleared['forward_queue']} queue item(s) removed")
    print(f"  - {cleared['channel_links']} channel link(s) dropped")
    print(f"  - {cleared['channel_webhooks']} channel webhook URL(s) "
          "dropped")
    print(f"  - {cleared['backfill_keys']} backfill state key(s) "
          "dropped")
    print("\nNext: start the bridge normally. It will recreate the "
          "same-name Discord channels and re-post the full Slack "
          "history it can still see (oldest to newest).")
    return 0


def check_tokens(cfg) -> str | None:
    """Return an English error message when tokens are missing/invalid."""
    problems = []
    if not cfg.slack_bot_token.startswith("xoxb-"):
        problems.append("SLACK_BOT_TOKEN (must start with xoxb-)")
    if not cfg.slack_app_token.startswith("xapp-"):
        problems.append("SLACK_APP_TOKEN (must start with xapp-)")
    if not cfg.discord_bot_token:
        problems.append("DISCORD_BOT_TOKEN")
    if problems:
        return ("Missing or invalid tokens in the environment. Set them in "
                ".env (copy .env.example first): "
                + ", ".join(problems))
    if cfg.slack_bot_token.startswith("xoxb-replace") or \
            cfg.discord_bot_token.startswith("replace"):
        return ("The tokens still contain the placeholder values from "
                ".env.example. Replace them with your real tokens.")
    return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.selftest:
        from .selftest import run_selftest
        setup_logging("INFO")
        return run_selftest()

    if args.reset_discord:
        setup_logging("INFO")
        return reset_discord_command(args)

    from .config import load_config, load_env_file
    load_env_file(args.env)
    cfg = load_config(config_path=args.config)
    if args.db:
        cfg.db_path = args.db

    setup_logging(cfg.log_level)

    error = check_tokens(cfg)
    if error:
        print(f"ERROR: {error}", file=sys.stderr)
        print("Run with --selftest for the token-free self test.",
              file=sys.stderr)
        return 2

    os.makedirs(os.path.dirname(os.path.abspath(cfg.db_path)), exist_ok=True)

    from .store import Store
    from .discord_client import DiscordClient
    from .forwarder import Forwarder, make_slack_downloader
    from .slack_listener import Bridge

    store = Store(cfg.db_path)
    discord = DiscordClient(cfg.discord_bot_token)
    bridge = Bridge(None, store, cfg)
    forwarder = Forwarder(store, discord, cfg,
                          download_file=make_slack_downloader(
                              cfg.slack_bot_token))

    stop = threading.Event()

    def _sig(signum, _frame):
        log.info("Signal %s received; shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    client = bridge.start_socket_mode()

    backfill_thread = threading.Thread(
        target=bridge.run_backfill_all, daemon=True)
    backfill_thread.start()

    worker_thread = threading.Thread(
        target=forwarder.run_forever, args=(stop,), daemon=True)
    worker_thread.start()

    log.info("Bridge is running. Press Ctrl+C to stop.")
    while not stop.wait(5):
        counts = store.queue_counts()
        log.debug("Queue state: %s", counts)
        if counts["failed"]:
            log.warning("%d queue item(s) failed; check the logs",
                        counts["failed"])
    try:
        client.close()
    except Exception:  # noqa: BLE001
        pass
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
