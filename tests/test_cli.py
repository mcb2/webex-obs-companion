import io
import plistlib
import stat
import tomllib
import warnings
from pathlib import Path
from unittest.mock import Mock

import pytest
from rich.console import Console
from getpass import GetPassWarning

from webex_obs import __version__, cli
from webex_obs.config import Config


def test_secret_prompt_aborts_before_insecure_input_fallback(monkeypatch, capsys):
    fallback = Mock()

    def insecure_prompt(*args, **kwargs):
        warnings.warn("Password input may be echoed.", GetPassWarning)
        return fallback()

    monkeypatch.setattr(cli.Prompt, "ask", insecure_prompt)
    with pytest.raises(cli.typer.Exit) as exc:
        cli._ask_secret("Secret", default="synthetic-existing-secret")
    assert exc.value.exit_code == 1
    fallback.assert_not_called()
    output = capsys.readouterr().out
    assert "supports hidden input" in output
    assert "synthetic-existing-secret" not in output


@pytest.mark.parametrize("existing,replace_secrets,delivery,diarization", [
    (False, True, True, True),
    (True, False, True, True),
    (True, True, True, True),
    (True, False, False, False),
])
def test_setup_hides_secrets_and_saves_private_file(
    tmp_path, monkeypatch, existing, replace_secrets, delivery, diarization
):
    env_file = tmp_path / ".env"
    old = {"webex_access_token": "synthetic-old-webex", "obs_ws_password": "synthetic-old-obs",
           "hf_token": "synthetic-old-hf"}
    new = {"webex_access_token": "synthetic-new-webex", "obs_ws_password": "synthetic-new-obs",
           "hf_token": "synthetic-new-hf"}
    if existing:
        env_file.write_text(
            f"WEBEX_ACCESS_TOKEN={old['webex_access_token']}\n"
            f"OBS_WS_PASSWORD={old['obs_ws_password']}\nHF_TOKEN={old['hf_token']}\n"
        )
        env_file.chmod(0o666)
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "src/webex_obs/cli.py"))
    monkeypatch.setattr(cli, "_check_ffmpeg", lambda: True)
    monkeypatch.setattr("webex_obs.config.Settings", lambda: Config(_env_file=env_file))
    output = io.StringIO()
    console = Console(file=output, width=180)
    monkeypatch.setattr(cli, "console", console)
    monkeypatch.setattr("rich.prompt.get_console", lambda: console)
    prompts = []

    def get_input(cls, console, prompt, password, stream=None):
        text = str(prompt)
        prompts.append((text, password))
        console.print(prompt)
        if "Automatically deliver" in text:
            answer = "y" if delivery else "n"
        elif "Enable Neural" in text:
            answer = "y" if diarization else "n"
        elif "Keep the existing Hugging Face" in text:
            answer = "n" if replace_secrets else "y"
        elif "Webex Bot Access Token" in text:
            answer = new["webex_access_token"] if replace_secrets else ""
        elif "OBS WebSocket password" in text:
            answer = new["obs_ws_password"] if replace_secrets else ""
        elif "Enter your Hugging Face read token" in text:
            answer = new["hf_token"]
        else:
            answer = ""
        # Simulate terminal echo only for non-password input.
        if not password:
            console.print(answer)
        return answer

    monkeypatch.setattr(cli.Prompt, "get_input", classmethod(get_input))
    monkeypatch.setattr(cli.Confirm, "get_input", classmethod(get_input))
    original_replace = cli.os.replace

    def replace(source, destination):
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        assert Path(destination) == env_file
        original_replace(source, destination)

    monkeypatch.setattr(cli.os, "replace", replace)
    cli.setup()

    loaded = Config(_env_file=env_file)
    for field in old:
        changed = replace_secrets and (delivery if field == "webex_access_token" else
                                      diarization if field == "hf_token" else True)
        assert getattr(loaded, field) == (new[field] if changed else old[field])
    for secret in [*old.values(), *new.values()]:
        assert secret not in output.getvalue()
    for text, password in prompts:
        if any(label in text for label in (
            "Webex Bot Access Token", "OBS WebSocket password", "Enter your Hugging Face read token"
        )):
            assert password is True
    assert loaded.webex_delivery_enabled is delivery
    assert loaded.enable_diarization is diarization
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".webex-obs-*"))


def test_setup_failed_replacement_preserves_existing_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    original = "WEBEX_ACCESS_TOKEN=synthetic-retained\nENABLE_DIARIZATION=false\n"
    env_file.write_text(original)
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "src/webex_obs/cli.py"))
    monkeypatch.setattr(cli, "_check_ffmpeg", lambda: True)
    monkeypatch.setattr("webex_obs.config.Settings", lambda: Config(_env_file=env_file))
    monkeypatch.setattr(cli.Prompt, "ask", lambda *args, **kwargs: kwargs.get("default", ""))
    monkeypatch.setattr(cli.Confirm, "ask", lambda *args, **kwargs: kwargs["default"])
    monkeypatch.setattr(cli.os, "replace", Mock(side_effect=OSError("synthetic replacement failure")))
    with pytest.raises(OSError, match="synthetic replacement failure"):
        cli.setup()
    assert env_file.read_text() == original
    assert not list(tmp_path.glob(".webex-obs-*"))


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
