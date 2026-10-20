"""Minimal Discord REST API client (bot token, no gateway needed).

One-way posting only needs the REST API. Rate limiting is surfaced as
a ``DiscordRateLimit`` exception carrying the server-provided
``Retry-After`` delay so the queue worker can wait exactly as long as
Discord asks — the same automatic rate-limit handling Slackord does
for its imports.
"""

from __future__ import annotations

import logging
import re

import requests

log = logging.getLogger("bridge.discord")

API_BASE = "https://discord.com/api/v10"

_WEBHOOK_RE = re.compile(r"/webhooks/(\d+)/([\w-]+)")


class DiscordError(Exception):
    """A Discord API call failed with a non-429 status."""

    def __init__(self, status: int, body: str):
        super().__init__(f"Discord API error {status}: {body[:300]}")
        self.status = status
        self.body = body


class DiscordRateLimit(Exception):
    """Discord asked us to slow down (HTTP 429)."""

    def __init__(self, retry_after: float):
        super().__init__(f"Discord rate limit, retry after "
                         f"{retry_after:.1f}s")
        self.retry_after = retry_after


class DiscordClient:
    """REST client used by the forwarder. Duck-typed for tests."""

    def __init__(self, bot_token: str, timeout: float = 30.0):
        self._timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
            "User-Agent": "slack-discord-bridge/1.0",
        })

    # --- low-level helpers ------------------------------------------
    def _request(self, method: str, path: str, **kwargs):
        url = path if path.startswith("http") else API_BASE + path
        kwargs.setdefault("timeout", self._timeout)
        resp = self.session.request(method, url, **kwargs)
        if resp.status_code == 429:
            try:
                retry_after = float(resp.json().get("retry_after", 1))
            except Exception:  # noqa: BLE001
                retry_after = float(resp.headers.get("Retry-After", 1))
            raise DiscordRateLimit(retry_after)
        if resp.status_code >= 400:
            raise DiscordError(resp.status_code, resp.text)
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    @staticmethod
    def _multipart(content: str, files: list[dict]):
        """Build a requests ``files=`` payload for message attachments."""
        form = {"payload_json": (None, _json_dumps(
            {"content": content, "allowed_mentions": {"parse": []}}),
            "application/json")}
        for idx, item in enumerate(files):
            form[f"files[{idx}]"] = (item["name"], item["data"],
                                     item.get("mimetype")
                                     or "application/octet-stream")
        return form

    # --- messages (bot identity) --------------------------------------
    def create_message(self, channel_id: str, content: str,
                       files: list[dict] | None = None) -> dict:
        if files:
            # Multipart upload must not carry the JSON content type, so
            # send it through a bare session with only the auth header.
            bare = requests.Session()
            bare.headers.update({
                "Authorization": self.session.headers["Authorization"],
                "User-Agent": "slack-discord-bridge/1.0",
            })
            resp = bare.post(
                f"{API_BASE}/channels/{channel_id}/messages",
                files=self._multipart(content, files),
                timeout=self._timeout)
            return self._webhook_response(resp)
        return self._request(
            "POST", f"/channels/{channel_id}/messages",
            json={"content": content,
                  "allowed_mentions": {"parse": []}})

    def edit_message(self, channel_id: str, message_id: str,
                     content: str) -> dict:
        return self._request(
            "PATCH", f"/channels/{channel_id}/messages/{message_id}",
            json={"content": content,
                  "allowed_mentions": {"parse": []}})

    def delete_message(self, channel_id: str, message_id: str) -> dict:
        return self._request(
            "DELETE", f"/channels/{channel_id}/messages/{message_id}")

    def create_thread(self, channel_id: str, message_id: str,
                      name: str) -> dict:
        return self._request(
            "POST",
            f"/channels/{channel_id}/messages/{message_id}/threads",
            json={"name": (name or "Thread")[:100],
                  "auto_archive_duration": 10080})

    # --- channels -------------------------------------------------------
    def list_guild_channels(self, guild_id: str) -> list[dict]:
        return self._request("GET", f"/guilds/{guild_id}/channels")

    def create_guild_channel(self, guild_id: str, name: str,
                             topic: str = "") -> dict:
        return self._request(
            "POST", f"/guilds/{guild_id}/channels",
            json={"name": name, "type": 0, "topic": topic or ""})

    def rename_channel(self, channel_id: str, name: str) -> dict:
        """Rename a channel (mirrors a Slack channel rename)."""
        return self._request(
            "PATCH", f"/channels/{channel_id}", json={"name": name})

    # --- webhook management (automatic impersonation) -------------------
    def list_channel_webhooks(self, channel_id: str) -> list[dict]:
        """List the webhooks already present in a channel."""
        return self._request("GET", f"/channels/{channel_id}/webhooks")

    def create_webhook(self, channel_id: str, name: str) -> dict:
        """Create a webhook in a channel; the reply includes its URL."""
        return self._request(
            "POST", f"/channels/{channel_id}/webhooks",
            json={"name": name})

    # --- webhooks (sender impersonation) ---------------------------------
    def webhook_execute(self, webhook_url: str, content: str,
                        username: str = "", avatar_url: str = "",
                        files: list[dict] | None = None,
                        thread_id: str | None = None) -> dict:
        url = webhook_url.rstrip("/") + "?wait=true"
        if thread_id:
            url += f"&thread_id={thread_id}"
        if files:
            form = self._multipart(content, files)
            form["payload_json"] = (None, _json_dumps({
                "content": content, "username": username or None,
                "avatar_url": avatar_url or None,
                "allowed_mentions": {"parse": []}}),
                "application/json")
            session = requests.Session()  # webhook calls are unauthenticated
            resp = session.post(url, files=form, timeout=self._timeout)
            return self._webhook_response(resp)
        resp = requests.post(
            url, json={"content": content,
                       "username": username or None,
                       "avatar_url": avatar_url or None,
                       "allowed_mentions": {"parse": []}},
            timeout=self._timeout)
        return self._webhook_response(resp)

    def _webhook_response(self, resp) -> dict:
        if resp.status_code == 429:
            try:
                retry_after = float(resp.json().get("retry_after", 1))
            except Exception:  # noqa: BLE001
                retry_after = float(resp.headers.get("Retry-After", 1))
            raise DiscordRateLimit(retry_after)
        if resp.status_code >= 400:
            raise DiscordError(resp.status_code, resp.text)
        return resp.json() if resp.content else {}

    @staticmethod
    def _webhook_base(webhook_url: str, message_id: str) -> str:
        match = _WEBHOOK_RE.search(webhook_url)
        if not match:
            raise DiscordError(0, "invalid webhook URL")
        wid, wtoken = match.group(1), match.group(2)
        return f"{API_BASE}/webhooks/{wid}/{wtoken}/messages/{message_id}"

    def webhook_edit(self, webhook_url: str, message_id: str,
                     content: str, thread_id: str | None = None) -> dict:
        url = self._webhook_base(webhook_url, message_id)
        if thread_id:
            url += f"?thread_id={thread_id}"
        resp = requests.patch(
            url, json={"content": content,
                       "allowed_mentions": {"parse": []}},
            timeout=self._timeout)
        return self._webhook_response(resp)

    def webhook_delete(self, webhook_url: str, message_id: str,
                       thread_id: str | None = None) -> dict:
        url = self._webhook_base(webhook_url, message_id)
        if thread_id:
            url += f"?thread_id={thread_id}"
        resp = requests.delete(url, timeout=self._timeout)
        return self._webhook_response(resp)


def _json_dumps(obj: dict) -> str:
    import json
    return json.dumps(obj)
