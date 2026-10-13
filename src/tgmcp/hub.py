"""One TDLib session per profile, shared by every tg process.

TDLib allows one process per database, so the first process takes a file lock,
runs TDLib and serves a unix socket; the others proxy through it.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import time
from typing import Any

from . import config
from .api import Api, dispatch
from .tdlib import TgError

_LINE_LIMIT = 64 << 20
_TAKEOVER_TIMEOUT = 20


class _OwnerGone(Exception):
    """The owner could not be reached; nothing was sent to it."""


class Hub:
    def __init__(self, profile: str | None = None):
        self.profile = profile or config.default_profile()
        self._paths = config.paths(self.profile)
        self._api: Api | None = None
        self._server: asyncio.AbstractServer | None = None
        self._lock_fd: int | None = None
        self._mutex = asyncio.Lock()
        self._handlers: set[asyncio.Task] = set()

    @property
    def is_owner(self) -> bool:
        return self._api is not None

    async def call(self, op: str, **args: Any) -> Any:
        deadline = time.monotonic() + _TAKEOVER_TIMEOUT
        while True:
            if self._api is None:
                try:
                    return await self._remote(op, args)
                except _OwnerGone:
                    pass
                if not await self._become_owner():
                    if time.monotonic() > deadline:
                        raise TgError(503, "another tg process holds the Telegram session "
                                           "but does not answer")
                    await asyncio.sleep(0.2)
                    continue
            return await self._local(op, args)

    async def aclose(self) -> None:
        async with self._mutex:
            if self._server is not None:
                self._server.close()
                self._server = None
                pending = {t for t in self._handlers if t is not asyncio.current_task()}
                if pending:
                    await asyncio.wait(pending, timeout=30)
            await self._release()

    # -- owner side --------------------------------------------------------

    async def _become_owner(self) -> bool:
        async with self._mutex:
            if self._api is not None:
                return True
            fd = os.open(self._paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return False
            self._lock_fd = fd
            try:
                creds = config.load_credentials()
                if creds is None:
                    raise TgError(config.NOT_CONFIGURED,
                                  "Telegram API credentials are not set; run `tg login`")
                api = Api(self._paths, *creds)
                self._api = api
                await api.start()
                self._paths.socket.unlink(missing_ok=True)
                self._server = await asyncio.start_unix_server(
                    self._handle, path=str(self._paths.socket), limit=_LINE_LIMIT)
                os.chmod(self._paths.socket, 0o600)
            except BaseException:
                await self._release()
                raise
            return True

    async def _release(self) -> None:
        api, self._api = self._api, None
        if api is not None:
            await api.close()
        if self._lock_fd is not None:
            self._paths.socket.unlink(missing_ok=True)
            os.close(self._lock_fd)
            self._lock_fd = None

    async def _local(self, op: str, args: dict) -> Any:
        api = self._api
        if api is None:
            raise TgError(503, "session is shutting down")
        try:
            return await dispatch(api, op, args)
        finally:
            # Logout closes the TDLib instance for good: drop ownership so the
            # next call starts a fresh one.
            if api.closed and self._api is api:
                self._spawn_reset()

    def _spawn_reset(self) -> None:
        task = asyncio.ensure_future(self.aclose())
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._handlers.add(task)
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                req = json.loads(line)
                reply = {"ok": True,
                         "result": await self._local(req["op"], req.get("args") or {})}
            except TgError as e:
                reply = {"ok": False, "code": e.code, "message": e.message}
            except Exception as e:  # keep the owner alive on a bad request
                reply = {"ok": False, "code": 500, "message": f"{type(e).__name__}: {e}"}
            writer.write(json.dumps(reply, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._handlers.discard(task)
            writer.close()

    # -- client side -------------------------------------------------------

    async def _remote(self, op: str, args: dict) -> Any:
        try:
            reader, writer = await asyncio.open_unix_connection(
                str(self._paths.socket), limit=_LINE_LIMIT)
        except OSError:
            raise _OwnerGone from None
        try:
            writer.write(json.dumps({"op": op, "args": args}).encode() + b"\n")
            await writer.drain()
            line = await reader.readline()
        except ConnectionError:
            line = b""
        finally:
            writer.close()
        if not line:
            raise TgError(502, "the tg process holding the session went away mid-request; "
                               f"{op} may or may not have been applied")
        reply = json.loads(line)
        if not reply["ok"]:
            raise TgError(reply["code"], reply["message"])
        return reply["result"]
