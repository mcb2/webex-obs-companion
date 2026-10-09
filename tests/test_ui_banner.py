import subprocess
import unittest
from unittest.mock import patch

from webex_obs.ui_banner import UIBanner


class UIBannerTests(unittest.TestCase):
    def test_manual_consent_ok_timeout_and_explicit_cancel(self):
        for code, stdout, stderr, expected in (
            (0, "button returned:OK", "", "ok"),
            (0, "gave up:true", "", "ok"),
            (0, "button returned:Cancel & Discard", "", "cancel"),
            (1, "", "User canceled. (-128)", "cancel"),
            (1, "", "UI unavailable", "ok"),
        ):
            with patch("webex_obs.ui_banner.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess(["osascript"], code, stdout, stderr)
                self.assertEqual(UIBanner.show_manual_consent_prompt('Meeting "Alpha"'), expected)
            script = run.call_args.args[0][2]
            self.assertIn('default button "OK"', script)
            self.assertIn('giving up after 10', script)
            self.assertIn("Obtain permission from all participants", script)
            self.assertIn('Meeting \\"Alpha\\"', script)

    def test_menu_discard_confirmation_defaults_to_no_and_requires_explicit_yes(self):
        for answer, confirmed in (("No, keep recording", False), ("Yes, stop and delete", True)):
            with patch("webex_obs.ui_banner.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess(["osascript"], 0, f"button returned:{answer}", "")
                self.assertEqual(UIBanner.confirm_discard(), confirmed)
            script = run.call_args.args[0][2]
            self.assertIn('default button "No, keep recording"', script)
            self.assertIn('cancel button "No, keep recording"', script)

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
