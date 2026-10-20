# SETUP — Mocha

The complete, click-by-click setup guide. By the end of this page
your bridge will be running on your own machine or VPS and mirroring
Slack channels into a Discord server in real time.

You will create two apps — one on Slack, one on Discord — copy three
tokens and one server ID into a `.env` file, write a small
`config.yaml`, and start the bot. Nothing else is needed: no public
URL, no domain name, no database server.

Estimated time: 15–25 minutes, most of it clicking around in the
Slack and Discord dashboards.

---

## 0. Prerequisites

- A **Slack workspace** where you can create and install apps
  (workspace admin approval may be required in some workspaces).
- A **Discord server** where you have the *Manage Server*
  permission (needed to add a bot).
- A machine to run the bot on, with **one** of these:
  - **Docker** with the Compose plugin (recommended), or
  - **Python 3.10+** with `venv` support (for the systemd and
    terminal paths), or
  - **Node.js** (for pm2) plus **Python 3.10+** with `venv`
    support (for the pm2 path — handy if your other apps already
    run under pm2 and you want everything in one `pm2 list`).
- This repository: clone it or download the release zip and unpack
  it, then open a terminal in the project folder.

```bash
git clone <this-repository-url> slack-discord-bridge
cd slack-discord-bridge
```

---

## 1. Discord side: create the bot

### 1.1 Create the application

1. Open https://discord.com/developers/applications and sign in.
2. Click **New Application** (top right).
3. Give it a name (for example `Slack Bridge`). This name shows up
   as the bot's username in your server; you can change it later.
4. Click **Create**.

### 1.2 Create the bot user and copy its token

1. In the left sidebar, open **Bot**.
2. Click **Reset Token** and confirm. Discord shows the bot token
   **once** — copy it now and keep it somewhere private for a
   moment; it goes into `.env` in step 3 as `DISCORD_BOT_TOKEN`.
3. Leave every **Privileged Gateway Intent** toggle **off**. This
   bridge talks to Discord through the REST API only and never
   opens a gateway connection, so it needs no privileged intents
   (including *Message Content Intent*).

### 1.3 Invite the bot to your server

1. In the left sidebar, open **OAuth2 → URL Generator**.
2. Under **Scopes**, tick **`bot`**.
3. Under **Bot Permissions**, tick the permissions below. The table
   says exactly what each one is used for; nothing extra is needed.

   | Permission | Why the bridge needs it |
   |---|---|
   | View Channel | See the channels it posts to |
   | Send Messages | Post mirrored messages; also covers editing and deleting its **own** posts |
   | Create Public Threads | Turn Slack thread parents into Discord threads |
   | Send Messages in Threads | Post thread replies inside those threads |
   | Attach Files | Re-upload Slack file attachments |
   | Manage Messages | Fallback cleanup in impersonation mode: if a channel's webhook is gone, the bot needs this to edit or delete posts the webhook made |
   | Manage Webhooks | **Only with `impersonate: true`** — the bridge creates and adopts one webhook per channel |
   | Manage Channels | **Only with `AUTO_CREATE_CHANNELS=true`** — the bridge creates missing Discord channels |

   Granting all eight at once corresponds to the permission integer
   **`309774560272`**. If you prefer the minimal set and skip both
   optional features, the five core permissions (View Channel,
   Send Messages, Attach Files, Create Public Threads, Send Messages
   in Threads) correspond to **`309237681152`**.
4. Copy the **Generated URL** at the bottom, open it in your
   browser, pick your server, and click **Authorize**.

### 1.4 Find your server (guild) ID

1. In the Discord app, open **User Settings → Advanced** and turn
   on **Developer Mode**.
2. Right-click your server's name or icon and choose **Copy Server
   ID**. This is `DISCORD_GUILD_ID` for step 3.

---

## 2. Slack side: create the app

### 2.1 Create the app

1. Open https://api.slack.com/apps and click **Create New App**.
2. Choose **From scratch**, give it a name (for example
   `Discord Bridge`), pick your workspace, and click
   **Create App**.

### 2.2 Enable Socket Mode and create the app-level token

Socket Mode lets the bot receive events over an outbound connection,
so no public URL is needed.

