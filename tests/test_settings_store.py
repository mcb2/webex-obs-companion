import tempfile
import unittest
from pathlib import Path

from webex_obs.config import Config
from webex_obs.settings_store import save_settings


class SettingsStoreTests(unittest.TestCase):
    def test_updates_preserve_credentials_comments_and_spaces(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("# keep this\nOBS_WS_PASSWORD=secret\nHOTKEY_VIDEO=<cmd>+<shift>+v\n")
            save_settings({"recordings_dir": "/tmp/Meeting Recordings", "obs_ws_port": "4456"}, path)
            self.assertIn("# keep this", path.read_text())
            self.assertIn("OBS_WS_PASSWORD=secret", path.read_text())
            loaded = Config(_env_file=path)
            self.assertEqual(loaded.obs_ws_port, 4456)
            self.assertEqual(loaded.recordings_dir, Path("/tmp/Meeting Recordings"))

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
