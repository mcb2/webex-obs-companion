"""Single application ownership and lifecycle health, separate from launchd load state."""

import fcntl
import json
import os
import time
from pathlib import Path

from .durable_state import write_json


class ApplicationStopping(Exception):
    """Cancel a UI request when the lifecycle is stopping."""


class RuntimeState:
    def __init__(self, directory: Path):
        self.directory = directory
        self._lock_file = None

    def acquire(self) -> None:
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._lock_file = (self.directory / "application.lock").open("a")
        os.chmod(self._lock_file.name, 0o600)
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            self._lock_file = None
            raise RuntimeError("Another companion owns this state directory") from None

    def report(self, state: str, poll_interval: float) -> None:
        write_json(self.directory / "health.json", {
            "pid": os.getpid(), "state": state,
            "expires": time.time() + max(30, poll_interval * 3),
        })

    def release(self) -> None:
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None


def application_health(directory: Path, pid: int | None) -> str:
    if not pid:
        return "not running"
    try:
        record = json.loads((directory / "health.json").read_text(encoding="utf-8"))
        if record["pid"] != pid:
            return "unhealthy (no matching application heartbeat)"
        os.kill(pid, 0)
        if record["state"] != "running":
            return f"unhealthy ({record['state']})"
        if record["expires"] < time.time():
            return "unhealthy (stale lifecycle heartbeat)"
        return "healthy (lifecycle responding)"
    except (OSError, ValueError, KeyError, TypeError):
        return "unhealthy (application heartbeat unavailable)"
