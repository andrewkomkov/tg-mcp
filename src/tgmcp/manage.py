"""Operations that manage chats, members, contacts and the account itself."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import fmt
from .ops import op
from .tdlib import TgError

_FOREVER = 400 * 86400
_ADMIN_RIGHTS = ("can_manage_chat", "can_change_info", "can_delete_messages",
                 "can_invite_users", "can_restrict_members", "can_pin_messages",
                 "can_manage_video_chats", "can_post_messages", "can_edit_messages")
CHAT_ACTIONS = ("mark_read", "mark_unread", "mute", "unmute", "archive", "unarchive", "pin",
                "unpin", "block", "unblock", "leave", "clear_history", "delete")
MEMBER_ACTIONS = ("add", "kick", "ban", "unban", "promote", "demote")


class ManageOps:
    @op
    async def mark_read(self, chat: int | str) -> dict:
        cid = await self._resolve(chat)
        c = await self._req("getChat", chat_id=cid)
        last = c.get("last_message")
        if last:
            await self._req("viewMessages", chat_id=cid, message_ids=[last["id"]],
                            force_read=True)
        if c.get("is_marked_as_unread"):
            await self._req("toggleChatIsMarkedAsUnread", chat_id=cid,
                            is_marked_as_unread=False)
        await self._req("readAllChatMentions", chat_id=cid)
        return {"chat_id": cid, "done": "mark_read"}

    @op
    async def chat_action(self, chat: int | str, action: str, seconds: int = 0) -> dict:
        """One of CHAT_ACTIONS; `seconds` limits a mute (0 mutes for good)."""
        if action not in CHAT_ACTIONS:
            raise TgError(400, f"unknown action {action!r}; one of: {', '.join(CHAT_ACTIONS)}")
        if action == "mark_read":
            return await self.mark_read(chat)
        cid = await self._resolve(chat)
        if action == "mark_unread":
            await self._req("toggleChatIsMarkedAsUnread", chat_id=cid, is_marked_as_unread=True)
        elif action in ("mute", "unmute"):
            await self._req("setChatNotificationSettings", chat_id=cid, notification_settings={
                "@type": "chatNotificationSettings", "use_default_mute_for": False,
                "mute_for": (seconds or _FOREVER) if action == "mute" else 0,
                "use_default_sound": True, "use_default_show_preview": True,
                "use_default_mute_stories": True, "use_default_story_sound": True,
                "use_default_show_story_poster": True,
                "use_default_disable_pinned_message_notifications": True,
                "use_default_disable_mention_notifications": True})
        elif action in ("archive", "unarchive"):
            kind = "chatListArchive" if action == "archive" else "chatListMain"
            await self._req("addChatToList", chat_id=cid, chat_list={"@type": kind})
        elif action in ("pin", "unpin"):
            c = await self._req("getChat", chat_id=cid)
            lists = [p["list"] for p in c.get("positions") or []] or [{"@type": "chatListMain"}]
            await self._req("toggleChatIsPinned", chat_id=cid, chat_list=lists[0],
                            is_pinned=action == "pin")
        elif action in ("block", "unblock"):
            await self._req("setMessageSenderBlockList",
                            sender_id=await self._resolve_sender(cid),
                            block_list={"@type": "blockListMain"} if action == "block" else None)
        elif action == "leave":
            await self._req("leaveChat", chat_id=cid)
        elif action == "clear_history":
            await self._req("deleteChatHistory", chat_id=cid, remove_from_chat_list=False,
                            revoke=False)
        elif action == "delete":
            await self._delete_chat(cid)
        return {"chat_id": cid, "done": action}

    async def _delete_chat(self, cid: int) -> None:
        c = await self._req("getChat", chat_id=cid)
        if c.get("can_be_deleted_for_all_users") and c["type"]["@type"] != "chatTypePrivate":
            await self._req("deleteChat", chat_id=cid)
            return
        if c["type"]["@type"] in ("chatTypeBasicGroup", "chatTypeSupergroup"):
            try:
                await self._req("leaveChat", chat_id=cid)
            except TgError:
                pass
        await self._req("deleteChatHistory", chat_id=cid, remove_from_chat_list=True,
                        revoke=False)

    @op
    async def join(self, target: str) -> dict:
        """Join a chat by invite link, @username or t.me link."""
        if re.search(r"t\.me/(\+|joinchat/)", target) or target.startswith("tg:"):
            res = await self._req("joinChatByInviteLink", invite_link=target)
        else:
            res = await self._req("joinChat", chat_id=await self._resolve(target))
        out: dict[str, Any] = {"result": res["@type"].removeprefix("chatJoinResult").lower()}
        if res.get("chat_id"):
            out["chat_id"] = res["chat_id"]
            out["chat"] = await self._chat_title(res["chat_id"])
        return out

    @op
    async def create_chat(self, title: str, kind: str = "group",
                          members: list[int | str] | None = None,
                          description: str = "") -> dict:
        """Create a group (supergroup), a channel, or a forum (group with topics)."""
        if kind not in ("group", "channel", "forum"):
            raise TgError(400, "kind is one of: group, channel, forum")
        users = [await self._resolve_user(m) for m in members or []]
        c = await self._req("createNewSupergroupChat", title=title, description=description,
                            is_channel=kind == "channel")
        if kind == "forum":
            # Creating with is_forum yields a channel on TDLib 1.8.68; convert instead.
            await self._req("toggleSupergroupIsForum", is_forum=True, has_forum_tabs=False,
                            supergroup_id=c["type"]["supergroup_id"])
        out = await self._chat_brief(c)
        if users:
            failed = await self._req("addChatMembers", chat_id=c["id"], user_ids=users)
            bad = [f["user_id"] for f in failed.get("failed_to_add_members") or []]
            if bad:
                out["not_added"] = bad
        return out

    @op
    async def edit_chat(self, chat: int | str, title: str | None = None,
                        description: str | None = None, photo: str | None = None) -> dict:
        cid = await self._resolve(chat)
        if title is not None:
            await self._req("setChatTitle", chat_id=cid, title=title)
        if description is not None:
            await self._req("setChatDescription", chat_id=cid, description=description)
        if photo:
            path = Path(photo).expanduser()
            if not path.is_file():
                raise TgError(400, f"no such file: {path}")
            await self._req("setChatPhoto", chat_id=cid, photo={
                "@type": "inputChatPhotoStatic",
                "photo": {"@type": "inputFileLocal", "path": str(path)}})
        return await self.chat(cid)

    @op
    async def invite_link(self, chat: int | str, name: str = "") -> dict:
        link = await self._req("createChatInviteLink", chat_id=await self._resolve(chat),
                               name=name, expiration_date=0, member_limit=0,
                               creates_join_request=False)
        return {"link": link["invite_link"]}

    @op
    async def members(self, chat: int | str, query: str = "", limit: int = 50,
                      filter: str | None = None) -> list[dict]:
        res = await self._req("searchChatMembers", chat_id=await self._resolve(chat),
                              query=query, limit=min(limit, 200),
                              filter=fmt.filter_of(filter, fmt.MEMBER_FILTERS,
                                                   "chatMembersFilter"))
        out = []
        for m in res.get("members", []):
            who = m["member_id"]
            role = m["status"]["@type"].removeprefix("chatMemberStatus").lower()
            if "user_id" in who:
                item = self._user_brief(await self._req("getUser", user_id=who["user_id"]))
                item.pop("phone", None)
            else:
                item = {"id": who["chat_id"], "name": await self._chat_title(who["chat_id"])}
            out.append({**item, "role": role})
        return out

    @op
    async def member(self, chat: int | str, user: int | str, action: str) -> dict:
        """One of MEMBER_ACTIONS applied to a user in a group or channel."""
        if action not in MEMBER_ACTIONS:
            raise TgError(400, f"unknown action {action!r}; one of: {', '.join(MEMBER_ACTIONS)}")
        cid, uid = await self._resolve(chat), await self._resolve_user(user)
        who = {"@type": "messageSenderUser", "user_id": uid}

        async def status(kind: str, **fields: Any) -> None:
            await self._req("setChatMemberStatus", chat_id=cid, member_id=who,
                            status={"@type": "chatMemberStatus" + kind, **fields})

        if action == "add":
            failed = await self._req("addChatMember", chat_id=cid, user_id=uid, forward_limit=0)
            if failed.get("failed_to_add_members"):
                raise TgError(403, "the user's privacy settings do not allow adding them")
        elif action in ("ban", "kick"):
            await self._req("banChatMember", chat_id=cid, member_id=who, banned_until_date=0,
                            revoke_messages=False)
            if action == "kick":
                await status("Left")
        elif action == "unban":
            await status("Left")
        elif action == "promote":
            rights = {"@type": "chatAdministratorRights", **dict.fromkeys(_ADMIN_RIGHTS, True)}
            await status("Administrator", rights=rights)
        elif action == "demote":
            await status("Member")
        return {"chat_id": cid, "user_id": uid, "done": action}

    @op
    async def contacts(self, query: str = "", limit: int = 100) -> list[dict]:
        if query:
            ids = (await self._req("searchContacts", query=query, limit=limit))["user_ids"]
        else:
            ids = (await self._req("getContacts"))["user_ids"][:limit]
        return [self._user_brief(await self._req("getUser", user_id=i)) for i in ids]

    @op
    async def contact(self, action: str, user: int | str | None = None, phone: str = "",
                      first_name: str = "", last_name: str = "") -> dict:
        """Add a contact (by phone, or an existing user) or remove one."""
        if action == "remove":
            uid = await self._resolve_user(user)
            await self._req("removeContacts", user_ids=[uid])
            return {"user_id": uid, "done": "remove"}
        if action != "add":
            raise TgError(400, "action is one of: add, remove")
        if user is None:
            if not (phone and first_name):
                raise TgError(400, "adding by phone needs phone and first_name")
            res = await self._req("importContacts", contacts=[{
                "@type": "importedContact", "phone_number": phone,
                "first_name": first_name, "last_name": last_name}])
            uid = (res.get("user_ids") or [0])[0]
            if not uid:
                raise TgError(404, f"{phone} is not on Telegram")
        else:
            uid = await self._resolve_user(user)
            current = await self._req("getUser", user_id=uid)
            await self._req("addContact", user_id=uid, share_phone_number=False, contact={
                "@type": "importedContact", "phone_number": phone,
                "first_name": first_name or current.get("first_name", ""),
                "last_name": last_name or current.get("last_name", "")})
        return {**self._user_brief(await self._req("getUser", user_id=uid)), "done": "add"}

    @op
    async def profile(self, first_name: str | None = None, last_name: str | None = None,
                      bio: str | None = None, username: str | None = None) -> dict:
        """Change the given fields of the user's own profile; returns the profile."""
        current = await self._get_me()
        if first_name is not None or last_name is not None:
            await self._req("setName",
                            first_name=current["first_name"] if first_name is None else first_name,
                            last_name=current["last_name"] if last_name is None else last_name)
        if bio is not None:
            await self._req("setBio", bio=bio)
        if username is not None:
            await self._req("setUsername", username=username.lstrip("@"))
        self._me = None
        me = await self._get_me()
        full = await self._req("getUserFullInfo", user_id=me["id"])
        return {**self._user_brief(me), "bio": fmt.markdown(full.get("bio"))}

    @op
    async def sessions(self) -> list[dict]:
        """Devices logged in to the account."""
        res = await self._req("getActiveSessions")
        return [{"id": s["id"], "app": f"{s['application_name']} {s['application_version']}",
                 "device": s["device_model"], "platform": s["platform"],
                 "location": s["location"], "last_active": fmt.when(s["last_active_date"]),
                 **({"current": True} if s.get("is_current") else {})}
                for s in res["sessions"]]
