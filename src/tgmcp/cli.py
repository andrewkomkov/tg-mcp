"""`tg`: Telegram from the terminal, and `tg mcp` to serve it to an MCP client."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import click

from . import __version__, config
from .hub import Hub
from .manage import CHAT_ACTIONS, MEMBER_ACTIONS
from .tdlib import TgError


@dataclass
class Ctx:
    profile: str | None
    as_json: bool


def _run(ctx: Ctx, work: Callable[[Hub], Awaitable[Any]]) -> Any:
    async def go():
        hub = Hub(ctx.profile)
        # SIGTERM must still close TDLib cleanly, like Ctrl-C does.
        asyncio.get_running_loop().add_signal_handler(
            signal.SIGTERM, asyncio.current_task().cancel)
        try:
            return await work(hub)
        finally:
            await hub.aclose()

    try:
        return asyncio.run(go())
    except TgError as e:
        raise click.ClickException(str(e)) from None
    except KeyboardInterrupt:
        sys.exit(130)
    except asyncio.CancelledError:
        sys.exit(143)


def _call(ctx: Ctx, op: str, **args: Any) -> Any:
    return _run(ctx, lambda hub: hub.call(op, **args))


def _emit(ctx: Ctx, data: Any, human: Callable[[Any], None]) -> None:
    if ctx.as_json:
        click.echo(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        human(data)


def _print_kv(data: dict) -> None:
    for k, v in data.items():
        click.echo(f"{k}: {v}")


def _print_message(m: dict) -> None:
    head = f"[{m['date']}] #{m['id']}"
    if "chat" in m:
        head += f" {m['chat']} |"
    who = "me" if m.get("out") else m.get("from", "")
    body = m.get("text", "")
    if m.get("media"):
        name = (m.get("file") or {}).get("name")
        body = f"[{m['media']}{': ' + name if name else ''}] {body}".rstrip()
    flags = "".join(f" ({f})" for f in ("edited", "forwarded", "pinned") if m.get(f))
    if m.get("scheduled"):
        flags += f" (scheduled {m['scheduled']})"
    click.echo(f"{head} {who}{flags}: {body}")
    for o in m.get("poll", []):
        click.echo(f"    - {o['option']} ({o['votes']})")
    if m.get("reactions"):
        click.echo("    " + " ".join(f"{e}{n}" for e, n in m["reactions"].items()))
    for row in m.get("buttons", []):
        click.echo("    " + " ".join(f"[{b}]" for b in row))


def _print_messages(ms: list[dict]) -> None:
    for m in ms:
        _print_message(m)


def _print_rows(rows: list[dict]) -> None:
    for r in rows:
        click.echo("  ".join(f"{k}={v}" for k, v in r.items()))


def _print_sent(data: Any) -> None:
    _print_messages(data if isinstance(data, list) else [data])


def _print_chats(chats: list[dict]) -> None:
    for c in chats:
        unread = f"{c['unread']:>4}" if c.get("unread") else "    "
        user = f" @{c['username']}" if c.get("username") else ""
        last = f"  — {c['last']}" if c.get("last") else ""
        click.echo(f"{c['id']:>15} {unread} {c['type']:<10} {c['title']}{user}{last}")


CHAT_HELP = "CHAT is an id, @username, t.me link, +phone, 'me' or a chat title."


class _Command(click.Command):
    """Lets a negative chat id such as -1001234567890 be given as a plain argument."""

    def __init__(self, *args: Any, **kwargs: Any):
        kwargs.setdefault("context_settings", {})["ignore_unknown_options"] = True
        super().__init__(*args, **kwargs)


class _Group(click.Group):
    command_class = _Command


@click.group(cls=_Group, context_settings={"help_option_names": ["-h", "--help"]},
             epilog=CHAT_HELP)
@click.option("-p", "--profile", default=None, help="Account profile (env TG_PROFILE).")
@click.option("--json", "as_json", is_flag=True, help="Print raw JSON.")
@click.version_option(__version__, prog_name="tg")
@click.pass_context
def main(ctx: click.Context, profile: str | None, as_json: bool) -> None:
    """Telegram client and MCP server on top of TDLib."""
    ctx.obj = Ctx(profile, as_json)


# -- account -------------------------------------------------------------

async def _ask(text: str, flag: str, hide: bool = False) -> str:
    if not sys.stdin.isatty():
        raise click.ClickException(
            f"{text} is needed but there is no terminal to ask in. Run `tg login` in a "
            f"terminal, or continue within 5 minutes with `tg login {flag} ...`")
    return await asyncio.to_thread(click.prompt, text, hide_input=hide, type=str)


def _show_qr(link: str) -> None:
    import segno

    segno.make(link).terminal(compact=True, border=1)
    click.echo("Telegram → Settings → Devices → Link Desktop Device, scan the code.")


async def _login(hub: Hub, phone: str | None, qr: bool, code: str | None,
                 password: str | None) -> dict:
    state = await hub.call("auth_state")
    while True:
        name = state["state"]
        try:
            if name == "Ready":
                return await hub.call("me")
            if name == "WaitPhoneNumber":
                if qr:
                    state = await hub.call("auth_qr")
                else:
                    phone = phone or await _ask("Phone number (international format)", "--phone")
                    state, phone = await hub.call("auth_phone", phone=phone), None
            elif name == "WaitOtherDeviceConfirmation":
                _show_qr(state["link"])
                state = await hub.call("auth_wait", seq=state["seq"], timeout=120)
            elif name in ("WaitCode", "WaitEmailCode"):
                via = state.get("code_via") or "email"
                code = code or await _ask(f"Login code ({via})", "--code")
                state, code = await hub.call("auth_code", code=code), None
            elif name == "WaitPassword":
                hint = f" (hint: {state['password_hint']})" if state.get("password_hint") else ""
                password = password or await _ask(f"2FA password{hint}", "--password",
                                                  hide=True)
                state, password = await hub.call("auth_password", password=password), None
            else:
                raise click.ClickException(f"cannot continue from login state {name}")
        except TgError as e:
            click.echo(f"Error: {e}", err=True)
            state = await hub.call("auth_state")


@main.command()
@click.option("--phone", help="Phone number in international format.")
@click.option("--code", help="Login code Telegram sent you.")
@click.option("--password", help="2FA password.")
@click.option("--qr", is_flag=True, help="Log in by scanning a QR code from the phone.")
@click.option("--api-id", type=int, help="Telegram API id (saved to config).")
@click.option("--api-hash", help="Telegram API hash (saved to config).")
@click.pass_obj
def login(ctx: Ctx, phone: str | None, code: str | None, password: str | None, qr: bool,
          api_id: int | None, api_hash: str | None) -> None:
    """Log in to Telegram (interactive)."""
    if api_id and api_hash:
        config.save_credentials(api_id, api_hash)
    elif config.load_credentials() is None:
        click.echo("Telegram needs an API id/hash of your own: create them once at "
                   "https://my.telegram.org/apps")
        config.save_credentials(click.prompt("api_id", type=int),
                                click.prompt("api_hash", type=str).strip())
    me = _run(ctx, lambda hub: _login(hub, phone, qr, code, password))
    click.echo(f"Logged in as {me.get('name')} ({me.get('phone', me['id'])}).")


@main.command()
@click.confirmation_option(prompt="Log out and delete the local session?")
@click.pass_obj
def logout(ctx: Ctx) -> None:
    """Log out and wipe the local session."""
    _call(ctx, "logout")
    click.echo("Logged out.")


@main.command()
@click.pass_obj
def status(ctx: Ctx) -> None:
    """Login state, TDLib version, session owner."""
    async def work(hub: Hub) -> dict:
        data = await hub.call("status")
        data["via"] = "this process" if hub.is_owner else "running tg process"
        return data

    _emit(ctx, _run(ctx, work), _print_kv)


@main.command()
@click.pass_obj
def me(ctx: Ctx) -> None:
    """Show the logged-in account."""
    _emit(ctx, _call(ctx, "me"), _print_kv)


# -- reading -------------------------------------------------------------

@main.command()
@click.option("-n", "--limit", default=30, show_default=True)
@click.option("-q", "--query", help="Search chats by title or username.")
@click.option("--unread", is_flag=True, help="Only chats with unread messages.")
@click.option("--archived", is_flag=True, help="List the archive folder.")
@click.option("--folder", help="List a chat folder, by name or id.")
@click.pass_obj
def chats(ctx: Ctx, **opts: Any) -> None:
    """List chats."""
    _emit(ctx, _call(ctx, "chats", **opts), _print_chats)


@main.command()
@click.argument("chat")
@click.pass_obj
def chat(ctx: Ctx, chat: str) -> None:
    """Show details of one chat."""
    _emit(ctx, _call(ctx, "chat", chat=chat), _print_kv)


@main.command()
@click.argument("chat")
@click.option("-n", "--limit", default=20, show_default=True)
@click.option("--before", "before_id", default=0, help="Page back from this message id.")
@click.option("--mark-read", is_flag=True, help="Mark the shown messages as read.")
@click.option("--topic", default=0, help="Forum topic id.")
@click.option("--since", help="Not older than this time (2026-01-31, -7d).")
@click.option("--until", help="Not newer than this time.")
@click.option("--scheduled", is_flag=True, help="Show your scheduled messages instead.")
@click.pass_obj
def read(ctx: Ctx, **opts: Any) -> None:
    """Read the latest messages of a chat."""
    def human(data: dict) -> None:
        click.echo(f"{data['chat']} ({data['chat_id']})")
        _print_messages(data["messages"])

    _emit(ctx, _call(ctx, "messages", **opts), human)


@main.command()
@click.argument("query", default="")
@click.option("-c", "--chat", help="Search inside this chat only.")
@click.option("-n", "--limit", default=20, show_default=True)
@click.option("--filter", help="photo, video, media, document, audio, voice, link, gif, "
                               "poll, pinned, mention.")
@click.option("--sender", help="Only messages of this user (needs --chat).")
@click.option("--since", help="Not older than this time (2026-01-31, -7d).")
@click.option("--until", help="Not newer than this time.")
@click.pass_obj
def search(ctx: Ctx, **opts: Any) -> None:
    """Search messages."""
    _emit(ctx, _call(ctx, "search", **opts), _print_messages)


@main.command()
@click.option("-c", "--chat", help="Only this chat.")
@click.option("--incoming", is_flag=True, help="Skip your own messages.")
@click.pass_obj
def watch(ctx: Ctx, chat: str | None, incoming: bool) -> None:
    """Print new messages as they arrive (Ctrl-C to stop)."""
    async def work(hub: Hub) -> None:
        cursor = None
        while True:
            got = await hub.call("updates", after=cursor, timeout=25, chat=chat,
                                 incoming_only=incoming)
            cursor = got["cursor"]
            for m in got["messages"]:
                if ctx.as_json:
                    click.echo(json.dumps(m, ensure_ascii=False))
                else:
                    _print_message(m)

    _run(ctx, work)


@main.command()
@click.argument("chat")
@click.argument("message_id", type=int)
@click.option("-o", "--out-dir", type=click.Path(file_okay=False, resolve_path=True),
              help="Copy the file here.")
@click.pass_obj
def download(ctx: Ctx, **opts: Any) -> None:
    """Download the file attached to a message."""
    _emit(ctx, _call(ctx, "download", **opts), lambda d: click.echo(d["path"]))


# -- writing -------------------------------------------------------------

@main.command()
@click.argument("chat")
@click.argument("text", nargs=-1)
@click.option("-f", "--file", "files", multiple=True,
              type=click.Path(exists=True, dir_okay=False, resolve_path=True),
              help="Attach a file; repeat for an album.")
@click.option("--schedule", help="Send later (2026-01-31T18:00, +2h).")
@click.option("--topic", default=0, help="Forum topic id.")
@click.option("--as-document", is_flag=True, help="Send files without compression.")
@click.option("--voice", is_flag=True, help="Send the audio file as a voice message.")
@click.option("--no-preview", is_flag=True, help="Do not show a link preview.")
@click.option("--reply", "reply_to", default=0, help="Reply to this message id.")
@click.option("--silent", is_flag=True, help="Send without notification.")
@click.option("--plain", is_flag=True, help="Do not parse Markdown.")
@click.pass_obj
def send(ctx: Ctx, chat: str, text: tuple[str, ...], files: tuple[str, ...],
         **opts: Any) -> None:
    """Send a message. TEXT is read from stdin when omitted."""
    body = " ".join(text)
    if not body and not files and not sys.stdin.isatty():
        body = sys.stdin.read().rstrip("\n")
    _emit(ctx, _call(ctx, "send", chat=chat, text=body, files=list(files), **opts),
          _print_sent)


@main.command()
@click.argument("chat")
@click.argument("message_id", type=int)
@click.argument("text")
@click.option("--plain", is_flag=True, help="Do not parse Markdown.")
@click.pass_obj
def edit(ctx: Ctx, **opts: Any) -> None:
    """Edit the text or caption of your message."""
    _emit(ctx, _call(ctx, "edit", **opts), _print_message)


@main.command()
@click.argument("chat")
@click.argument("message_ids", type=int, nargs=-1, required=True)
@click.option("--for-me", is_flag=True, help="Delete only for yourself.")
@click.pass_obj
def delete(ctx: Ctx, chat: str, message_ids: tuple[int, ...], for_me: bool) -> None:
    """Delete messages (for everyone unless --for-me)."""
    _emit(ctx, _call(ctx, "delete", chat=chat, message_ids=list(message_ids),
                     for_everyone=not for_me),
          lambda d: click.echo(f"Deleted {len(d['deleted'])} message(s)."))


@main.command()
@click.argument("from_chat")
@click.argument("to_chat")
@click.argument("message_ids", type=int, nargs=-1, required=True)
@click.option("--copy", "as_copy", is_flag=True, help="Send as a copy, without the author.")
@click.pass_obj
def forward(ctx: Ctx, from_chat: str, to_chat: str, message_ids: tuple[int, ...],
            as_copy: bool) -> None:
    """Forward messages from one chat to another."""
    _emit(ctx, _call(ctx, "forward", from_chat=from_chat, to_chat=to_chat,
                     message_ids=list(message_ids), as_copy=as_copy), _print_messages)


@main.command()
@click.argument("chat")
@click.argument("message_id", type=int)
@click.argument("emoji")
@click.option("--remove", is_flag=True, help="Remove the reaction instead.")
@click.pass_obj
def react(ctx: Ctx, **opts: Any) -> None:
    """Add (or remove) an emoji reaction on a message."""
    _emit(ctx, _call(ctx, "react", **opts), lambda d: click.echo("Done."))


@main.command("mark-read")
@click.argument("chat")
@click.pass_obj
def mark_read(ctx: Ctx, chat: str) -> None:
    """Mark a chat as read."""
    _emit(ctx, _call(ctx, "mark_read", chat=chat), lambda d: click.echo("Done."))


@main.command()
@click.argument("chat")
@click.argument("message_id", type=int)
@click.option("--unpin", is_flag=True)
@click.option("--notify", is_flag=True, help="Notify the chat members.")
@click.pass_obj
def pin(ctx: Ctx, **opts: Any) -> None:
    """Pin (or unpin) a message."""
    _emit(ctx, _call(ctx, "pin", **opts), lambda d: click.echo("Done."))


@main.command()
@click.argument("chat")
@click.argument("text", default="")
@click.pass_obj
def draft(ctx: Ctx, **opts: Any) -> None:
    """Set the draft of a chat; no TEXT clears it."""
    _emit(ctx, _call(ctx, "draft", **opts), lambda d: click.echo("Done."))


@main.command()
@click.argument("chat")
@click.argument("question")
@click.argument("options", nargs=-1, required=True)
@click.option("--multiple", is_flag=True, help="Allow several answers.")
@click.option("--public", is_flag=True, help="Show who voted.")
@click.pass_obj
def poll(ctx: Ctx, chat: str, question: str, options: tuple[str, ...], multiple: bool,
         public: bool) -> None:
    """Send a poll: tg poll CHAT "Question?" "Option 1" "Option 2"."""
    _emit(ctx, _call(ctx, "poll", chat=chat, question=question, options=list(options),
                     multiple=multiple, anonymous=not public), _print_message)


@main.command("click")
@click.argument("chat")
@click.argument("message_id", type=int)
@click.argument("button")
@click.pass_obj
def click_button(ctx: Ctx, **opts: Any) -> None:
    """Press a bot button, by its text or 0-based position."""
    def human(d: dict) -> None:
        message = d.pop("message", None)
        if "id" in d:
            _print_message(d)
        else:
            _print_kv(d)
        if message:
            _print_message(message)

    _emit(ctx, _call(ctx, "click", **opts), human)


# -- chats and people ----------------------------------------------------

@main.command()
@click.pass_obj
def folders(ctx: Ctx) -> None:
    """List chat folders."""
    _emit(ctx, _call(ctx, "folders"), _print_rows)


@main.command()
@click.argument("chat", required=False)
@click.argument("message_id", type=int, default=0)
@click.option("--link", help="A t.me message link instead of CHAT and MESSAGE_ID.")
@click.pass_obj
def message(ctx: Ctx, **opts: Any) -> None:
    """Show one message in full."""
    def human(d: dict) -> None:
        _print_message(d)
        if d.get("link"):
            click.echo(f"    {d['link']}")

    _emit(ctx, _call(ctx, "message", **opts), human)


@main.command()
@click.argument("chat")
@click.option("-q", "--query", default="")
@click.option("-n", "--limit", default=50, show_default=True)
@click.pass_obj
def topics(ctx: Ctx, **opts: Any) -> None:
    """List the topics of a forum group."""
    _emit(ctx, _call(ctx, "topics", **opts), _print_rows)


@main.command()
@click.argument("chat")
@click.option("-q", "--query", default="")
@click.option("-n", "--limit", default=50, show_default=True)
@click.option("--filter", help="admins, bots, banned, restricted, contacts.")
@click.pass_obj
def members(ctx: Ctx, **opts: Any) -> None:
    """List members of a group or channel."""
    _emit(ctx, _call(ctx, "members", **opts), _print_rows)


@main.command()
@click.argument("chat")
@click.argument("user")
@click.argument("action", type=click.Choice(MEMBER_ACTIONS))
@click.pass_obj
def member(ctx: Ctx, **opts: Any) -> None:
    """Add, kick, ban, unban, promote or demote a member."""
    _emit(ctx, _call(ctx, "member", **opts), lambda d: click.echo("Done."))


@main.command("chat-action")
@click.argument("chat")
@click.argument("action", type=click.Choice(CHAT_ACTIONS))
@click.option("--seconds", default=0, help="Mute duration; 0 mutes for good.")
@click.option("-y", "--yes", is_flag=True, help="Do not ask before leave/clear/delete.")
@click.pass_obj
def chat_action(ctx: Ctx, chat: str, action: str, seconds: int, yes: bool) -> None:
    """Mute, archive, pin, block, leave, clear or delete a chat."""
    if action in ("leave", "clear_history", "delete") and not yes:
        click.confirm(f"{action} '{chat}'? This cannot be undone", abort=True)
    _emit(ctx, _call(ctx, "chat_action", chat=chat, action=action, seconds=seconds),
          lambda d: click.echo("Done."))


@main.command()
@click.argument("target")
@click.pass_obj
def join(ctx: Ctx, target: str) -> None:
    """Join a chat by invite link, @username or t.me link."""
    _emit(ctx, _call(ctx, "join", target=target), _print_kv)


@main.command()
@click.argument("title")
@click.option("--kind", type=click.Choice(["group", "channel", "forum"]), default="group")
@click.option("-m", "--member", "members", multiple=True, help="User to add; repeatable.")
@click.option("--description", default="")
@click.pass_obj
def create(ctx: Ctx, title: str, kind: str, members: tuple[str, ...],
           description: str) -> None:
    """Create a group, channel or forum."""
    _emit(ctx, _call(ctx, "create_chat", title=title, kind=kind, members=list(members),
                     description=description), _print_kv)


@main.command("edit-chat")
@click.argument("chat")
@click.option("--title")
@click.option("--description")
@click.option("--photo", type=click.Path(exists=True, dir_okay=False, resolve_path=True))
@click.pass_obj
def edit_chat(ctx: Ctx, **opts: Any) -> None:
    """Change the title, description or photo of a chat."""
    _emit(ctx, _call(ctx, "edit_chat", **opts), _print_kv)


@main.command("invite-link")
@click.argument("chat")
@click.option("--name", default="")
@click.pass_obj
def invite_link(ctx: Ctx, **opts: Any) -> None:
    """Create an invite link."""
    _emit(ctx, _call(ctx, "invite_link", **opts), lambda d: click.echo(d["link"]))


@main.command()
@click.option("-q", "--query", default="")
@click.option("-n", "--limit", default=100, show_default=True)
@click.pass_obj
def contacts(ctx: Ctx, **opts: Any) -> None:
    """List contacts."""
    _emit(ctx, _call(ctx, "contacts", **opts), _print_rows)


@main.command()
@click.argument("action", type=click.Choice(["add", "remove"]))
@click.argument("user", required=False)
@click.option("--phone", default="")
@click.option("--first-name", default="")
@click.option("--last-name", default="")
@click.pass_obj
def contact(ctx: Ctx, **opts: Any) -> None:
    """Add a contact (USER, or --phone with --first-name) or remove one."""
    _emit(ctx, _call(ctx, "contact", **opts), _print_kv)


@main.command()
@click.option("--first-name")
@click.option("--last-name")
@click.option("--bio")
@click.option("--username")
@click.pass_obj
def profile(ctx: Ctx, **opts: Any) -> None:
    """Show your profile, or change the given fields."""
    _emit(ctx, _call(ctx, "profile", **opts), _print_kv)


@main.command()
@click.pass_obj
def sessions(ctx: Ctx) -> None:
    """List devices logged in to the account."""
    _emit(ctx, _call(ctx, "sessions"), _print_rows)


# -- plumbing ------------------------------------------------------------

@main.command()
@click.argument("method")
@click.argument("params", default="{}")
@click.option("--unsafe", is_flag=True, help="Allow account/session-destroying methods.")
@click.pass_obj
def call(ctx: Ctx, method: str, params: str, unsafe: bool) -> None:
    """Call any TDLib method: tg call getOption '{"name": "version"}'."""
    try:
        parsed = json.loads(params)
    except ValueError as e:
        raise click.BadParameter(f"PARAMS must be a JSON object: {e}") from None
    result = _call(ctx, "call", method=method, params=parsed, unsafe=unsafe)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@main.group()
def api() -> None:
    """Look up the TDLib API of the installed version."""


@api.command("search")
@click.argument("query")
@click.option("-n", "--limit", default=15, show_default=True)
@click.option("--types", is_flag=True, help="Search types instead of methods.")
@click.pass_obj
def api_search(ctx: Ctx, **opts: Any) -> None:
    """Find methods by keywords."""
    def human(rows: list[dict]) -> None:
        for r in rows:
            click.echo(r["signature"])
            click.echo(f"    {r['description']}")

    _emit(ctx, _call(ctx, "api_search", **opts), human)


@api.command("show")
@click.argument("name")
@click.pass_obj
def api_show(ctx: Ctx, name: str) -> None:
    """Show the fields of a method, type or class."""
    def human(d: dict) -> None:
        click.echo(f"{d['kind']} {d['name']}: {d['description']}")
        for c in d.get("constructors", []):
            click.echo(f"  {c}")
        for f in d.get("params", []):
            click.echo(f"  {f['name']}: {f['type']} — {f['description']}")
        if d.get("returns"):
            click.echo(f"  -> {d['returns']}")

    _emit(ctx, _call(ctx, "api_describe", name=name), human)


@main.command()
@click.pass_obj
def selfcheck(ctx: Ctx) -> None:
    """Check this tool against the API schema of the installed TDLib."""
    data = _call(ctx, "selfcheck")
    _emit(ctx, data, _print_kv)
    if data["problems"]:
        sys.exit(1)


@main.command()
@click.pass_obj
def serve(ctx: Ctx) -> None:
    """Keep the session running so other tg commands answer instantly."""
    async def work(hub: Hub) -> None:
        info = await hub.call("status")
        if not hub.is_owner:
            click.echo(f"Already served by pid {info['owner_pid']}.")
            return
        click.echo(f"Serving profile '{hub.profile}' (pid {os.getpid()}, "
                   f"TDLib {info['tdlib']}, {info['auth']}). Ctrl-C to stop.", err=True)
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        await stop.wait()

    _run(ctx, work)


@main.command()
@click.option("--read-only", is_flag=True, help="Expose only tools that do not change anything.")
@click.pass_obj
def mcp(ctx: Ctx, read_only: bool) -> None:
    """Run as an MCP server on stdio."""
    from .mcp_server import serve as serve_mcp

    serve_mcp(ctx.profile, read_only)


if __name__ == "__main__":
    main()