1. In the left sidebar, open **Socket Mode** and toggle it **on**.
2. Slack asks you to generate an app-level token. Give it a name,
   add the scope **`connections:write`**, and click **Generate**.
3. Copy the token — it starts with **`xapp-`**. This is
   `SLACK_APP_TOKEN` for step 3.
   (You can re-open it later under **Basic Information → App-Level
   Tokens**.)

### 2.3 Add the bot token scopes

1. In the left sidebar, open **OAuth & Permissions**.
2. Scroll to **Scopes → Bot Token Scopes** and add exactly these
   seven scopes:

   | Scope | Why the bridge needs it |
   |---|---|
   | `channels:history` | Read message history in public channels (backfill) |
   | `groups:history` | Read message history in private channels (backfill) |
   | `channels:read` | List and inspect public channels |
   | `groups:read` | List and inspect private channels |
   | `users:read` | Resolve sender names and avatars |
   | `files:read` | Download file attachments for the relay |
   | `channels:join` | Let the bot join public channels by itself (auto-join) |

### 2.4 Subscribe to events

1. In the left sidebar, open **Event Subscriptions** and toggle
   **Enable Events** on.
2. Expand **Subscribe to bot events** and add exactly these four:

   | Event | What it drives |
   |---|---|
   | `message.channels` | New messages, edits, and deletes in public channels |
   | `message.groups` | New messages, edits, and deletes in private channels |
   | `member_joined_channel` | Starts a backfill when the bot joins a channel |
   | `channel_created` | Lets the bot auto-join newly created public channels |

   Edit and delete sync needs no extra events: edits and deletes
   arrive as subtypes of the two `message.*` events.
3. Click **Save Changes** if the button appears.

### 2.5 Install the app and copy the bot token

1. In the left sidebar, open **Install App** and click
   **Install to Workspace** (or **Reinstall** if you changed scopes
   on an existing install — Slack requires a reinstall before new
   scopes and events take effect).
2. Approve the permission screen.
3. Copy the **Bot User OAuth Token** — it starts with **`xoxb-`**.
   This is `SLACK_BOT_TOKEN` for step 3.

### 2.6 Channel membership

- **Public channels need no manual step.** With the `channels:join`
  scope granted and `AUTO_JOIN_PUBLIC_CHANNELS=true` (the default),
  the bot joins every public channel on startup and joins new public
  channels as they are created.
- **Private channels:** Slack never lets a bot enter a private
  channel on its own. In each private channel you want bridged, a
  member must run:

  ```
  /invite @YourBridgeBotName
  ```

The bot can only see channels it is a member of — that list is its
whole world.

---

## 3. Configuration

### 3.1 The `.env` file

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` and fill in your values. Every variable the bridge
reads:

| Variable | Required | What it does |
|---|---|---|
| `SLACK_BOT_TOKEN` | Yes | Slack bot token (`xoxb-...`), used for all Slack Web API calls |
| `SLACK_APP_TOKEN` | Yes | Slack app-level token (`xapp-...`), used for the Socket Mode connection |
| `DISCORD_BOT_TOKEN` | Yes | Discord bot token from step 1.2 |
| `DISCORD_GUILD_ID` | Yes (for channel pairing) | The target Discord server's ID from step 1.4 |
| `AUTO_CREATE_CHANNELS` | No (default `false`) | When `true`, a missing paired Discord channel is created automatically (needs Manage Channels) |
| `AUTO_JOIN_PUBLIC_CHANNELS` | No (default `true`) | When `true`, the bot joins public Slack channels by itself (needs the `channels:join` scope) |
| `DISCORD_MAX_FILE_MB` | No (default `25`) | Largest Slack file, in MB, that is re-uploaded to Discord; larger files are forwarded as a text note with the file name |
| `LOG_LEVEL` | No (default `INFO`) | One of `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `BRIDGE_DB_PATH` | No (default `data/bridge.db`) | Where the SQLite database lives. The Docker Compose file sets this for you inside the container |
| `BRIDGE_CONFIG_PATH` | No (default `config.yaml`) | Where `config.yaml` lives. The Docker Compose file sets this for you inside the container |

Never commit `.env` to git — it is already listed in `.gitignore`.

### 3.2 The `config.yaml` file

