# Mocha

Mocha is a standalone bot that mirrors Slack channels to a Discord server
automatically, in real time, one way (Slack → Discord). No manual
exports, no GUI importer, and no dependency on any third-party
automation service. Deploy it on your own VPS with Docker Compose,
systemd, or pm2, using only Slack and Discord app tokens that you
create and hold yourself.

**Setting up for the first time? Follow [SETUP.md](SETUP.md)** —
the complete click-by-click walkthrough (creating the Slack app
and the Discord bot, every token, scope, and permission, all three
run methods, and day-2 operations). The Setup steps below are the
short version of the same path.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design, data model,
failure modes, and design decisions.

## How is this different from Slackord?

Slackord is a manual migration tool: you export your Slack history
as JSON files, run its desktop app, and it posts the exported
messages into Discord once. This bridge instead connects to Slack
through its API (Socket Mode) and forwards every new message as it
arrives — automatically, forever, with no export file and no manual
step after the first setup.

Behavior borrowed (by reference, not by code) from Slackord:
Slack-to-Discord markdown conversion, recreating Discord channels
with their descriptions, resuming an interrupted backfill without
duplicates, handling Discord rate limits automatically, and letting
the owner choose the poster name format (display, real, or user
name).

## Features

- Real-time Slack → Discord mirroring for every channel the bot is
  a member of, using Slack Socket Mode (outbound connection only, no
  public URL needed).
- Auto-join for public channels: on startup (and whenever a new
  public channel is created) the bot joins every public channel by
  itself, so the manual `/invite` step is only needed for private
  channels. Controlled by `AUTO_JOIN_PUBLIC_CHANNELS` (default on)
  and needs the `channels:join` scope.
- Backfill on startup and when the bot joins a channel: pulls the
  visible Slack history (roughly the last 90 days on the free plan),
  oldest first, deduplicated so restarts never double-post.
- Edit and delete sync: edit a Slack message, the Discord copy is
  edited; delete it, and the Discord copy is deleted.
- Thread replies become Discord threads.
- File relay: Slack attachments are downloaded and re-uploaded to
  Discord. Files larger than `DISCORD_MAX_FILE_MB` (25 MB by default)
  are forwarded as a text note with the file name.
- Persistent SQLite queue: if Discord is slow, down, or
  rate-limiting, messages wait and are retried. Rate limits (429)
  only defer a message following the server-provided `Retry-After`;
  they never count toward its failure limit, so a long rate-limit
  storm during a big backfill cannot fail a message. Real errors use
  exponential backoff. Nothing is lost on a crash or VPS reboot.
- Channel mapping: manual pairs in `config.yaml`, or automatic
  same-name discovery, plus optional auto-creation of missing Discord
  channels.
- Sender identity: set `impersonate: true` in `config.yaml` and the
  bridge creates and manages a per-channel Discord webhook itself,
  so messages display the Slack sender's name and avatar (dead
  webhooks are detected and recreated automatically). A manually
  configured per-channel webhook URL also works and wins over the
  automatic mode. Without either, the bot posts with the sender
  name in bold as a prefix.
- Local archive with full-text search (SQLite FTS5) of every message
  the bridge has seen.

## Setup Step 1: Create the Slack App

1. Go to https://api.slack.com/apps and choose **Create New App** →
   **From scratch**, pick your workspace.
2. Open **Socket Mode** and toggle it on. Create an app-level token
   (scope `connections:write`) — this is `SLACK_APP_TOKEN` and it
   starts with `xapp-`.
3. Open **OAuth & Permissions** and add these bot token scopes:
   `channels:history`, `groups:history`, `channels:read`,
   `groups:read`, `users:read`, `files:read`, `channels:join`.
   (The last one lets the bot join public channels by itself; see
   step 6.)
4. Open **Event Subscriptions**, ensure events are enabled, and add
   the bot events: `message.channels`, `message.groups`,
   `member_joined_channel`, `channel_created`. (The edit and delete
   events arrive as subtypes of the message events.)
5. Open **Install App** and install the app to your workspace. Copy
   the bot token — this is `SLACK_BOT_TOKEN` and starts with `xoxb-`.
6. **Public channels need no manual step.** With the `channels:join`
   scope granted and `AUTO_JOIN_PUBLIC_CHANNELS=true` (the default),
   the bot joins every public channel on startup and joins new public
   channels as they are created. **Private channels are different:**
   Slack only lets a bot enter a private channel when a member
   invites it, so for each private channel you want bridged, run
   `/invite @YourBridgeBotName` inside that channel. The bot can
   only see channels it is a member of.

## Setup Step 2: Create the Discord Bot

1. Go to https://discord.com/developers/applications and choose
   **New Application**.
