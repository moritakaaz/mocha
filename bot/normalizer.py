"""Normalizer: Slack message payloads become neutral BridgeMessages.

Includes the Slack mrkdwn to Discord markdown conversion. This mirrors
the behavior of Slackord's Slack-to-Discord markdown conversion
(behavioral reference only; this is an original implementation).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

# Slack subtypes that carry no conversation content worth bridging.
SKIP_SUBTYPES = {
    "channel_join", "channel_leave", "group_join", "group_leave",
    "channel_topic", "channel_purpose", "group_topic", "group_purpose",
    "channel_name", "group_name", "channel_archive", "channel_unarchive",
    "group_archive", "group_unarchive", "channel_convert_to_private",
    "ekm_access_denied", "app_conversation_prefilter", "huddle_thread",
}

_LINK_LABELED = re.compile(r"<((?:https?://|mailto:)[^>|]+)\|([^>]+)>")
_LINK_BARE = re.compile(r"<((?:https?://|mailto:)[^>]+)>")


def _link_compare_key(value: str) -> str:
    """Reduce a link URL or label to a comparable form.

    Strips the scheme (http:// or https://) and a single trailing
    slash, and folds case, so that a label like
    ``github.com/acme/tool`` matches its URL
    ``https://github.com/acme/tool``. This mirrors how Slack users
    see the label Slack generates for a pasted link.
    """
    key = value.casefold()
    for scheme in ("https://", "http://"):
        if key.startswith(scheme):
            key = key[len(scheme):]
            break
    if key.endswith("/"):
        key = key[:-1]
    return key
_USER_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]+))?>")
_CHANNEL_MENTION = re.compile(r"<#([CG][A-Z0-9]+)(?:\|([^>]+))?>")
_SUBTEAM = re.compile(r"<!subteam\^([A-Z0-9]+)(?:\|([^>]+))?>")
_SPECIAL = re.compile(r"<!(channel|here|everyone)>")
_BOLD = re.compile(r"\*([^*\n]+)\*")
_STRIKE = re.compile(r"~([^~\n]+)~")
_CODE_SPAN = re.compile(r"(```.*?```|`[^`\n]+`)", re.DOTALL)


@dataclass
class BridgeFile:
    """One Slack file attachment, reduced to relay essentials."""

    name: str = "file"
    size: int = 0
    mimetype: str = ""
    url: str = ""  # private download URL (needs the Slack bot token)


@dataclass
class BridgeMessage:
    """Platform-neutral representation of one Slack message."""

    channel_id: str
    ts: str
    thread_ts: str | None = None
    user_id: str = ""
    sender_name: str = ""
    sender_avatar: str = ""
    text: str = ""
    files: list[BridgeFile] = field(default_factory=list)
    posted_at: float | None = None

    @property
    def is_thread_reply(self) -> bool:
        return bool(self.thread_ts) and self.thread_ts != self.ts


def convert_text(text: str, resolve_user=None,
                 resolve_channel=None) -> str:
    """Convert Slack mrkdwn text to Discord markdown.

    ``resolve_user(user_id)`` and ``resolve_channel(channel_id)``
    optionally return display strings used for mentions; without them
    the label embedded in the Slack markup is used when present. A
    channel resolver may instead return a ready-made Discord channel
    mention (``<#123...>``), which is then emitted verbatim so the
    mention renders as a clickable channel link in Discord.
    """
    if not text:
        return ""
    # Protect code blocks and inline code from any conversion.
    parts = _CODE_SPAN.split(html.unescape(text))
    out: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 1:  # code segment, kept verbatim
            out.append(part)
            continue
        def _link(match: re.Match) -> str:
            url, label = match.group(1), match.group(2)
            # Slack stores a typed email address as a mailto link
            # ("<mailto:a@b.co|a@b.co>"). Discord has no use for the
            # mailto: scheme in message text, so render the bare
            # address; a differing label is kept as plain text before
            # it, mirroring the http(s) label rule below.
            if url.startswith("mailto:"):
                address = url[len("mailto:"):]
                if not label or label == address or label == url:
                    return address
                return f"{label} ({address})"
            # Never emit masked markdown links ([label](url)): some
            # Discord clients render them as ugly bracketed text,
            # which the owner explicitly rejected. A label that adds
            # nothing (empty, or the URL itself) collapses to the
            # bare URL; a real label is kept as plain text before the
            # URL. Slack's stored label for a pasted link omits the
            # scheme ("example.com/page" for "https://example.com/
            # page"), so the "is the URL itself" test compares
            # normalized forms; otherwise the same address would be
            # printed twice ("example.com/page (https://...)").
            if not label or _link_compare_key(label) == \
                    _link_compare_key(url):
                return url
            return f"{label} ({url})"

        part = _LINK_LABELED.sub(_link, part)

        def _bare_link(match: re.Match) -> str:
            url = match.group(1)
            if url.startswith("mailto:"):
                return url[len("mailto:"):]
            return url

        part = _LINK_BARE.sub(_bare_link, part)

        def _user(match: re.Match) -> str:
            user_id, label = match.group(1), match.group(2)
            if resolve_user:
                name = resolve_user(user_id)
                if name:
                    return "@" + name
            return "@" + (label or user_id)

        def _channel(match: re.Match) -> str:
            channel_id, label = match.group(1), match.group(2)
            if resolve_channel:
                resolved = resolve_channel(channel_id)
                if resolved:
                    if resolved.startswith("<#"):
                        return resolved
                    return "#" + resolved
            return "#" + (label or channel_id)

        part = _SUBTEAM.sub(lambda m: "@" + (m.group(2) or "group"), part)
        part = _USER_MENTION.sub(_user, part)
        part = _CHANNEL_MENTION.sub(_channel, part)
        part = _SPECIAL.sub(r"@\1", part)
        part = _BOLD.sub(r"**\1**", part)
        part = _STRIKE.sub(r"~~\1~~", part)
        out.append(part)
    return "".join(out)


def sender_name_for(user_row: dict | None, name_format: str,
                    fallback: str = "unknown") -> str:
    """Pick the sender name in the configured format.

    ``name_format`` mirrors Slackord's poster name choice: ``display``
    (display name), ``real`` (full real name), or ``user`` (username).
    """
    if not user_row:
        return fallback
    display = user_row.get("display_name")
    real = user_row.get("real_name")
    username = user_row.get("name")
    if name_format == "real":
        candidates = (real, display, username)
    elif name_format == "user":
        candidates = (username, display, real)
    else:
        candidates = (display, real, username)
    for candidate in candidates:
        if candidate:
            return candidate
    return fallback


def build_message(msg: dict, channel_id: str, get_user=None,
                  name_format: str = "display",
                  resolve_channel=None) -> BridgeMessage | None:
    """Build a BridgeMessage from a raw Slack message dict.

    ``get_user(user_id)`` optionally returns a user record dict with
    ``name`` / ``display_name`` / ``real_name`` / ``avatar_url`` keys.
    Returns None for messages that must not be bridged.
    """
    ts = msg.get("ts")
    if not ts:
        return None
    if msg.get("subtype") in SKIP_SUBTYPES:
        return None
    if msg.get("channel_type") == "im":  # private DMs are out of scope
        return None

    user_id = msg.get("user") or msg.get("bot_id") or ""
    user_row = None
    if get_user and user_id:
        try:
            user_row = get_user(user_id)
        except Exception:  # noqa: BLE001 - lookup is best-effort
            user_row = None

    def resolve_user(uid: str) -> str | None:
        if get_user:
            try:
                row = get_user(uid)
            except Exception:  # noqa: BLE001
                row = None
            if row:
                return (row.get("display_name") or row.get("real_name")
                        or row.get("name"))
        return None

    text = convert_text(msg.get("text") or "", resolve_user=resolve_user,
                        resolve_channel=resolve_channel)

    files: list[BridgeFile] = []
    for slack_file in msg.get("files") or []:
        files.append(BridgeFile(
            name=slack_file.get("name") or slack_file.get("title")
            or "file",
            size=int(slack_file.get("size") or 0),
            mimetype=slack_file.get("mimetype") or "",
            url=slack_file.get("url_private_download")
            or slack_file.get("url_private") or "",
        ))

    try:
        posted_at = float(ts)
    except (TypeError, ValueError):
        posted_at = None

    return BridgeMessage(
        channel_id=channel_id,
        ts=ts,
        thread_ts=msg.get("thread_ts"),
        user_id=user_id,
        sender_name=sender_name_for(user_row, name_format,
                                    fallback=user_id or "unknown"),
        sender_avatar=(user_row or {}).get("avatar_url") or "",
        text=text,
        files=files,
        posted_at=posted_at,
    )
