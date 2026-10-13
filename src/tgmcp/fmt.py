"""Conversions between TDLib objects and the compact values the tools return."""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .tdlib import TgError, execute

_PHOTO = {".jpg", ".jpeg", ".png", ".webp"}
_VIDEO = {".mp4", ".mov", ".m4v"}
_VOICE = {".ogg", ".oga", ".opus"}
_AUDIO = {".mp3", ".m4a", ".flac", ".wav"}
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

SEARCH_FILTERS = {
    "photo": "Photo", "video": "Video", "media": "PhotoAndVideo", "document": "Document",
    "file": "Document", "audio": "Audio", "voice": "VoiceNote", "video_note": "VideoNote",
    "link": "Url", "url": "Url", "gif": "Animation", "poll": "Poll", "pinned": "Pinned",
    "mention": "Mention",
}
MEMBER_FILTERS = {"admins": "Administrators", "bots": "Bots", "banned": "Banned",
                  "restricted": "Restricted", "contacts": "Contacts"}


def when(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def timestamp(value: int | str) -> int:
    """Unix time from a number, an ISO date/time (local zone) or an offset like +2h, -7d."""
    s = str(value).strip()
    if re.fullmatch(r"\d{9,}", s):
        return int(s)
    if m := re.fullmatch(r"([+-])(\d+)([smhdw])", s):
        delta = int(m.group(2)) * _UNITS[m.group(3)]
        return int(time.time()) + (delta if m.group(1) == "+" else -delta)
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        raise TgError(400, f"bad time {s!r}: use 2026-01-31, 2026-01-31T18:00, "
                           "+2h or -7d") from None


def markdown(ft: dict | None) -> str:
    """formattedText -> text, keeping links and styles as markdown."""
    if not ft:
        return ""
    if not ft.get("entities"):
        return ft.get("text", "")
    try:
        return execute("getMarkdownText", text=ft)["text"]
    except TgError:
        return ft.get("text", "")


def formatted(text: str, plain: bool = False) -> dict:
    ft = {"@type": "formattedText", "text": text}
    if plain or not text:
        return ft
    return execute("parseMarkdown", text=ft)


def filter_of(name: str | None, table: dict[str, str], prefix: str) -> dict | None:
    if not name:
        return None
    if name not in table:
        raise TgError(400, f"unknown filter {name!r}; one of: {', '.join(sorted(table))}")
    return {"@type": prefix + table[name]}


def file_of(content: dict) -> tuple[dict | None, str | None]:
    """Find the downloadable file inside a message content, with its name."""
    if content.get("@type") == "messagePhoto":
        sizes = content["photo"].get("sizes") or []
        return (sizes[-1]["photo"], None) if sizes else (None, None)
    for field, inner in (("document", "document"), ("video", "video"),
                         ("audio", "audio"), ("voice_note", "voice"),
                         ("animation", "animation"), ("video_note", "video"),
                         ("sticker", "sticker")):
        obj = content.get(field)
        if isinstance(obj, dict) and isinstance(obj.get(inner), dict):
            return obj[inner], obj.get("file_name") or None
    return None, None


def content(c: dict) -> dict:
    kind = c.get("@type", "")
    if kind == "messageText":
        return {"text": markdown(c.get("text"))}
    short = kind.removeprefix("message")
    out: dict[str, Any] = {"media": short[:1].lower() + short[1:]}
    if "caption" in c:
        out["text"] = markdown(c["caption"])
    elif kind == "messagePoll":
        poll = c["poll"]
        out["text"] = markdown(poll.get("question"))
        out["poll"] = [{"option": markdown(o.get("text")), "votes": o.get("voter_count", 0)}
                       for o in poll.get("options", [])]
    elif kind == "messageSticker":
        out["text"] = c["sticker"].get("emoji", "")
    file, name = file_of(c)
    if file:
        info = {"name": name, "size": file.get("size") or file.get("expected_size")}
        out["file"] = {k: v for k, v in info.items() if v}
    return {k: v for k, v in out.items() if v not in ("", None, {}, [])}


def buttons(markup: dict | None) -> list[list[dict]]:
    """Rows of the buttons attached to a message, inline or reply keyboard."""
    if not markup or "rows" not in markup:
        return []
    return markup["rows"]


def input_content(file: str, caption: dict, as_document: bool, voice: bool) -> dict:
    path = Path(file).expanduser()
    if not path.is_file():
        raise TgError(400, f"no such file: {path}")
    local = {"@type": "inputFileLocal", "path": str(path)}
    ext = path.suffix.lower()
    if voice or (ext in _VOICE and not as_document):
        return {"@type": "inputMessageVoiceNote", "caption": caption,
                "voice_note": {"@type": "inputVoiceNote", "voice_note": local}}
    if as_document:
        ext = ""
    if ext in _PHOTO:
        return {"@type": "inputMessagePhoto", "caption": caption,
                "photo": {"@type": "inputPhoto", "photo": local}}
    if ext in _VIDEO:
        return {"@type": "inputMessageVideo", "caption": caption,
                "video": {"@type": "inputVideo", "video": local, "supports_streaming": True}}
    if ext == ".gif":
        return {"@type": "inputMessageAnimation", "caption": caption,
                "animation": {"@type": "inputAnimation", "animation": local}}
    if ext in _AUDIO:
        return {"@type": "inputMessageAudio", "caption": caption,
                "audio": {"@type": "inputAudio", "audio": local}}
    return {"@type": "inputMessageDocument", "caption": caption,
            "document": {"@type": "inputDocument", "document": local}}
