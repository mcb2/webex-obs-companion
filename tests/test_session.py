import datetime
import tempfile
import unittest
from pathlib import Path

from webex_obs.session import (
    DEFAULT_MEETING_TITLE,
    RecordingSession,
    sanitize_filename_title,
)


class SessionNamingTests(unittest.TestCase):
    def test_sanitizes_problematic_filename_characters(self):
        self.assertEqual(
            sanitize_filename_title('Q3: Cloud/AI <Roadmap> | Mark & Team 🔒'),
            "Q3 - Cloud - AI - Roadmap - Mark & Team",
        )

    def test_preserves_unicode_letters_and_limits_utf8_length(self):
        title = sanitize_filename_title("Résumé 東京 " * 30)
        self.assertLessEqual(len(title.encode("utf-8")), 100)
        self.assertIn("Résumé", title)
        self.assertIn("東京", title)

    def test_empty_title_uses_fallback(self):
        self.assertEqual(sanitize_filename_title("\n\t"), DEFAULT_MEETING_TITLE)

    def test_display_title_whitespace_does_not_join_words(self):
        session = RecordingSession.create("Weekly\nSync")
        self.assertEqual(session.display_title, "Weekly Sync")

    def test_session_stem_uses_call_start_time(self):
        session = RecordingSession.create(
            "Weekly Sync",
            started_at=datetime.datetime(2026, 9, 9, 12, 3, 26),
        )
        self.assertEqual(
            session.filename_stem,
            "2026-09-09_12-03-26 - Weekly Sync",
        )

    def test_late_title_replaces_fallback_without_changing_timestamp(self):
        session = RecordingSession.create(
            None,
            started_at=datetime.datetime(2026, 9, 9, 12, 3, 26),
        )
        session.use_title_if_missing("Architecture Review")
        self.assertEqual(session.display_title, "Architecture Review")
        self.assertEqual(
            session.filename_stem,
            "2026-09-09_12-03-26 - Architecture Review",
        )

    def test_multiple_recording_segments_receive_part_numbers(self):
        session = RecordingSession.create(
            "Weekly Sync",
            started_at=datetime.datetime(2026, 9, 9, 12, 3, 26),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "obs-one.mkv"
            second = Path(temp_dir) / "obs-two.mp4"
            first.touch()
            second.touch()

            renamed = session.rename_recordings([str(first), str(second)])

            self.assertEqual(
                [Path(path).name for path in renamed],
                [
                    "2026-09-09_12-03-26 - Weekly Sync - part-01.mkv",
                    "2026-09-09_12-03-26 - Weekly Sync - part-02.mp4",
                ],
            )
            self.assertTrue(all(Path(path).exists() for path in renamed))

    def test_existing_file_is_not_overwritten(self):
        session = RecordingSession.create(
            "Weekly Sync",
            started_at=datetime.datetime(2026, 9, 9, 12, 3, 26),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "obs.mkv"
            source.touch()
            existing = Path(temp_dir) / f"{session.filename_stem}.mkv"
            existing.write_text("keep me")

            renamed = session.rename_recordings([str(source)])

            self.assertEqual(
                Path(renamed[0]).name,
                "2026-09-09_12-03-26 - Weekly Sync (2).mkv",
            )
            self.assertEqual(existing.read_text(), "keep me")


if __name__ == "__main__":
    unittest.main()