2. Open **Bot** and click **Reset Token** to get the bot token —
   this is `DISCORD_BOT_TOKEN`. Keep it secret. No privileged
   gateway intents are needed: the bridge uses the Discord REST
   API only and never opens a gateway connection.
3. Open **OAuth2 → URL Generator**. Scopes: `bot`. Bot permissions
   — exactly what the code uses:
   - Always: `View Channel`, `Send Messages`, `Attach Files`,
     `Create Public Threads`, `Send Messages in Threads`
     (posting, file relay, and thread replies; `Send Messages`
     also covers editing and deleting the bot's own posts).
   - `Manage Messages` — fallback cleanup in webhook modes: when
     a channel's webhook is gone, the bot needs it to edit or
     delete posts the webhook made.
   - `Manage Webhooks` — only with `impersonate: true` (the
     bridge creates and adopts one webhook per channel).
   - `Manage Channels` — only with `AUTO_CREATE_CHANNELS=true`
     (the bridge creates missing Discord channels).

   Granting all eight at once is the permission integer
   `309774560272`. SETUP.md explains each permission in a table
   and gives the minimal-set integer.
4. Open the generated URL and add the bot to your Discord server.
6. Enable Developer Mode in Discord (User Settings → Advanced),
   right-click your server name, and copy the server ID — this is
   `DISCORD_GUILD_ID`.

## Setup Step 3: Configure

```bash
cp .env.example .env
# Edit .env and fill in SLACK_BOT_TOKEN, SLACK_APP_TOKEN,
# DISCORD_BOT_TOKEN, DISCORD_GUILD_ID.
chmod 600 .env

cp config.example.yaml config.yaml
# Edit config.yaml to map your channels. Manual mappings win over
# same-name auto-discovery. For sender name/avatar impersonation,
# either set impersonate: true (the bridge manages its own webhooks)
# or set per-channel webhook URLs manually, and pick the name_format
# (display / real / user).
```

Never commit `.env` or `config.yaml` if `config.yaml` contains
webhook URLs — both are listed in `.gitignore` already.

## Deploy with Docker Compose (recommended)

```bash
docker compose up -d
docker compose logs -f
```

Check the logs for the lines `Authenticated to Slack as bot user`
and `Socket Mode connected`. Post a message in a bridged Slack
channel; it should appear in the paired Discord channel within a
few seconds. History backfill runs on the first start and can take
a while for large channels — progress is logged per channel.

## Deploy with systemd (no Docker)

```bash
sudo mkdir -p /opt/slack-discord-bridge
sudo cp -r . /opt/slack-discord-bridge/
cd /opt/slack-discord-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then edit it and fill in the real tokens
cp config.example.yaml config.yaml   # then edit it
sudo cp systemd/slack-discord-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now slack-discord-bridge
journalctl -u slack-discord-bridge -f
```

## Deploy with pm2 (no Docker)

Already running your other apps under pm2? The repo ships an
`ecosystem.config.js`, so the bridge joins the same `pm2 list` and
is monitored the same way as everything else:

```bash
npm install -g pm2   # once, if pm2 is not installed yet
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then edit it and fill in the real tokens
cp config.example.yaml config.yaml   # then edit it
pm2 start ecosystem.config.js
pm2 logs mocha
```

Run `pm2 startup` (and the command it prints) plus `pm2 save` so
the bridge restarts automatically after a reboot. Full walkthrough:
[SETUP.md, Option C — pm2](SETUP.md#option-c--pm2).

## Operations

- **Logs:** `docker compose logs -f bridge`,
  `journalctl -u slack-discord-bridge -f` on systemd, or
  `pm2 logs mocha` on pm2.
- **Stop / start:** `docker compose down` / `docker compose up -d`,
  `systemctl stop|start slack-discord-bridge`, or
  `pm2 stop|start mocha`.
- **Backup:** stop the bot briefly and copy the `data/` folder (the
  SQLite database in WAL mode). A weekly copy of `data/bridge.db` is
  enough. Treat database backups as sensitive: they contain chat
  content.
- **Updating:** `git pull`, then `docker compose up -d --build`. On
  systemd installs, `git pull` and
  `systemctl restart slack-discord-bridge`. On pm2 installs,
  `git pull`, reinstall dependencies with
  `.venv/bin/pip install -r requirements.txt`, then
  `pm2 restart mocha`.
- **Token rotation:** if a token is exposed or revoked, generate a
  new one in the Slack App settings or Discord Developer Portal,
  replace it in `.env`, and restart the bot. Expect a clear startup
  error rather than a silent failure while a revoked token is in
  place.
- **Self test:** `python -m bot.main --selftest` runs an end-to-end
  check of the store, normalizer, queue, and forwarder with
  synthetic data and a fake Discord client — no tokens, no network.
- **Unit tests:** `python3 -m unittest discover -s tests`.

### Re-import from scratch (reset Discord state)

Deleted all the Discord channels, or moving to a fresh Discord
server, and want the bridge to re-post the full Slack history? The
bridge remembers what it already posted in its database, so deleting
channels alone is not enough — the Discord-side memory must be reset:

1. **Stop the bridge first** (`docker compose down`,
   `systemctl stop slack-discord-bridge`, or
   `pm2 stop mocha`). Never reset while it is
   running.
2. Delete the Discord channels (or create the fresh server and put
   its ID in `DISCORD_GUILD_ID`). If you keep a same-name channel,
   empty it first, or the re-import will duplicate its content.
3. Preview the reset (dry run, changes nothing):

   ```bash
   python -m bot.main --db data/bridge.db --reset-discord
   ```

4. Apply it. This first writes a safety backup next to the database
   (`bridge.db.bak-<UTC timestamp>`), then clears only the
   Discord-side state: the posted-message mapping, the forward queue,
   the channel Discord links, auto-managed webhook URLs, and backfill
   bookkeeping. **The Slack archive in the database is not touched.**
   With `impersonate: true` there is nothing else to redo — the
   bridge recreates its webhooks in the new channels by itself.

   ```bash
   python -m bot.main --db data/bridge.db --reset-discord --yes
   ```

5. Start the bridge normally. It recreates the same-name Discord
   channels and re-posts the history oldest to newest.

The reset command needs no tokens and no network. What can be
re-imported is bounded by what Slack still returns — on the free
plan roughly the last 90 days; anything older is gone from Slack
itself and cannot be re-imported (see Limitations).

## Troubleshooting

| Symptom | What to check |
|---|---|
| `ERROR: Missing or invalid tokens in the environment` | `.env` still has placeholder values, or a token does not start with `xoxb-` / `xapp-`. Copy `.env.example` again and fill it in. |
| Messages never appear in Discord | Confirm the bot is a member of the Slack channel (public channels are auto-joined when `AUTO_JOIN_PUBLIC_CHANNELS=true` and the app has the `channels:join` scope; private channels need a manual `/invite`), present and permitted in the Discord channel, and that a pairing exists (manual rule, same-name channel, or `AUTO_CREATE_CHANNELS=true`). Check the logs for `No Discord channel paired with Slack channel`. |
| Messages appear twice | A manual Slackord import overlapped with this bridge's backfill of the same history. Delete the duplicated Discord messages; the bridge's own dedupe prevents further copies. |
| Some old Slack messages are missing in Discord | They are older than what the Slack free plan shows the API (about 90 days). They can no longer be fetched automatically; see Limitations. |
| Edits are missing in Discord | A message edited before its first forward is posted with the newest text (by design); messages from before the bridge was installed have no Discord copy to edit. |
| Queue keeps growing, items marked failed | Read the logs for the failing item errors (`docker compose logs`, `journalctl -u slack-discord-bridge`, or `pm2 logs mocha`). Common causes: missing Discord permission, revoked token, or no paired channel. Fix the cause and restart; pending items resume automatically, failed items are not retried. |

## Limitations (honest list)

- **Slack free plan, 90-day window.** The Slack API only returns
  about the last 90 days of history on the free plan. This bridge
  protects everything from the day it starts; it cannot recover what
  Slack no longer returns. For older history, a one-time manual
  Slackord import remains the only route.
- **One-way only.** Replies in Discord are not sent back to Slack.
- **Private DMs are not bridged.**
- **Reactions are not synced in v1.**
- Senders appear under their Slack name and avatar, or a bold name
  prefix; they are not posting from their real Discord accounts
  (the same trade-off Slackord makes).
- **One Slack workspace and one Discord guild per process/container.**
  Run a second container with its own `.env` and `data/` volume for a
  second pair.
- Ordering is guaranteed inside each channel, not across channels.

## Credits and license

- **[Slackord](https://github.com/thomasloupe/Slackord)** by Thomas
  Loupe is the behavioral reference and original inspiration for
  this project (markdown conversion, channel recreation, resume,
  rate-limit handling, name formats). No Slackord code is used: it
  is a C#/.NET application licensed GPL-3.0, while this project is an
  original, clean-room Python implementation.
- The Slack-side event handling, SQLite/FTS5 archive, and backfill
  approach descend from the project owner's earlier Slack archive
  bot.
- This project is released under the MIT License — see
  [LICENSE](LICENSE).
- Slack is a trademark of Slack Technologies, LLC, and Discord is a
  trademark of Discord Inc. This project is not affiliated with or
  endorsed by either company.
