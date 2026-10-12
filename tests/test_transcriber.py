import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def speech_components(speech_modules, monkeypatch):
    monkeypatch.setitem(globals(), "Transcriber", speech_modules["Transcriber"])


class TranscriberNamingTests(unittest.TestCase):
    def test_uses_session_stem_and_title_for_raw_transcript(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "recording.mkv"
            media.touch()
            transcripts = root / "transcripts"
            transcriber = Transcriber(
                model_name="test-model",
                transcripts_dir=transcripts,
                enable_diarization=False,
            )

            with patch("webex_obs.transcriber.shutil.which", return_value="/usr/bin/ffmpeg"):
                result = transcriber.transcribe_files(
                    [str(media)],
                    meeting_title="Architecture Review",
                    output_stem="2026-09-09_12-03-26 - Architecture Review",
                )

            self.assertIsNotNone(result)
            self.assertEqual(
                result.name,
                "2026-09-09_12-03-26 - Architecture Review.txt",
            )
            self.assertIn(
                "# Meeting Transcript: Architecture Review",
                result.read_text(),
            )


if __name__ == "__main__":
    unittest.main()


@pytest.mark.parametrize("failure", ["missing-ffmpeg", "inference-error"])
def test_transcription_failure_preserves_media_without_empty_transcript(tmp_path, speech_modules, monkeypatch, failure):
    media = tmp_path / "synthetic.mkv"
    media.write_bytes(b"synthetic media")
    output = tmp_path / "transcripts"
    transcriber = Transcriber("test-model", output, enable_diarization=False)
    monkeypatch.setattr("webex_obs.transcriber.shutil.which",
                        lambda name: None if failure == "missing-ffmpeg" else "/synthetic/ffmpeg")
    inference = speech_modules["inference"]
    inference.side_effect = RuntimeError("synthetic inference failure")
    assert transcriber.transcribe_files([str(media)]) is None
    assert media.read_bytes() == b"synthetic media"
    assert not list(output.glob("*.txt"))
    assert inference.call_count == (failure == "inference-error")
