"""Telegram operations shared by the CLI and the MCP server.

Every operation is registered with @op and takes/returns plain JSON values,
so it can be invoked in-process or proxied over the hub socket.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import shutil
import sys
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Callable

from . import __version__, config, fmt, schema
from .config import Paths
from .manage import ManageOps
from .ops import NO_AUTH, OPS, op
from .tdlib import TdClient, TgError, execute

# Raw methods that can lock the user out of the account or kill the session.
_RAW_DENY = {"deleteAccount", "destroy", "close", "logOut", "setTdlibParameters",
             "terminateSession", "terminateAllOtherSessions",
             "setAuthenticationPhoneNumber", "changePhoneNumber", "setPassword"}
_UPDATES_KEPT = 1000


def _forum(topic: int) -> dict | None:
    return {"@type": "messageTopicForum", "forum_topic_id": topic} if topic else None


class Core:
    def __init__(self, paths: Paths, api_id: int, api_hash: str):
        self._paths = paths
        self._api_id = api_id
        self._api_hash = api_hash
        self._td: TdClient | None = None
        self._started = time.time()
        self._version = ""
        self._commit = ""
        self.schema: schema.Schema | None = None
        self.closed = False

        self._auth: dict = {}
        self._auth_seq = 0
        self._auth_changed = asyncio.Event()
        self._fatal: TgError | None = None

        self._me: dict | None = None
        self._names: dict[tuple[str, int], str] = {}
        self._tasks: set[asyncio.Task] = set()
        self._folders: list[dict] = []
        self._folders_known = asyncio.Event()

        self._send_results: OrderedDict[tuple[int, int], dict | TgError] = OrderedDict()
        self._send_waiters: dict[tuple[int, int], asyncio.Future] = {}

        self._updates: deque[tuple[int, dict]] = deque(maxlen=_UPDATES_KEPT)
        self._update_seq = 0
        self._update_event = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        execute("setLogStream", log_stream={
            "@type": "logStreamFile", "path": str(self._paths.log),
            "max_file_size": 10 << 20, "redirect_stderr": False})
        execute("setLogVerbosityLevel", new_verbosity_level=1)
        self._td = TdClient(self._on_update)
        self._td.start()
        self._version = (await self._req("getOption", name="version")).get("value", "")
        self._commit = (await self._req("getOption", name="commit_hash")).get("value", "")
        self.schema = await asyncio.to_thread(schema.load, self._commit,
                                              config.home() / "cache")
        await self._wait_auth(lambda s: s not in ("", "WaitTdlibParameters"), 30)

    async def close(self) -> None:
        if self._td is None:
            return
        if not self.closed:
            try:
                await self._req("close", timeout=10)
                await self._wait_auth(lambda s: s == "Closed", 10)
            except TgError:
                pass
        self.closed = True
        self._td.stop()

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _req(self, method: str, timeout: float = 60, **params: Any) -> dict:
        assert self._td is not None
        if self.schema is not None:
            self.schema.validate_call(method, params)
        return await self._td.request(method, timeout=timeout, **params)

    # -- updates -----------------------------------------------------------

    def _on_update(self, u: dict) -> None:
        kind = u.get("@type")
        if kind == "updateAuthorizationState":
            self._auth = u["authorization_state"]
            self._auth_seq += 1
            state = self._auth_name()
            if state == "WaitTdlibParameters":
                self._spawn(self._set_parameters())
            elif state == "Closed":
                self.closed = True
            elif state != "Ready":
                self._me = None
            self._auth_changed.set()
        elif kind == "updateNewMessage":
            # A message still being sent has a temporary id; buffer it once sent.
            if not u["message"].get("sending_state"):
                self._buffer(u["message"])
        elif kind == "updateMessageSendSucceeded":
            msg = u["message"]
            self._buffer(msg)
            self._send_done((msg["chat_id"], u["old_message_id"]), msg)
        elif kind == "updateMessageSendFailed":
            err = u.get("error") or {}
            self._send_done((u["message"]["chat_id"], u["old_message_id"]),
                            TgError(err.get("code", 0), err.get("message", "send failed")))
        elif kind == "updateChatFolders":
            self._folders = [{"id": f["id"], "name": f["name"]["text"]["text"]}
                             for f in u.get("chat_folders", [])]
            self._folders_known.set()

    def _buffer(self, message: dict) -> None:
        self._update_seq += 1
        self._updates.append((self._update_seq, message))
        self._update_event.set()

    async def _set_parameters(self) -> None:
        try:
            await self._req(
                "setTdlibParameters",
                use_test_dc=False,
                database_directory=str(self._paths.db),
                files_directory=str(self._paths.files),
                database_encryption_key="",
                use_file_database=True,
                use_chat_info_database=True,
                use_message_database=True,
                use_secret_chats=False,
                api_id=self._api_id,
                api_hash=self._api_hash,
                system_language_code="en",
                device_model="tg-mcp",
                system_version="",
                application_version=__version__,
            )
        except TgError as e:
            self._fatal = e
            self._auth_changed.set()

    # -- auth --------------------------------------------------------------

    def _auth_name(self) -> str:
        return self._auth.get("@type", "").removeprefix("authorizationState")

    async def _wait_auth(self, done: Callable[[str], bool], timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while not done(self._auth_name()):
            if self._fatal:
                raise self._fatal
            self._auth_changed.clear()
            left = deadline - time.monotonic()
            if left <= 0:
                raise TgError(408, f"TDLib is stuck in state {self._auth_name() or 'starting'}")
            try:
                await asyncio.wait_for(self._auth_changed.wait(), left)
            except asyncio.TimeoutError:
                pass

    def _auth_summary(self) -> dict:
        a = self._auth
        out: dict[str, Any] = {"state": self._auth_name(), "seq": self._auth_seq}
        if "link" in a:
            out["link"] = a["link"]
        if a.get("password_hint"):
            out["password_hint"] = a["password_hint"]
        info = a.get("code_info")
        if info:
            out["phone"] = info.get("phone_number")
            out["code_via"] = info.get("type", {}).get("@type", "").removeprefix(
                "authenticationCodeType")
        return out

    async def _auth_step(self, method: str, **params: Any) -> dict:
        seq = self._auth_seq
        await self._req(method, **params)
        await self._auth_after(seq, 30)
        return self._auth_summary()

    async def _auth_after(self, seq: int, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while self._auth_seq == seq and (left := deadline - time.monotonic()) > 0:
            self._auth_changed.clear()
            try:
                await asyncio.wait_for(self._auth_changed.wait(), left)
            except asyncio.TimeoutError:
                break

    def _require_ready(self) -> None:
        state = self._auth_name()
        if state != "Ready":
            raise TgError(401, f"not logged in (state: {state}); use tg_login, "
                               "or run `tg login` in a terminal")

    def _need_schema(self) -> schema.Schema:
        if self.schema is None:
            raise TgError(503, "the TDLib schema could not be downloaded; check the network")
        return self.schema

    @op(no_auth=True)
    async def status(self) -> dict:
        return {"auth": self._auth_name(), "tdlib": self._version,
                "tg_mcp": __version__, "owner_pid": os.getpid(),
                "uptime_s": int(time.time() - self._started),
                "data_dir": str(self._paths.root)}

    @op(no_auth=True)
    async def selfcheck(self) -> dict:
        """Compare every request this code builds with the installed TDLib schema."""
        s = self._need_schema()
        problems = []
        for name in ("tgmcp.api", "tgmcp.manage", "tgmcp.fmt"):
            source = inspect.getsource(sys.modules[name])
            problems += [f"{name}: {p}" for p in s.lint(source)]
        return {"tdlib": self._version, "commit": self._commit,
                "methods": sum(d.is_function for d in s.defs.values()),
                "problems": problems}

    @op(no_auth=True)
    async def api_search(self, query: str, limit: int = 15, types: bool = False) -> list[dict]:
        return self._need_schema().search(query, limit, types)

    @op(no_auth=True)
    async def api_describe(self, name: str) -> dict:
        return self._need_schema().describe(name)

    @op(no_auth=True)
    async def auth_state(self) -> dict:
        return self._auth_summary()

    @op(no_auth=True)
    async def auth_wait(self, seq: int, timeout: float = 30) -> dict:
        """Long-poll until the authorization state moves past `seq`."""
        await self._auth_after(seq, timeout)
        return self._auth_summary()

    @op(no_auth=True)
    async def auth_phone(self, phone: str) -> dict:
        return await self._auth_step("setAuthenticationPhoneNumber", phone_number=phone)

    @op(no_auth=True)
    async def auth_qr(self) -> dict:
        return await self._auth_step("requestQrCodeAuthentication", other_user_ids=[])

    @op(no_auth=True)
    async def auth_code(self, code: str) -> dict:
        if self._auth_name() == "WaitEmailCode":
            return await self._auth_step("checkAuthenticationEmailCode", code={
                "@type": "emailAddressAuthenticationCode", "code": code})
        return await self._auth_step("checkAuthenticationCode", code=code)

    @op(no_auth=True)
    async def auth_password(self, password: str) -> dict:
        return await self._auth_step("checkAuthenticationPassword", password=password)

    @op
    async def logout(self) -> dict:
        await self._req("logOut")
        await self._wait_auth(lambda s: s == "Closed", 30)
        return {"state": "LoggedOut"}

    @op(no_auth=True)
    async def call(self, method: str, params: dict | None = None,
                   unsafe: bool = False) -> dict:
        """Any TDLib method by name: full API coverage without wrappers."""
        if method in _RAW_DENY and not unsafe:
            raise TgError(403, f"{method} is blocked for raw calls")
        return await self._req(method, **(params or {}))

    # -- lookups -----------------------------------------------------------

    async def _get_me(self) -> dict:
        if self._me is None:
            self._me = await self._req("getMe")
        return self._me

    @staticmethod
    def _user_name(u: dict) -> str:
        return " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or "Deleted"

    @staticmethod
    def _username(obj: dict) -> str | None:
        active = (obj.get("usernames") or {}).get("active_usernames") or []
        return active[0] if active else None

    def _user_brief(self, u: dict) -> dict:
        out = {"id": u["id"], "name": self._user_name(u), "username": self._username(u),
               "phone": u.get("phone_number")}
        return {k: v for k, v in out.items() if v}

    async def _sender(self, sender: dict | None) -> tuple[int | None, str]:
        if not sender:
            return None, ""
        if sender["@type"] == "messageSenderUser":
            key = ("user", sender["user_id"])
        else:
            key = ("chat", sender["chat_id"])
        if key not in self._names:
            try:
                if key[0] == "user":
                    self._names[key] = self._user_name(await self._req("getUser", user_id=key[1]))
                else:
                    self._names[key] = (await self._req("getChat", chat_id=key[1]))["title"]
            except TgError:
                self._names[key] = str(key[1])
        return key[1], self._names[key]

    async def _chat_title(self, chat_id: int) -> str:
        return (await self._sender({"@type": "messageSenderChat", "chat_id": chat_id}))[1]

    async def _message(self, m: dict, with_chat: bool = False) -> dict:
        sender_id, sender = await self._sender(m.get("sender_id"))
        out: dict[str, Any] = {"id": m["id"], "date": fmt.when(m["date"]),
                               "from": sender, "from_id": sender_id}
        if with_chat:
            out["chat_id"] = m["chat_id"]
            out["chat"] = await self._chat_title(m["chat_id"])
        if m.get("is_outgoing"):
            out["out"] = True
        out.update(fmt.content(m.get("content") or {}))
        reply = m.get("reply_to") or {}
        if reply.get("message_id"):
            out["reply_to"] = reply["message_id"]
        topic = m.get("topic_id") or {}
        if topic.get("forum_topic_id"):
            out["topic"] = topic["forum_topic_id"]
        for flag, name in (("forward_info", "forwarded"), ("edit_date", "edited"),
                           ("is_pinned", "pinned")):
            if m.get(flag):
                out[name] = True
        schedule = m.get("scheduling_state") or {}
        if schedule.get("send_date"):
            out["scheduled"] = fmt.when(schedule["send_date"])
        info = m.get("interaction_info") or {}
        if info.get("view_count"):
            out["views"] = info["view_count"]
        reactions = (info.get("reactions") or {}).get("reactions") or []
        if reactions:
            out["reactions"] = {r["type"].get("emoji", "custom"): r["total_count"]
                                for r in reactions}
        rows = fmt.buttons(m.get("reply_markup"))
        if rows:
            out["buttons"] = [[b["text"] for b in row] for row in rows]
        return out

    async def _chat_brief(self, c: dict) -> dict:
        t = c.get("type") or {}
        kind = t.get("@type", "").removeprefix("chatType").lower()
        username = None
        try:
            if kind == "private":
                user = await self._req("getUser", user_id=t["user_id"])
                username = self._username(user)
                if user.get("type", {}).get("@type") == "userTypeBot":
                    kind = "bot"
            elif kind == "supergroup":
                username = self._username(
                    await self._req("getSupergroup", supergroup_id=t["supergroup_id"]))
                if t.get("is_channel"):
                    kind = "channel"
            elif kind == "basicgroup":
                kind = "group"
        except TgError:
            pass
        out: dict[str, Any] = {"id": c["id"], "title": c.get("title", ""), "type": kind}
        if username:
            out["username"] = username
        if c.get("unread_count"):
            out["unread"] = c["unread_count"]
        if c.get("unread_mention_count"):
            out["mentions"] = c["unread_mention_count"]
        last = c.get("last_message")
        if last:
            out["last_date"] = fmt.when(last["date"])
            body = fmt.content(last.get("content") or {})
            preview = " ".join((body.get("text") or f"[{body.get('media', '')}]").split())
            out["last"] = preview[:100]
        return out

    async def _private_chat(self, user_id: int) -> int:
        return (await self._req("createPrivateChat", user_id=user_id, force=False))["id"]

    async def _resolve(self, ref: int | str) -> int:
        """Chat reference -> chat id. Refuses to guess between several matches."""
        s = str(ref).strip()
        if not s:
            raise TgError(400, "empty chat reference")
        if re.fullmatch(r"-?\d+", s):
            cid = int(s)
            try:
                return (await self._req("getChat", chat_id=cid))["id"]
            except TgError:
                if cid > 0:
                    return await self._private_chat(cid)
                raise
        if s.lower() in ("me", "self", "saved"):
            return await self._private_chat((await self._get_me())["id"])
        if re.fullmatch(r"\+\d{7,15}", s):
            user = await self._req("searchUserByPhoneNumber", phone_number=s)
            return await self._private_chat(user["id"])
        m = re.fullmatch(r"(?:@|(?:https?://)?t\.me/)([A-Za-z][A-Za-z0-9_]{3,31})/?", s)
        if m:
            return (await self._req("searchPublicChat", username=m.group(1)))["id"]

        ids = (await self._req("searchChats", query=s, limit=10))["chat_ids"]
        if not ids:
            ids = (await self._req("searchChatsOnServer", query=s, limit=10))["chat_ids"]
        if not ids:
            users = (await self._req("searchContacts", query=s, limit=5)).get("user_ids", [])
            ids = [await self._private_chat(u) for u in users]
        if not ids:
            raise TgError(404, f"no chat matches {s!r}")
        chats = [await self._req("getChat", chat_id=i) for i in ids]
        exact = [c for c in chats if c.get("title", "").casefold() == s.casefold()]
        if len(exact) == 1:
            return exact[0]["id"]
        if len(chats) == 1:
            return chats[0]["id"]
        options = "; ".join(f"{c['id']} = {c.get('title', '')}" for c in (exact or chats))
        raise TgError(409, f"{s!r} is ambiguous, pass a chat id instead: {options}")

    async def _resolve_sender(self, ref: int | str) -> dict:
        chat = await self._req("getChat", chat_id=await self._resolve(ref))
        if chat["type"]["@type"] == "chatTypePrivate":
            return {"@type": "messageSenderUser", "user_id": chat["type"]["user_id"]}
        return {"@type": "messageSenderChat", "chat_id": chat["id"]}

    async def _resolve_user(self, ref: int | str) -> int:
        sender = await self._resolve_sender(ref)
        if "user_id" not in sender:
            raise TgError(400, f"{ref!r} is a group or channel, not a user")
        return sender["user_id"]

    # -- reading -----------------------------------------------------------

    @op
    async def me(self) -> dict:
        return self._user_brief(await self._get_me())

    @op
    async def folders(self) -> list[dict]:
        try:
            await asyncio.wait_for(self._folders_known.wait(), 5)
        except asyncio.TimeoutError:
            pass
        return self._folders

    async def _chat_list(self, archived: bool, folder: int | str | None) -> dict:
        if folder is None:
            return {"@type": "chatListArchive" if archived else "chatListMain"}
        wanted = str(folder).casefold()
        for f in await self.folders():
            if wanted in (str(f["id"]), f["name"].casefold()):
                return {"@type": "chatListFolder", "chat_folder_id": f["id"]}
        names = ", ".join(f["name"] for f in self._folders) or "none"
        raise TgError(404, f"no folder {folder!r}; folders: {names}")

    @op
    async def chats(self, limit: int = 30, query: str | None = None, unread: bool = False,
                    archived: bool = False, folder: int | str | None = None) -> list[dict]:
        if query:
            ids = (await self._req("searchChats", query=query, limit=limit))["chat_ids"]
            if len(ids) < limit:
                more = (await self._req("searchChatsOnServer", query=query,
                                        limit=limit))["chat_ids"]
                ids += [i for i in more if i not in ids]
        else:
            want = max(limit, 300) if unread else limit
            ids = (await self._req("getChats", limit=want,
                                   chat_list=await self._chat_list(archived, folder)))["chat_ids"]
        out = []
        for cid in ids:
            c = await self._req("getChat", chat_id=cid)
            if unread and not (c.get("unread_count") or c.get("is_marked_as_unread")):
                continue
            out.append(await self._chat_brief(c))
            if len(out) >= limit:
                break
        return out

    @op
    async def chat(self, chat: int | str) -> dict:
        c = await self._req("getChat", chat_id=await self._resolve(chat))
        out = await self._chat_brief(c)
        t = c["type"]
        try:
            if t["@type"] == "chatTypeSupergroup":
                full = await self._req("getSupergroupFullInfo",
                                       supergroup_id=t["supergroup_id"])
                out["members"] = full.get("member_count")
                out["description"] = full.get("description")
            elif t["@type"] == "chatTypeBasicGroup":
                full = await self._req("getBasicGroupFullInfo",
                                       basic_group_id=t["basic_group_id"])
                out["members"] = len(full.get("members") or [])
                out["description"] = full.get("description")
            elif t["@type"] == "chatTypePrivate":
                user = await self._req("getUser", user_id=t["user_id"])
                full = await self._req("getUserFullInfo", user_id=t["user_id"])
                status = user.get("status") or {}
                out["phone"] = user.get("phone_number")
                out["contact"] = user.get("is_contact") or None
                out["status"] = status.get("@type", "").removeprefix("userStatus").lower()
                if status.get("was_online"):
                    out["last_seen"] = fmt.when(status["was_online"])
                out["description"] = fmt.markdown(full.get("bio"))
                out["common_groups"] = full.get("group_in_common_count")
                out["blocked"] = bool(full.get("block_list")) or None
        except TgError:
            pass
        settings = c.get("notification_settings") or {}
        if settings.get("mute_for") and not settings.get("use_default_mute_for"):
            out["muted"] = True
        return {k: v for k, v in out.items() if v not in ("", None, 0)}

    @op
    async def messages(self, chat: int | str, limit: int = 20, before_id: int = 0,
                       mark_read: bool = False, topic: int = 0, since: int | str | None = None,
                       until: int | str | None = None, scheduled: bool = False) -> dict:
        cid = await self._resolve(chat)
        if scheduled:
            got = (await self._req("getChatScheduledMessages", chat_id=cid))["messages"]
        else:
            got = await self._history(cid, limit, before_id, topic, since, until)
        if mark_read and got and not scheduled:
            await self._req("viewMessages", chat_id=cid,
                            message_ids=[m["id"] for m in got], force_read=True)
        return {"chat_id": cid, "chat": await self._chat_title(cid),
                "messages": [await self._message(m) for m in reversed(got)]}

    async def _history(self, cid: int, limit: int, from_id: int, topic: int,
                       since: int | str | None, until: int | str | None) -> list[dict]:
        floor = fmt.timestamp(since) if since else 0
        if until and not from_id:
            try:
                last = await self._req("getChatMessageByDate", chat_id=cid,
                                       date=fmt.timestamp(until))
            except TgError:
                return []
            from_id = last["id"] + 1
        got: list[dict] = []
        # TDLib may return fewer messages than asked for, so keep paging.
        while len(got) < limit:
            page_size = min(100, limit - len(got))
            if topic:
                page = await self._req("getForumTopicHistory", chat_id=cid,
                                       forum_topic_id=topic, from_message_id=from_id,
                                       offset=0, limit=page_size)
            else:
                page = await self._req("getChatHistory", chat_id=cid, from_message_id=from_id,
                                       offset=0, limit=page_size, only_local=False)
            fresh = [m for m in page["messages"] if m["date"] >= floor]
            got += fresh
            if not page["messages"] or len(fresh) < len(page["messages"]):
                break
            from_id = page["messages"][-1]["id"]
        return got[:limit]

    @op
    async def message(self, chat: int | str | None = None, message_id: int = 0,
                      link: str | None = None) -> dict:
        """One message in full, by chat and id or by a t.me message link."""
        if link:
            m = (await self._req("getMessageLinkInfo", url=link)).get("message")
            if not m:
                raise TgError(404, "the link does not lead to a message you can access")
        elif chat is not None and message_id:
            m = await self._req("getMessage", chat_id=await self._resolve(chat),
                                message_id=message_id)
        else:
            raise TgError(400, "give chat and message_id, or link")
        out = await self._message(m, with_chat=True)
        try:
            out["link"] = (await self._req("getMessageLink", chat_id=m["chat_id"],
                                           message_id=m["id"]))["link"]
        except TgError:
            pass
        return out

    @op
    async def search(self, query: str = "", chat: int | str | None = None, limit: int = 20,
                     filter: str | None = None, sender: int | str | None = None,
                     since: int | str | None = None, until: int | str | None = None,
                     topic: int = 0) -> list[dict]:
        kind = fmt.filter_of(filter, fmt.SEARCH_FILTERS, "searchMessagesFilter")
        floor = fmt.timestamp(since) if since else 0
        ceiling = fmt.timestamp(until) if until else 0
        found: list[dict] = []
        if chat is None:
            if sender is not None or topic:
                raise TgError(400, "sender and topic need a chat")
            offset = ""
            while len(found) < limit:
                page = await self._req("searchMessages", query=query, offset=offset,
                                       limit=min(100, limit - len(found)), filter=kind,
                                       min_date=floor, max_date=ceiling)
                found += page["messages"]
                offset = page.get("next_offset") or ""
                if not offset or not page["messages"]:
                    break
        else:
            cid = await self._resolve(chat)
            who = await self._resolve_sender(sender) if sender is not None else None
            from_id = 0
            if ceiling:
                try:
                    from_id = (await self._req("getChatMessageByDate", chat_id=cid,
                                               date=ceiling))["id"] + 1
                except TgError:
                    return []
            while len(found) < limit:
                page = await self._req("searchChatMessages", chat_id=cid, query=query,
                                       sender_id=who, from_message_id=from_id, offset=0,
                                       limit=min(100, limit - len(found)), filter=kind,
                                       topic_id=_forum(topic))
                fresh = [m for m in page["messages"] if m["date"] >= floor]
                found += fresh
                from_id = page.get("next_from_message_id") or 0
                if not from_id or len(fresh) < len(page["messages"]):
                    break
        return [await self._message(m, with_chat=chat is None)
                for m in reversed(found[:limit])]

    @op
    async def topics(self, chat: int | str, query: str = "", limit: int = 50) -> list[dict]:
        res = await self._req("getForumTopics", chat_id=await self._resolve(chat),
                              query=query, offset_date=0, offset_message_id=0,
                              offset_forum_topic_id=0, limit=min(limit, 100))
        out = []
        for t in res.get("topics", []):
            item = {"id": t["info"]["forum_topic_id"], "name": t["info"]["name"],
                    "unread": t.get("unread_count"), "closed": t["info"].get("is_closed"),
                    "pinned": t.get("is_pinned")}
            if t.get("last_message"):
                item["last_date"] = fmt.when(t["last_message"]["date"])
            out.append({k: v for k, v in item.items() if v})
        return out

    @op
    async def updates(self, after: int | None = None, timeout: float = 0,
                      chat: int | str | None = None, incoming_only: bool = False) -> dict:
        """New messages seen since cursor `after`; long-polls up to `timeout`."""
        if after is None:
            after = self._update_seq
        cid = await self._resolve(chat) if chat is not None else None
        deadline = time.monotonic() + timeout

        def pending() -> list[dict]:
            return [m for seq, m in self._updates
                    if seq > after
                    and (cid is None or m["chat_id"] == cid)
                    and not (incoming_only and m.get("is_outgoing"))]

        while not (found := pending()) and (left := deadline - time.monotonic()) > 0:
            self._update_event.clear()
            try:
                await asyncio.wait_for(self._update_event.wait(), left)
            except asyncio.TimeoutError:
                break
        return {"cursor": self._update_seq,
                "messages": [await self._message(m, with_chat=True) for m in found]}

    @op
    async def download(self, chat: int | str, message_id: int,
                       out_dir: str | None = None) -> dict:
        cid = await self._resolve(chat)
        m = await self._req("getMessage", chat_id=cid, message_id=message_id)
        file, name = fmt.file_of(m["content"])
        if not file:
            raise TgError(400, "this message has no downloadable file")
        done = await self._req("downloadFile", timeout=1800, file_id=file["id"], priority=1,
                               offset=0, limit=0, synchronous=True)
        path = Path(done["local"]["path"])
        if out_dir:
            target = Path(out_dir).expanduser() / (name or path.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            path = target
        return {"path": str(path), "size": path.stat().st_size}

    # -- writing -----------------------------------------------------------

    def _send_done(self, key: tuple[int, int], result: dict | TgError) -> None:
        waiter = self._send_waiters.pop(key, None)
        if waiter and not waiter.done():
            if isinstance(result, TgError):
                waiter.set_exception(result)
            else:
                waiter.set_result(result)
            return
        self._send_results[key] = result
        while len(self._send_results) > 200:
            self._send_results.popitem(last=False)

    async def _sent(self, m: dict, timeout: float) -> dict:
        """Wait until the server has accepted a message TDLib queued locally."""
        if not m.get("sending_state"):
            return m
        key = (m["chat_id"], m["id"])
        if key in self._send_results:
            result = self._send_results.pop(key)
            if isinstance(result, TgError):
                raise result
            return result
        fut = asyncio.get_running_loop().create_future()
        self._send_waiters[key] = fut
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise TgError(408, "message is queued but the server has not confirmed it") from None
        finally:
            self._send_waiters.pop(key, None)

    def _envelope(self, cid: int, reply_to: int, silent: bool, schedule: int | str | None,
                  topic: int) -> dict:
        options: dict[str, Any] = {"@type": "messageSendOptions",
                                   "disable_notification": silent}
        if schedule:
            options["scheduling_state"] = {"@type": "messageSchedulingStateSendAtDate",
                                           "send_date": fmt.timestamp(schedule)}
        params: dict[str, Any] = {"chat_id": cid, "options": options}
        if reply_to:
            params["reply_to"] = {"@type": "inputMessageReplyToMessage",
                                  "message_id": reply_to}
        if topic:
            params["topic_id"] = _forum(topic)
        return params

    @op
    async def send(self, chat: int | str, text: str = "", files: list[str] | None = None,
                   reply_to: int = 0, silent: bool = False, plain: bool = False,
                   schedule: int | str | None = None, topic: int = 0,
                   as_document: bool = False, voice: bool = False,
                   no_preview: bool = False) -> dict | list[dict]:
        """Send text, one file, or an album of 2-10 files; returns the sent message(s)."""
        params = self._envelope(await self._resolve(chat), reply_to, silent, schedule, topic)
        ft = fmt.formatted(text, plain)
        files = files or []
        if len(files) > 1:
            empty = fmt.formatted("")
            contents = [fmt.input_content(f, ft if i == 0 else empty, as_document, voice)
                        for i, f in enumerate(files)]
            res = await self._req("sendMessageAlbum", input_message_contents=contents,
                                  **params)
            return [await self._message(await self._sent(m, 900), with_chat=True)
                    for m in res["messages"] if m]
        if files:
            content = fmt.input_content(files[0], ft, as_document, voice)
        elif text:
            content = {"@type": "inputMessageText", "text": ft, "clear_draft": True}
            if no_preview:
                content["link_preview_options"] = {"@type": "linkPreviewOptions",
                                                   "is_disabled": True}
        else:
            raise TgError(400, "nothing to send: give text or files")
        queued = await self._req("sendMessage", input_message_content=content, **params)
        return await self._message(await self._sent(queued, 900 if files else 60),
                                   with_chat=True)

    @op
    async def poll(self, chat: int | str, question: str, options: list[str],
                   multiple: bool = False, anonymous: bool = True, silent: bool = False,
                   topic: int = 0) -> dict:
        if not 2 <= len(options) <= 12:
            raise TgError(400, "a poll needs 2 to 12 options")
        content = {"@type": "inputMessagePoll", "question": fmt.formatted(question, True),
                   "options": [{"@type": "inputPollOption", "text": fmt.formatted(o, True)}
                               for o in options],
                   "is_anonymous": anonymous, "allows_multiple_answers": multiple,
                   "type": {"@type": "inputPollTypeRegular"}}
        params = self._envelope(await self._resolve(chat), 0, silent, None, topic)
        queued = await self._req("sendMessage", input_message_content=content, **params)
        return await self._message(await self._sent(queued, 60), with_chat=True)

    @op
    async def edit(self, chat: int | str, message_id: int, text: str,
                   plain: bool = False) -> dict:
        cid = await self._resolve(chat)
        ft = fmt.formatted(text, plain)
        current = await self._req("getMessage", chat_id=cid, message_id=message_id)
        if current["content"]["@type"] == "messageText":
            m = await self._req("editMessageText", chat_id=cid, message_id=message_id,
                                input_message_content={"@type": "inputMessageText",
                                                       "text": ft})
        else:
            m = await self._req("editMessageCaption", chat_id=cid, message_id=message_id,
                                caption=ft)
        return await self._message(m, with_chat=True)

    @op
    async def delete(self, chat: int | str, message_ids: list[int],
                     for_everyone: bool = True) -> dict:
        cid = await self._resolve(chat)
        await self._req("deleteMessages", chat_id=cid, message_ids=message_ids,
                        revoke=for_everyone)
        return {"deleted": message_ids, "chat_id": cid}

    @op
    async def forward(self, from_chat: int | str, to_chat: int | str, message_ids: list[int],
                      as_copy: bool = False, silent: bool = False) -> list[dict]:
        src, dst = await self._resolve(from_chat), await self._resolve(to_chat)
        res = await self._req("forwardMessages", chat_id=dst, from_chat_id=src,
                              message_ids=sorted(message_ids), send_copy=as_copy,
                              remove_caption=False,
                              options={"@type": "messageSendOptions",
                                       "disable_notification": silent})
        queued = [m for m in res.get("messages") or [] if m]
        if len(queued) < len(message_ids):
            raise TgError(400, f"only {len(queued)} of {len(message_ids)} messages "
                               "could be forwarded")
        return [await self._message(await self._sent(m, 60), with_chat=True) for m in queued]

    @op
    async def react(self, chat: int | str, message_id: int, emoji: str,
                    remove: bool = False) -> dict:
        cid = await self._resolve(chat)
        reaction = {"@type": "reactionTypeEmoji", "emoji": emoji}
        if remove:
            await self._req("removeMessageReaction", chat_id=cid, message_id=message_id,
                            reaction_type=reaction)
        else:
            await self._req("addMessageReaction", chat_id=cid, message_id=message_id,
                            reaction_type=reaction, is_big=False,
                            update_recent_reactions=False)
        return {"chat_id": cid, "message_id": message_id,
                "removed" if remove else "reaction": emoji}

    @op
    async def pin(self, chat: int | str, message_id: int, unpin: bool = False,
                  notify: bool = False) -> dict:
        cid = await self._resolve(chat)
        if unpin:
            await self._req("unpinChatMessage", chat_id=cid, message_id=message_id)
        else:
            await self._req("pinChatMessage", chat_id=cid, message_id=message_id,
                            disable_notification=not notify, only_for_self=False)
        return {"chat_id": cid, "message_id": message_id, "pinned": not unpin}

    @op
    async def draft(self, chat: int | str, text: str = "") -> dict:
        """Set the unsent draft of a chat; empty text clears it."""
        cid = await self._resolve(chat)
        value = None
        if text:
            value = {"@type": "draftMessage",
                     "content": {"@type": "draftMessageContentText",
                                 "text": fmt.formatted(text)}}
        await self._req("setChatDraftMessage", chat_id=cid, draft_message=value)
        return {"chat_id": cid, "draft": text}

    @op
    async def click(self, chat: int | str, message_id: int, button: int | str) -> dict:
        """Press a button under a message, by its text or its position (0-based)."""
        cid = await self._resolve(chat)
        m = await self._req("getMessage", chat_id=cid, message_id=message_id)
        flat = [b for row in fmt.buttons(m.get("reply_markup")) for b in row]
        if re.fullmatch(r"\d+", str(button)):
            found = flat[int(button):int(button) + 1]
        else:
            found = [b for b in flat if b["text"].strip().casefold() == str(button).casefold()]
        if not found:
            names = ", ".join(repr(b["text"]) for b in flat) or "none"
            raise TgError(404, f"no button {button!r}; buttons: {names}")
        kind = found[0]["type"]
        short = kind["@type"]
        if short.startswith("keyboardButtonType"):
            return await self.send(cid, found[0]["text"], plain=True)
        if short == "inlineKeyboardButtonTypeCallback":
            out: dict[str, Any] = {}
            try:
                answer = await self._req("getCallbackQueryAnswer", chat_id=cid,
                                         message_id=message_id, payload={
                                             "@type": "callbackQueryPayloadData",
                                             "data": kind["data"]})
                out = {k: v for k, v in answer.items() if v and not k.startswith("@")}
            except TgError as e:
                out["bot_error"] = e.message
            await asyncio.sleep(1)
            out["message"] = await self._message(
                await self._req("getMessage", chat_id=cid, message_id=message_id))
            return out
        if "url" in kind:
            return {"url": kind["url"]}
        if "text" in kind:
            return {"copy_text": kind["text"]}
        raise TgError(400, f"{short} buttons are not supported here; use the raw call")


class Api(ManageOps, Core):
    pass


async def dispatch(api: Core, name: str, args: dict) -> Any:
    fn = OPS.get(name)
    if fn is None:
        raise TgError(400, f"unknown operation: {name}")
    if name not in NO_AUTH:
        api._require_ready()
    try:
        inspect.signature(fn).bind(api, **args)
    except TypeError as e:
        raise TgError(400, f"bad arguments for {name}: {e}") from None
    return await fn(api, **args)
