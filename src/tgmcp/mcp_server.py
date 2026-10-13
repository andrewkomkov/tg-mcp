"""MCP server exposing the Telegram operations as tools over stdio."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .hub import Hub
from .tdlib import TgError

INSTRUCTIONS = """\
Tools for the user's own Telegram account (a real user session, not a bot).

A `chat` argument accepts: a numeric chat id (most reliable, taken from \
tg_list_chats), "@username", a t.me link, a phone number like "+15551234567", \
"me" for Saved Messages, or a chat title. An ambiguous title is rejected with \
the list of candidate ids. Message text is Markdown (**bold**, __italic__, \
`code`, [text](url)). Times accept "2026-01-31", "2026-01-31T18:00" (local \
time), or offsets like "+2h" and "-7d".
Messages are sent as the user to real people: send, edit, delete, forward or \
manage only what the user asked for.
Anything without a dedicated tool is still available: find the TDLib method \
with tg_api_search, read its fields with tg_api_describe, run it with tg_call.
If a tool reports "not logged in", walk the user through tg_login.\
"""

_READ = ToolAnnotations(readOnlyHint=True)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def _abs(path: str | None) -> str | None:
    return os.path.abspath(os.path.expanduser(path)) if path else None


def build(profile: str | None, read_only: bool) -> MCPServer:
    hub = Hub(profile)

    @asynccontextmanager
    async def lifespan(_server: MCPServer):
        try:
            yield
        finally:
            await hub.aclose()

    server = MCPServer("telegram", instructions=INSTRUCTIONS, version=__version__,
                       lifespan=lifespan)

    async def run(op: str, **args: Any) -> str:
        try:
            return json.dumps(await hub.call(op, **args), ensure_ascii=False)
        except TgError as e:
            # Only ToolError text reaches the model; anything else is masked.
            raise ToolError(str(e)) from None

    def read(description: str):
        return server.tool(description=description, annotations=_READ)

    def write(description: str, annotations: ToolAnnotations = _WRITE, always: bool = False):
        if read_only and not always:
            return lambda fn: fn
        return server.tool(description=description, annotations=annotations)

    def destructive(description: str):
        return write(description, _DESTRUCTIVE)

    # -- account -----------------------------------------------------------

    @read("Login state, TDLib version and which process holds the session.")
    async def tg_status() -> str:
        return await run("status")

    @write("Log in step by step; call with no arguments to see what is needed next. "
           "Order: `phone` (international format), then the `code` Telegram sends, then "
           "the 2FA `password` if the account has one. Ask the user for each.", always=True)
    async def tg_login(phone: str | None = None, code: str | None = None,
                       password: str | None = None) -> str:
        try:
            state = await hub.call("auth_state")
            for value, op, states in ((phone, "auth_phone", ("WaitPhoneNumber",)),
                                      (code, "auth_code", ("WaitCode", "WaitEmailCode")),
                                      (password, "auth_password", ("WaitPassword",))):
                if value and state["state"] in states:
                    state = await hub.call(op, **{op.removeprefix("auth_"): value})
        except TgError as e:
            raise ToolError(str(e)) from None
        return json.dumps(state, ensure_ascii=False)

    @read("The logged-in account: id, name, username, phone.")
    async def tg_me() -> str:
        return await run("me")

    @read("Devices and apps currently logged in to the account.")
    async def tg_list_sessions() -> str:
        return await run("sessions")

    # -- reading -----------------------------------------------------------

    @read("List chats, most recent first, with unread counts and last message. `query` "
          "searches chats by title/username instead; `unread` keeps only chats with unread "
          "messages; `archived` lists the archive; `folder` is a folder name or id from "
          "tg_list_folders.")
    async def tg_list_chats(limit: int = 30, query: str | None = None, unread: bool = False,
                            archived: bool = False, folder: str | None = None) -> str:
        return await run("chats", limit=limit, query=query, unread=unread, archived=archived,
                         folder=folder)

    @read("The user's chat folders.")
    async def tg_list_folders() -> str:
        return await run("folders")

    @read("Details of a chat or a user: type, username, members, bio, last seen.")
    async def tg_get_chat(chat: str) -> str:
        return await run("chat", chat=chat)

    @read("Read messages of a chat, oldest first; does not mark anything as read. Page back "
          "with `before_id` (id of the oldest message you have). `since` and `until` bound "
          "the dates, `topic` is a forum topic id from tg_list_topics, `scheduled` lists the "
          "user's scheduled messages instead.")
    async def tg_read_messages(chat: str, limit: int = 20, before_id: int = 0, topic: int = 0,
                               since: str | None = None, until: str | None = None,
                               scheduled: bool = False) -> str:
        return await run("messages", chat=chat, limit=limit, before_id=before_id, topic=topic,
                         since=since, until=until, scheduled=scheduled)

    @read("One message in full (reactions, buttons, link), by chat+id or by t.me link.")
    async def tg_get_message(chat: str | None = None, message_id: int = 0,
                             link: str | None = None) -> str:
        return await run("message", chat=chat, message_id=message_id, link=link)

    @read("Search messages in one chat or across all chats. `filter`: photo, video, media, "
          "document, audio, voice, link, gif, poll, pinned, mention. `sender` (needs `chat`) "
          "limits to one author. An empty query with a filter lists everything of that kind.")
    async def tg_search_messages(query: str = "", chat: str | None = None, limit: int = 20,
                                 filter: str | None = None, sender: str | None = None,
                                 since: str | None = None, until: str | None = None) -> str:
        return await run("search", query=query, chat=chat, limit=limit, filter=filter,
                         sender=sender, since=since, until=until)

    @read("Wait up to `timeout` seconds for new messages. Returns `messages` and a `cursor`; "
          "pass the cursor back to get only what arrived after it. Without a cursor, waits "
          "for messages from now on.")
    async def tg_wait_messages(cursor: int | None = None, timeout: float = 30,
                               chat: str | None = None, incoming_only: bool = True) -> str:
        return await run("updates", after=cursor, timeout=min(timeout, 300), chat=chat,
                         incoming_only=incoming_only)

    @read("Download the file/photo/video attached to a message; returns its local path.")
    async def tg_download_file(chat: str, message_id: int, out_dir: str | None = None) -> str:
        return await run("download", chat=chat, message_id=message_id, out_dir=_abs(out_dir))

    @read("Members of a group or channel. `filter`: admins, bots, banned, restricted.")
    async def tg_list_members(chat: str, query: str = "", limit: int = 50,
                              filter: str | None = None) -> str:
        return await run("members", chat=chat, query=query, limit=limit, filter=filter)

    @read("The user's contacts, optionally searched by name.")
    async def tg_list_contacts(query: str = "", limit: int = 100) -> str:
        return await run("contacts", query=query, limit=limit)

    @read("Topics of a forum group.")
    async def tg_list_topics(chat: str, query: str = "", limit: int = 50) -> str:
        return await run("topics", chat=chat, query=query, limit=limit)

    @read("Find TDLib methods by keywords, e.g. \"sticker set\" or \"story\"; `types` "
          "searches types instead of methods.")
    async def tg_api_search(query: str, limit: int = 15, types: bool = False) -> str:
        return await run("api_search", query=query, limit=limit, types=types)

    @read("Fields and documentation of a TDLib method, type or class, for use with tg_call.")
    async def tg_api_describe(name: str) -> str:
        return await run("api_describe", name=name)

    # -- writing -----------------------------------------------------------

    @write("Send a message as the user: text, a file, or an album of 2-10 files. `files` are "
           "local paths; the kind (photo, video, audio, voice, gif, document) follows the "
           "extension unless `as_document`. `text` becomes the caption. `schedule` delays "
           "sending; `reply_to` is a message id; `topic` is a forum topic id.")
    async def tg_send_message(chat: str, text: str = "", files: list[str] | None = None,
                              reply_to: int = 0, silent: bool = False,
                              schedule: str | None = None, topic: int = 0,
                              as_document: bool = False, voice: bool = False) -> str:
        return await run("send", chat=chat, text=text, files=[_abs(f) for f in files or []],
                         reply_to=reply_to, silent=silent, schedule=schedule, topic=topic,
                         as_document=as_document, voice=voice)

    @write("Send a poll with 2-12 options.")
    async def tg_send_poll(chat: str, question: str, options: list[str],
                           multiple: bool = False, anonymous: bool = True) -> str:
        return await run("poll", chat=chat, question=question, options=options,
                         multiple=multiple, anonymous=anonymous)

    @write("Replace the text (or caption) of one of the user's own messages.")
    async def tg_edit_message(chat: str, message_id: int, text: str) -> str:
        return await run("edit", chat=chat, message_id=message_id, text=text)

    @write("Forward messages to another chat; `as_copy` hides the original author.")
    async def tg_forward_messages(from_chat: str, to_chat: str, message_ids: list[int],
                                  as_copy: bool = False) -> str:
        return await run("forward", from_chat=from_chat, to_chat=to_chat,
                         message_ids=message_ids, as_copy=as_copy)

    @write("Add an emoji reaction to a message, or remove it.")
    async def tg_react(chat: str, message_id: int, emoji: str, remove: bool = False) -> str:
        return await run("react", chat=chat, message_id=message_id, emoji=emoji, remove=remove)

    @write("Pin or unpin a message in a chat.")
    async def tg_pin_message(chat: str, message_id: int, unpin: bool = False,
                             notify: bool = False) -> str:
        return await run("pin", chat=chat, message_id=message_id, unpin=unpin, notify=notify)

    @write("Leave an unsent draft in a chat for the user to review; empty text clears it.")
    async def tg_set_draft(chat: str, text: str = "") -> str:
        return await run("draft", chat=chat, text=text)

    @write("Press a bot button under a message, by its text or 0-based position. Returns the "
           "bot's answer and the message as it looks afterwards. Button texts are listed in "
           "the `buttons` field of a message.")
    async def tg_click_button(chat: str, message_id: int, button: str) -> str:
        return await run("click", chat=chat, message_id=message_id, button=button)

    @destructive("Change the state of a chat. `action`: mark_read, mark_unread, mute, unmute, "
                 "archive, unarchive, pin, unpin, block, unblock, leave, clear_history, "
                 "delete. `seconds` limits a mute. leave, clear_history and delete cannot be "
                 "undone.")
    async def tg_chat_action(chat: str, action: str, seconds: int = 0) -> str:
        return await run("chat_action", chat=chat, action=action, seconds=seconds)

    @write("Join a group or channel by invite link, @username or t.me link.")
    async def tg_join_chat(target: str) -> str:
        return await run("join", target=target)

    @write("Create a chat. `kind`: group, channel or forum (a group with topics).")
    async def tg_create_chat(title: str, kind: str = "group", members: list[str] | None = None,
                             description: str = "") -> str:
        return await run("create_chat", title=title, kind=kind, members=members,
                         description=description)

    @write("Change the title, description or photo (local image path) of a chat.")
    async def tg_edit_chat(chat: str, title: str | None = None, description: str | None = None,
                           photo: str | None = None) -> str:
        return await run("edit_chat", chat=chat, title=title, description=description,
                         photo=_abs(photo))

    @write("Create an invite link for a group or channel.")
    async def tg_create_invite_link(chat: str, name: str = "") -> str:
        return await run("invite_link", chat=chat, name=name)

    @destructive("Manage a member of a group or channel. `action`: add, kick, ban, unban, "
                 "promote (to admin), demote.")
    async def tg_manage_member(chat: str, user: str, action: str) -> str:
        return await run("member", chat=chat, user=user, action=action)

    @write("Add or remove a contact. `action`: add (by `user`, or `phone` + name), remove.")
    async def tg_manage_contact(action: str, user: str | None = None, phone: str = "",
                                first_name: str = "", last_name: str = "") -> str:
        return await run("contact", action=action, user=user, phone=phone,
                         first_name=first_name, last_name=last_name)

    @write("Change the user's own name, bio or username; only the given fields change.")
    async def tg_update_profile(first_name: str | None = None, last_name: str | None = None,
                                bio: str | None = None, username: str | None = None) -> str:
        return await run("profile", first_name=first_name, last_name=last_name, bio=bio,
                         username=username)

    @destructive("Delete messages, for everyone or only for the user. Cannot be undone.")
    async def tg_delete_messages(chat: str, message_ids: list[int],
                                 for_everyone: bool = True) -> str:
        return await run("delete", chat=chat, message_ids=message_ids,
                         for_everyone=for_everyone)

    @destructive("Call any TDLib method by name with JSON params; returns the raw result. "
                 "Covers everything without a dedicated tool (stories, stickers, privacy, "
                 "admin rights, ...). Look the method up with tg_api_search and "
                 "tg_api_describe first. Account- and session-destroying methods are blocked.")
    async def tg_call(method: str, params: dict[str, Any] | None = None) -> str:
        return await run("call", method=method, params=params)

    return server


def serve(profile: str | None, read_only: bool) -> None:
    build(profile, read_only).run("stdio")
