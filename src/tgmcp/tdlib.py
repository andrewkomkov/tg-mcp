"""Thin asyncio layer over TDLib's JSON interface.

Nothing here knows about specific TDLib methods or types: requests and
responses are plain dicts, so a newer TDLib works without code changes.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import threading
from typing import Any, Callable

import tdjson


class TgError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(f"{message} (code {code})")
        self.code = code
        self.message = message


def execute(method: str, **params: Any) -> dict:
    """Run one of TDLib's synchronous, offline methods."""
    raw = tdjson.td_execute(json.dumps({"@type": method, **params}).encode())
    if raw is None:
        raise TgError(400, f"{method} cannot be executed synchronously")
    result = json.loads(raw)
    if result.get("@type") == "error":
        raise TgError(result.get("code", 0), result.get("message", ""))
    return result


class TdClient:
    """One TDLib instance. td_receive is process-wide, so keep one per process."""

    def __init__(self, on_update: Callable[[dict], None]):
        self._loop = asyncio.get_running_loop()
        self._id = tdjson.td_create_client_id()
        self._on_update = on_update
        self._pending: dict[str, asyncio.Future] = {}
        self._seq = itertools.count(1)
        self._stopping = False
        self._thread = threading.Thread(
            target=self._receive_loop, name="tdlib-receive", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopping = True
        if self._thread.is_alive():
            self._thread.join(timeout=3)

    def _receive_loop(self) -> None:
        while not self._stopping:
            raw = tdjson.td_receive(1.0)
            if raw is None:
                continue
            try:
                self._loop.call_soon_threadsafe(self._dispatch, json.loads(raw))
            except RuntimeError:  # event loop is gone
                return

    def _dispatch(self, obj: dict) -> None:
        if obj.get("@client_id", self._id) != self._id:
            return
        extra = obj.pop("@extra", None)
        if extra is None:
            self._on_update(obj)
            return
        fut = self._pending.pop(extra, None)
        if fut is None or fut.done():
            return
        if obj.get("@type") == "error":
            fut.set_exception(TgError(obj.get("code", 0), obj.get("message", "")))
        else:
            fut.set_result(obj)

    async def request(self, method: str, timeout: float = 60, **params: Any) -> dict:
        extra = str(next(self._seq))
        fut = self._loop.create_future()
        self._pending[extra] = fut
        payload = {"@type": method, **params, "@extra": extra}
        tdjson.td_send(self._id, json.dumps(payload).encode())
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise TgError(408, f"{method} timed out after {timeout:g}s") from None
        finally:
            self._pending.pop(extra, None)
