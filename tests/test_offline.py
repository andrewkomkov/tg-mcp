"""Checks that need neither a Telegram account nor credentials."""

import asyncio

import pytest

from tgmcp import config, fmt
from tgmcp.hub import Hub
from tgmcp.mcp_server import build
from tgmcp.tdlib import TgError, execute


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_MCP_HOME", str(tmp_path))
    monkeypatch.delenv("TG_API_ID", raising=False)
    monkeypatch.delenv("TG_API_HASH", raising=False)
    return tmp_path


def test_tdlib_loads():
    assert execute("getOption", name="version")["value"].startswith("1.")


def test_markdown_round_trip():
    ft = fmt.formatted("**bold** and [link](https://example.com)")
    assert ft["text"] == "bold and link"
    assert fmt.markdown(ft) == "**bold** and [link](https://example.com/)"
    assert fmt.formatted("**raw**", plain=True)["text"] == "**raw**"


def test_content_summary():
    doc = {"@type": "messageDocument",
           "document": {"file_name": "a.pdf", "document": {"id": 7, "size": 10}},
           "caption": {"@type": "formattedText", "text": "see", "entities": []}}
    assert fmt.content(doc) == {"media": "document", "text": "see",
                                "file": {"name": "a.pdf", "size": 10}}
    assert fmt.file_of(doc)[0]["id"] == 7
    assert fmt.content({"@type": "messageChatJoinByLink"}) == {"media": "chatJoinByLink"}


def test_timestamp_forms():
    assert fmt.timestamp(1700000000) == 1700000000
    assert fmt.timestamp("2026-01-31T18:00") - fmt.timestamp("2026-01-31") == 18 * 3600
    assert fmt.timestamp("+2h") - fmt.timestamp("-2h") in (4 * 3600, 4 * 3600 + 1)
    with pytest.raises(TgError):
        fmt.timestamp("tomorrow")


def test_file_kind_follows_extension(tmp_path):
    kinds = {}
    for name in ("a.jpg", "a.mp4", "a.gif", "a.mp3", "a.ogg", "a.pdf"):
        (tmp_path / name).write_bytes(b"x")
        kinds[name] = fmt.input_content(str(tmp_path / name), {}, False, False)["@type"]
    assert kinds == {"a.jpg": "inputMessagePhoto", "a.mp4": "inputMessageVideo",
                     "a.gif": "inputMessageAnimation", "a.mp3": "inputMessageAudio",
                     "a.ogg": "inputMessageVoiceNote", "a.pdf": "inputMessageDocument"}
    forced = fmt.input_content(str(tmp_path / "a.jpg"), {}, True, False)
    assert forced["@type"] == "inputMessageDocument"
    with pytest.raises(TgError):
        fmt.input_content(str(tmp_path / "missing.jpg"), {}, False, False)


def test_credentials_are_private(home):
    assert config.load_credentials() is None
    config.save_credentials(123, "abc")
    assert config.load_credentials() == (123, "abc")
    assert (home / "config.json").stat().st_mode & 0o777 == 0o600


def test_long_socket_path_falls_back(home):
    deep = config.Paths(home / ("x" * 120))
    assert len(str(deep.socket)) < 100


def test_hub_without_credentials(home):
    async def go():
        hub = Hub()
        try:
            with pytest.raises(TgError) as e:
                await hub.call("status")
            assert e.value.code == config.NOT_CONFIGURED
            assert not hub.is_owner
        finally:
            await hub.aclose()

    asyncio.run(go())


def test_read_only_server_has_no_write_tools(home):
    async def tools(read_only):
        return {t.name: t for t in await build(None, read_only).list_tools()}

    full, ro = asyncio.run(tools(False)), asyncio.run(tools(True))
    assert {"tg_send_message", "tg_delete_messages", "tg_call"} <= set(full)
    assert "tg_login" in ro and set(ro) < set(full)
    assert all(t.annotations.read_only_hint for name, t in ro.items() if name != "tg_login")
    assert all(t.description for t in full.values())
