"""Small LaunchAgent controls shared by the menu-bar UI and service commands."""

import subprocess
from pathlib import Path


LAUNCH_AGENT_LABEL = "com.cisco.webex-obs"
LAUNCH_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def stop_launch_agent() -> bool:
    """Unload the registered job so KeepAlive cannot relaunch this process.

    Return False when running in the foreground with no loaded LaunchAgent.
    """
    running = subprocess.run(
        ["launchctl", "list", LAUNCH_AGENT_LABEL],
        capture_output=True, text=True, timeout=5,
    )
    if running.returncode != 0:
        return False
    if not LAUNCH_AGENT_PATH.exists():
        raise RuntimeError(f"LaunchAgent is loaded but its plist is missing: {LAUNCH_AGENT_PATH}")
    result = subprocess.run(
        ["launchctl", "unload", str(LAUNCH_AGENT_PATH)],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Could not stop the LaunchAgent")
    return True
