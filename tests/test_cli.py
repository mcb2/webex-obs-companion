import plistlib
import tomllib
from pathlib import Path
from unittest.mock import Mock

from webex_obs import __version__, cli


def test_install_serializes_special_characters_without_operating_service(tmp_path, monkeypatch):
    home = tmp_path / "Home & <Test>"
    agents = home / "Library" / "LaunchAgents"
    plist = agents / cli.PLIST_NAME
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(cli, "LAUNCH_AGENTS_DIR", agents)
    monkeypatch.setattr(cli, "PLIST_PATH", plist)
    monkeypatch.setattr(cli.sys, "executable", str(home / "Python & <3>"))
    monkeypatch.setattr(cli, "_check_ffmpeg", lambda: True)
    monkeypatch.setenv("PATH", str(home / "bin"))
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(cli.subprocess, "run", run)

    cli.install_service()

    with plist.open("rb") as stream:
        data = plistlib.load(stream)
    assert data["Label"] == "com.cisco.webex-obs"
    assert data["WorkingDirectory"] == str(Path(cli.__file__).resolve().parents[2])
    assert data["ProgramArguments"] == [str(home / "Python & <3>"), "-u", "-m", "webex_obs.daemon"]
    assert data["EnvironmentVariables"] == {
        "PYTHONUNBUFFERED": "1", "HF_HUB_ENABLE_HF_TRANSFER": "0",
        "PATH": f"/opt/homebrew/bin:/usr/local/bin:{home / 'bin'}",
    }
    assert data["RunAtLoad"] is True and data["KeepAlive"] is True
    assert data["StandardOutPath"] == str(home / "Library/Logs/WebexOBS/stdout.log")
    assert data["StandardErrorPath"] == str(home / "Library/Logs/WebexOBS/stderr.log")
    assert [call.args[0] for call in run.call_args_list] == [
        ["launchctl", "unload", str(plist)], ["launchctl", "load", str(plist)],
    ]


def test_runtime_and_locked_project_versions_agree():
    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    lock = tomllib.loads((root / "uv.lock").read_text())
    locked = next(package for package in lock["package"] if package["name"] == metadata["project"]["name"])
    assert __version__ == metadata["project"]["version"] == locked["version"]
