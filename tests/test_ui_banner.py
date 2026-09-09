import subprocess
import unittest
from unittest.mock import patch

from webex_obs.ui_banner import UIBanner


class UIBannerTests(unittest.TestCase):
    def test_startup_prompt_includes_recording_consent_notice(self):
        with patch("webex_obs.ui_banner.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["osascript"], returncode=0, stdout="gave up:true", stderr=""
            )

            self.assertEqual(UIBanner.show_startup_prompt(), "keep_audio")

        apple_script = run.call_args.args[0][2]
        self.assertIn("Recording Consent Notice", apple_script)
        self.assertIn("Obtain permission from all participants", apple_script)
        self.assertIn("when required by applicable law", apple_script)


if __name__ == "__main__":
    unittest.main()
