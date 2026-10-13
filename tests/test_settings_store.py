import tempfile
import stat
import unittest
from pathlib import Path

import pytest

from webex_obs.config import Config
from webex_obs.settings_store import save_settings


@pytest.mark.parametrize("mode", [None, 0o666, 0o755, 0o400])
def test_credentials_are_private_after_creation_or_replacement(tmp_path, monkeypatch, mode):
    path = tmp_path / ".env"
    if mode is not None:
        path.write_text("# retain\nWEBEX_ACCESS_TOKEN=synthetic-retained\n")
        path.chmod(mode)
    from webex_obs import settings_store
    original_replace = settings_store.os.replace

    def replace(source, destination):
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        original_replace(source, destination)

    monkeypatch.setattr(settings_store.os, "replace", replace)
    save_settings({"obs_ws_password": "synthetic-new"}, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert Config(_env_file=path).obs_ws_password == "synthetic-new"
    if mode is not None:
        assert "WEBEX_ACCESS_TOKEN=synthetic-retained" in path.read_text()
        assert "# retain" in path.read_text()


class SettingsStoreTests(unittest.TestCase):
    def test_new_source_setting_overrides_legacy_menu_and_audio_round_trips(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("HOTKEY_MENU=<ctrl>+m\n")
            self.assertEqual(Config(_env_file=path).hotkey_video_source, "<ctrl>+m")
            save_settings({"hotkey_audio": "<ctrl>+a", "hotkey_video_source": "<ctrl>+r"}, path)
            updated = Config(_env_file=path)
            self.assertEqual(updated.hotkey_audio, "<ctrl>+a")
            self.assertEqual(updated.hotkey_video_source, "<ctrl>+r")

    def test_updates_preserve_credentials_comments_and_spaces(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("# keep this\nOBS_WS_PASSWORD=secret\nHOTKEY_VIDEO=<cmd>+<shift>+v\n")
            save_settings({"recordings_dir": "/tmp/Meeting Recordings", "obs_ws_port": "4456"}, path)
            self.assertIn("# keep this", path.read_text())
            self.assertIn("OBS_WS_PASSWORD=secret", path.read_text())
            loaded = Config(_env_file=path)
            self.assertEqual(loaded.obs_ws_port, 4456)
            self.assertEqual(loaded.recordings_dir, Path("/tmp/Meeting Recordings").resolve())

    def test_invalid_input_does_not_touch_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("OBS_WS_PORT=4455\n")
            original = path.read_bytes()
            for values in ({"obs_ws_port": "99999"},
                           {"hotkey_video": "<cmd>+<shift>+r"},
                           {"poll_interval": "0"},
                           {"webex_access_token": "changed", "obs_ws_port": "99999"}):
                with self.assertRaises(ValueError):
                    save_settings(values, path)
                self.assertEqual(path.read_bytes(), original)

    def test_new_obs_setting_overrides_legacy_env_value(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("RELAUNCH_OBS_PER_CALL=false\n")
            self.assertFalse(Config(_env_file=path).exit_obs_on_recording_stop)
            save_settings({"exit_obs_on_recording_stop": True}, path)
            self.assertTrue(Config(_env_file=path).exit_obs_on_recording_stop)

    def test_every_config_field_is_editable_and_secrets_round_trip(self):
        from webex_obs.settings_store import EDITABLE
        # LOG_DIR is a legacy field; LaunchAgent owns its log file paths.
        self.assertEqual(set(EDITABLE), set(Config.model_fields) - {"log_dir"})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("# preserve\nOBS_WS_PORT=4455\n")
            updated = save_settings({"webex_access_token": "a token with spaces",
                                     "webex_delivery_enabled": False,
                                     "hf_token": "hf_example", "enable_diarization": True,
                                     "whisper_model": "another-model", "poll_interval": "1.5"}, path)
            self.assertEqual(updated.webex_access_token, "a token with spaces")
            self.assertEqual(Config(_env_file=path).hf_token, "hf_example")
            self.assertFalse(Config(_env_file=path).webex_delivery_enabled)
            self.assertEqual(Config(_env_file=path).poll_interval, 1.5)
            self.assertIn("# preserve", path.read_text())


if __name__ == "__main__":
    unittest.main()


@pytest.mark.parametrize("value", [
    "Meeting Recordings #1", "l'été / 雪", 'double "quotes"',
    r"back\slash\'quoted", "literal ${HOME} and $PATH", "line\nbreak",
])
def test_literal_values_round_trip_through_writer_and_config(tmp_path, value):
    path = tmp_path / "configuration with spaces" / ".env"
    updated = save_settings({"obs_ws_password": value, "recordings_dir": str(tmp_path / value)}, path)
    loaded = Config(_env_file=path)
    assert loaded.obs_ws_password == value
    assert loaded.recordings_dir == updated.recordings_dir


@pytest.mark.parametrize("assignment", [
    "OBS_WS_PORT=4455 # keep inline\n", "export OBS_WS_PORT = '4455'  # keep inline\n",
    'OBS_WS_PORT="4455" # keep inline\n',
])
def test_preserves_inline_comments_on_edited_fields(tmp_path, assignment):
    path = tmp_path / ".env"
    path.write_text("# heading\n" + assignment + "UNRELATED=literal\n")
    save_settings({"obs_ws_port": "4456"}, path)
    assert "# keep inline\n" in path.read_text()
    assert "# heading\n" in path.read_text()
    assert "UNRELATED=literal\n" in path.read_text()
    assert Config(_env_file=path).obs_ws_port == 4456


@pytest.mark.parametrize("exists", [False, True])
def test_apply_failure_restores_exact_original_file(tmp_path, exists):
    path = tmp_path / ".env"
    original = b"# retain\r\nOBS_PORT=4455 # legacy\r\nUNRELATED=x\r\n"
    if exists:
        path.write_bytes(original)
    def fail(config):
        assert Config(_env_file=path).obs_ws_port == 4456
        raise RuntimeError("synthetic apply failure")
    with pytest.raises(RuntimeError, match="synthetic apply failure"):
        save_settings({"obs_ws_port": "4456"}, path, apply=fail)
    assert path.exists() is exists
    if exists:
        assert path.read_bytes() == original
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".webex-obs-*"))


def test_failed_atomic_save_never_applies_runtime(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from webex_obs import settings_store
    path = tmp_path / ".env"
    path.write_text("OBS_PORT=4455\n")
    apply = Mock()
    monkeypatch.setattr(settings_store.os, "replace", Mock(side_effect=OSError("synthetic disk failure")))
    with pytest.raises(OSError):
        save_settings({"obs_ws_port": "4456"}, path, apply=apply)
    apply.assert_not_called()
    assert path.read_text() == "OBS_PORT=4455\n"
    assert not list(tmp_path.glob(".webex-obs-*"))


def test_persistence_rollback_failure_is_reported_without_secret_values(tmp_path, monkeypatch):
    from webex_obs import settings_store
    path = tmp_path / ".env"
    path.write_text("OBS_PORT=4455\n")
    original_replace = settings_store.os.replace
    calls = 0
    def replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic rollback disk failure")
        original_replace(source, destination)
    monkeypatch.setattr(settings_store.os, "replace", replace)
    def fail(config):
        raise RuntimeError("synthetic secret")
    with pytest.raises(settings_store.SettingsRollbackError, match="restore the previous") as exc:
        save_settings({"obs_ws_password": "synthetic secret"}, path, apply=fail)
    assert "synthetic secret" not in str(exc.value)
    assert not list(tmp_path.glob(".webex-obs-*"))


def test_rollback_file_is_private_before_original_credentials_are_written(tmp_path, monkeypatch):
    from webex_obs import settings_store
    path = tmp_path / ".env"
    path.write_text("OBS_WS_PASSWORD=synthetic-retained\n")
    original_fdopen = settings_store.os.fdopen
    writes = []
    def fdopen(fd, *args, **kwargs):
        writes.append(stat.S_IMODE(settings_store.os.fstat(fd).st_mode))
        assert writes[-1] == 0o600
        return original_fdopen(fd, *args, **kwargs)
    monkeypatch.setattr(settings_store.os, "fdopen", fdopen)
    def fail(config):
        raise RuntimeError("synthetic failure")
    with pytest.raises(RuntimeError):
        save_settings({"obs_ws_port": "4456"}, path, apply=fail)
    assert writes == [0o600]
    assert Config(_env_file=path).obs_ws_password == "synthetic-retained"
