import subprocess
import unittest
from unittest.mock import patch

from webex_obs.ui_banner import UIBanner


class UIBannerTests(unittest.TestCase):
    def test_startup_prompt_includes_title_consent_and_visual_sections(self):
        with patch("webex_obs.ui_banner.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["osascript"],
                returncode=0,
                stdout="gave up:true",
                stderr="",
            )

            self.assertEqual(
                UIBanner.show_startup_prompt("Cloud & AI Weekly Sync"),
                "keep_audio",
            )

        apple_script = run.call_args.args[0][2]
        self.assertIn("Cloud & AI Weekly Sync", apple_script)
        self.assertIn("RECORDING CONSENT", apple_script)
        self.assertIn("Obtain permission from all participants", apple_script)
        self.assertIn("when required by applicable law", apple_script)
        self.assertIn("KEYBOARD SHORTCUTS", apple_script)
        self.assertIn("━━━━━━━━━━━━━━━━━━━━━━━━━━", apple_script)
        self.assertIn("with icon caution", apple_script)

    def test_active_control_prompt_includes_meeting_title(self):
        with patch("webex_obs.ui_banner.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["osascript"],
                returncode=0,
                stdout="button returned:Stop & Transcribe",
                stderr="",
            )

            self.assertEqual(
                UIBanner.show_control_prompt(
                    is_recording=True,
                    meeting_title="Architecture Review",
                ),
                "stop_transcribe",
            )

        apple_script = run.call_args.args[0][2]
        self.assertIn("RECORDING CONTROLS — ACTIVE", apple_script)
        self.assertIn("MEETING\\nArchitecture Review", apple_script)
        self.assertIn("with icon note", apple_script)

    def test_meeting_title_is_escaped_for_applescript(self):
        with patch("webex_obs.ui_banner.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["osascript"],
                returncode=0,
                stdout="gave up:true",
                stderr="",
            )

            UIBanner.show_startup_prompt('Roadmap "Alpha"\\Beta')

        apple_script = run.call_args.args[0][2]
        self.assertIn('Roadmap \\"Alpha\\"\\\\Beta', apple_script)


if __name__ == "__main__":
    unittest.main()
