import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


fake_mlx_whisper = types.ModuleType("mlx_whisper")
fake_mlx_whisper.transcribe = lambda *args, **kwargs: {
    "text": "Discussed the roadmap.",
    "segments": [],
}
sys.modules.setdefault("mlx_whisper", fake_mlx_whisper)

from webex_obs.transcriber import Transcriber


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