```bash
cp config.example.yaml config.yaml
```

Every option:

| Option | What it does |
|---|---|
| `name_format` | How the sender name is shown: `display` (Slack display name, default), `real` (full real name), or `user` (Slack username) |
| `impersonate` | `true` / `false` (default `false`). When `true`, the bridge creates and manages a webhook named `Slack Bridge` in each paired Discord channel, so posts carry the Slack sender's name and avatar instead of the bot's name with a bold prefix. Needs the Manage Webhooks permission. Dead webhooks (their channel was deleted) are detected and recreated automatically |
| `channels` | A list of manual channel mappings (below). Manual mappings always win over same-name auto-discovery |

Each entry under `channels` takes:

| Key | Meaning |
|---|---|
| `slack` | Slack channel name (without `#`) or channel ID |
| `discord` | Discord channel name (without `#`) or channel ID, in the guild from `DISCORD_GUILD_ID` |
| `webhook` | Optional. A Discord webhook URL for that channel. When set, that channel posts through this webhook (sender name and avatar) and the URL wins over automatic `impersonate` mode for that channel |

```yaml
name_format: display
impersonate: true
channels:
  - slack: general
    discord: general
  - slack: daily-standup
    discord: standup
    # webhook: https://discord.com/api/webhooks/WEBHOOK_ID/WEBHOOK_TOKEN
```

