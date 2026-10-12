import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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


@pytest.mark.parametrize("failure", ["missing-segment", "inference-error"])
def test_partial_session_transcription_is_not_reported_as_processing_success(tmp_path, speech_modules, monkeypatch, failure):
    first, second = tmp_path / "first.mkv", tmp_path / "second.mkv"
    first.write_bytes(b"synthetic first segment")
    if failure != "missing-segment":
        second.write_bytes(b"synthetic second segment")
    transcriber = Transcriber("test-model", tmp_path / "transcripts", enable_diarization=False)
    monkeypatch.setattr("webex_obs.transcriber.shutil.which", lambda name: "/synthetic/ffmpeg")
    speech_modules["inference"].side_effect = [
        {"text": "Synthetic first segment", "segments": []},
        RuntimeError("synthetic inference failure"),
    ]
    assert transcriber.transcribe_files([str(first), str(second)]) is None
    assert first.exists()
    assert second.exists() == (failure != "missing-segment")
    partial = list((tmp_path / "transcripts").glob("*.txt"))
    assert len(partial) == 1
    assert "Synthetic first segment" in partial[0].read_text()


@pytest.mark.parametrize("stage", ["write", "replace"])
def test_interrupted_transcript_write_preserves_committed_output(tmp_path, speech_modules, monkeypatch, stage):
    media = tmp_path / "synthetic.mkv"
    media.write_bytes(b"synthetic media")
    target = tmp_path / "transcript.txt"
    target.write_text("Previously committed synthetic transcript")
    transcriber = Transcriber("test-model", tmp_path, enable_diarization=False)
    monkeypatch.setattr("webex_obs.transcriber.shutil.which", lambda _: "/synthetic/ffmpeg")
    if stage == "replace":
        monkeypatch.setattr("webex_obs.durable_state.os.replace",
                            Mock(side_effect=OSError("synthetic interruption")))
    else:
        import os
        original = os.fdopen

        def fail_write(fd, *args, **kwargs):
            stream = original(fd, *args, **kwargs)
            class Interrupted:
                def __enter__(self):
                    return self
                def write(self, text):
                    stream.write(text[:5])
                    raise OSError("synthetic interrupted write")
                def __exit__(self, *args):
                    stream.close()
            return Interrupted()

        monkeypatch.setattr("webex_obs.durable_state.os.fdopen", fail_write)
    with pytest.raises(OSError):
        transcriber.transcribe_files([str(media)], output_path=target)
    assert target.read_text() == "Previously committed synthetic transcript"
    assert media.exists()
    assert not list(tmp_path.glob(".webex-obs-*"))
