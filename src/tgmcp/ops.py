"""Registry of the operations that can be called in-process or over the hub socket."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

OPS: dict[str, Callable[..., Awaitable[Any]]] = {}
# Operations that make sense before the account is authorized.
NO_AUTH: set[str] = set()


def op(fn=None, *, no_auth: bool = False):
    def register(f):
        OPS[f.__name__] = f
        if no_auth:
            NO_AUTH.add(f.__name__)
        return f

    return register(fn) if fn else register