**Mapping vs auto-discovery:** with no entry at all, a Slack channel
pairs with the Discord text channel of the **same name** in your
guild. If no such channel exists the message is skipped and logged —
unless `AUTO_CREATE_CHANNELS=true`, in which case the bridge creates
the Discord channel (with the Slack channel's name and topic).
Manual entries let you rename the Discord side or pin an exact
channel ID.

Note: if `config.yaml` contains manual webhook URLs, treat it as a
secret too (it is also listed in `.gitignore`).

---

## 4. Run the bridge

### Option A — Docker Compose (recommended)

```bash
docker compose up -d
docker compose logs -f
```

The Compose file builds the image, mounts `./data` (the database)
and `./config.yaml` into the container, and restarts the bot
automatically if it exits or the machine reboots.

### Option B — Python + systemd (no Docker)

Requires Python 3.10 or newer.

```bash
sudo mkdir -p /opt/slack-discord-bridge
sudo cp -r . /opt/slack-discord-bridge/
cd /opt/slack-discord-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env            # then edit it and fill in real values
cp config.example.yaml config.yaml   # then edit it
sudo cp systemd/slack-discord-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now slack-discord-bridge
journalctl -u slack-discord-bridge -f
```

(If you install into a different folder, adjust the paths inside
`systemd/slack-discord-bridge.service` first.)

### Option C — pm2

Pick this if your server already runs your other apps under
[pm2](https://pm2.keymetrics.io/): the bridge then shows up in the
same `pm2 list` as everything else, and you monitor it the same
way. Requires Node.js (for pm2 itself) and Python 3.10+.

The repo ships a ready-made pm2 process file,
`ecosystem.config.js`, which starts the bridge from the project
venv exactly the way the other options do. It contains no secrets —
the bridge reads its tokens from the project's own `.env` file.

```bash
# Install pm2 once (skip if it is already installed):
npm install -g pm2

# Set the bridge up (from the project folder):
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env            # then edit it and fill in real values
cp config.example.yaml config.yaml   # then edit it

# Start it:
pm2 start ecosystem.config.js

# Check that it is up and read its logs:
pm2 list
pm2 logs mocha
```

Make it survive a reboot — two steps, both needed:

```bash
pm2 startup        # prints one command; run that command as shown
pm2 save           # freeze the current app list for the next boot
```

Re-run `pm2 save` whenever you add or remove pm2 apps later, so
the saved list stays current.

Day-to-day pm2 commands for the bridge:

```bash
pm2 restart mocha   # restart (after config or token changes)
pm2 stop mocha      # stop
pm2 start mocha     # start again
pm2 delete mocha    # remove from pm2 entirely
```

### Option D — Just run it in a terminal (quick test)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env            # fill in real values
cp config.example.yaml config.yaml
.venv/bin/python -m bot.main
```

Stop with Ctrl+C. Useful for a first smoke test before setting up
Docker, systemd, or pm2.

### 4.1 Verify it works

1. In the logs, look for these two lines:
   - `Authenticated to Slack as bot user ...`
   - `Socket Mode connected; bridging Slack to Discord`
2. Post a message in a Slack channel the bot belongs to. It should
   appear in the paired Discord channel within a few seconds.
3. On the first start, the bot also backfills the visible history
   (about the last 90 days on the Slack free plan), oldest first.
   Progress is logged per channel, and the bot cannot double-post:
   restarting mid-backfill is safe.
4. Quick self-checks that need no tokens and post nothing:

   ```bash
   python3 -m bot.main --selftest          # end-to-end self test
   python3 -m unittest discover -s tests   # unit tests
   ```

---

## 5. Day-2 operations

### Commands by runtime

The same operations look slightly different depending on how you
run the bridge:

| Operation | Docker Compose | systemd | pm2 |
|---|---|---|---|
| Status | `docker compose ps` | `systemctl status slack-discord-bridge` | `pm2 list` |
| Logs (follow) | `docker compose logs -f bridge` | `journalctl -u slack-discord-bridge -f` | `pm2 logs mocha` |
| Restart | `docker compose restart bridge` | `sudo systemctl restart slack-discord-bridge` | `pm2 restart mocha` |
| Stop | `docker compose down` | `sudo systemctl stop slack-discord-bridge` | `pm2 stop mocha` |
| Start | `docker compose up -d` | `sudo systemctl start slack-discord-bridge` | `pm2 start mocha` |

### Updating to a new version

```bash
git pull
# Docker:
docker compose up -d --build
# systemd:
sudo systemctl restart slack-discord-bridge
# pm2 (also reinstall dependencies, in case they changed):
.venv/bin/pip install -r requirements.txt
pm2 restart mocha
```

Your `.env`, `config.yaml`, and `data/` are untouched by an update,
so no re-configuration is needed. Pending queue items resume
automatically after the restart.

### Rotating a token

If a token leaks or is revoked:

1. Create the replacement where it lives:
   - **Discord:** Developer Portal → your app → **Bot** →
     **Reset Token**.
   - **Slack bot token:** reinstalling the app (Slack dashboard →
     **Install App**) issues a fresh `xoxb-` token; revoke the old
     one under **OAuth & Permissions** if needed.
   - **Slack app token:** **Basic Information → App-Level Tokens** →
     revoke and generate a new one (scope `connections:write`).
2. Replace the value in `.env`.
3. Restart the bot (`docker compose restart bridge`,
   `sudo systemctl restart slack-discord-bridge`, or
   `pm2 restart mocha`).

While a revoked token is in place the bot logs a clear startup error
instead of failing silently.

### Backup

Stop the bot briefly and copy the `data/` folder — the SQLite
database inside it holds the message archive, the Slack→Discord
message mapping, and any auto-managed webhook URLs. A weekly copy of
`data/bridge.db` is enough. Treat backups as sensitive: they contain
chat content and webhook URLs.

### Re-import from scratch

Deleted the Discord channels, or pointed the bridge at a fresh
server, and want the full history re-posted? Use the built-in reset
command — it forgets only the Discord-side state (after writing a
safety backup of the database) and the next normal start re-imports
everything Slack still shows. The full procedure is in the README
section **"Re-import from scratch (reset Discord state)"**.

### Stopping and uninstalling

```bash
# Docker:
docker compose down            # add -v is NOT needed; your data/ stays
# systemd:
sudo systemctl disable --now slack-discord-bridge
sudo rm /etc/systemd/system/slack-discord-bridge.service
sudo systemctl daemon-reload
# pm2:
pm2 delete mocha
pm2 save                       # so it stays gone after a reboot
```

Then delete the project folder (including `.env` and `data/`) if you
want everything gone. To also remove the apps themselves, delete
them in the Slack app dashboard and the Discord Developer Portal,
which invalidates their tokens immediately.

---

## 6. Where to go next

- **README.md** — feature overview, operations summary,
  troubleshooting table, and the honest limitations list.
- **ARCHITECTURE.md** — how the bridge works inside: components,
  data model, failure modes, and design decisions.
