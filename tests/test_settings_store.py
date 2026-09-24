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
                           {"obs_ws_password": "changed"}):
                with self.assertRaises(ValueError):
                    save_settings(values, path)
                self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
