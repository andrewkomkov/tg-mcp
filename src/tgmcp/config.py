"""Where things live on disk, and the Telegram API credentials."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

NOT_CONFIGURED = 412
_SUN_PATH_MAX = 100


def _load_env_files() -> None:
    """Fill unset variables from .env: current directory first, then the project root."""
    for file in (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"):
        try:
            lines = file.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            key, sep, value = line.strip().partition("=")
            if sep and key and not key.startswith("#") and value.strip():
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_env_files()


def home() -> Path:
    return Path(os.environ.get("TG_MCP_HOME") or Path.home() / ".tg-mcp").expanduser()


def default_profile() -> str:
    return os.environ.get("TG_PROFILE") or "default"


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def db(self) -> Path:
        return self.root / "db"

    @property
    def files(self) -> Path:
        return self.root / "files"

    @property
    def socket(self) -> Path:
        path = self.root / "tg.sock"
        if len(os.fsencode(path)) <= _SUN_PATH_MAX:
            return path
        # AF_UNIX paths are capped at ~104 bytes; fall back to a short private dir.
        short = Path(f"/tmp/tg-mcp-{os.getuid()}")
        short.mkdir(mode=0o700, exist_ok=True)
        if short.is_symlink() or short.stat().st_uid != os.getuid():
            raise RuntimeError(f"{short} is not owned by the current user")
        os.chmod(short, 0o700)
        return short / (hashlib.sha256(os.fsencode(self.root)).hexdigest()[:16] + ".sock")

    @property
    def lock(self) -> Path:
        return self.root / "lock"

    @property
    def log(self) -> Path:
        return self.root / "tdlib.log"


def paths(profile: str) -> Paths:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", profile):
        raise ValueError(f"bad profile name: {profile!r}")
    p = Paths(home() / "profiles" / profile)
    p.root.mkdir(parents=True, exist_ok=True)
    os.chmod(home(), 0o700)
    return p


def _config_file() -> Path:
    return home() / "config.json"


def load_credentials() -> tuple[int, str] | None:
    """API id/hash from the environment, falling back to config.json."""
    api_id, api_hash = os.environ.get("TG_API_ID"), os.environ.get("TG_API_HASH")
    if not (api_id and api_hash):
        try:
            data = json.loads(_config_file().read_text())
        except (OSError, ValueError):
            return None
        api_id, api_hash = data.get("api_id"), data.get("api_hash")
    if not (api_id and api_hash):
        return None
    return int(api_id), str(api_hash)


def save_credentials(api_id: int, api_hash: str) -> None:
    f = _config_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"api_id": api_id, "api_hash": api_hash}, fh)
    os.chmod(f, 0o600)
