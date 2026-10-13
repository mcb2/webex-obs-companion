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
    content = partial[0].read_text()
    assert content.startswith("# PARTIAL TRANSCRIPT — INCOMPLETE")
    assert "1/2 segments transcribed successfully" in content
    assert "Synthetic first segment" in content
    status = "MISSING" if failure == "missing-segment" else "FAILED"
    assert f"Part 2/2 — {status}: second.mkv" in content


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


@pytest.mark.parametrize("diarized", [False, True])
def test_partial_transcript_marks_gaps_in_order_and_retry_can_complete(tmp_path, speech_modules, monkeypatch, diarized):
    files = [tmp_path / f"part-{number}.mkv" for number in range(1, 5)]
    for media in files[1:]:
        media.write_bytes(b"synthetic media")
    target = tmp_path / "transcripts" / "synthetic.txt"
    transcriber = Transcriber("test-model", target.parent, enable_diarization=diarized)
    monkeypatch.setattr("webex_obs.transcriber.shutil.which", lambda _: "/synthetic/ffmpeg")
    transcriber.diarizer = Mock()
    transcriber.diarizer.format_transcript.return_value = "Synthetic diarized speech"
    result = {"text": "Synthetic speech", "segments": [{"start": 0, "end": 1, "text": "Synthetic speech"}]}
    speech_modules["inference"].side_effect = [result, RuntimeError("synthetic failure"), {"text": "", "segments": []}]
    assert transcriber.transcribe_files([str(f) for f in files], output_path=target) is None
    content = target.read_text()
    assert content.startswith("# PARTIAL TRANSCRIPT — INCOMPLETE")
    assert "2/4 segments transcribed successfully" in content
    assert content.index("MISSING: part-1.mkv") < content.index("Synthetic") < content.index("FAILED: part-3.mkv")
    assert "Part 4/4 — No speech detected" in content
    assert all(f.read_bytes() == b"synthetic media" for f in files[1:])

    files[0].write_bytes(b"synthetic restored media")
    speech_modules["inference"].side_effect = None
    speech_modules["inference"].return_value = result
    assert transcriber.transcribe_files([str(f) for f in files], output_path=target) == target
    assert "PARTIAL TRANSCRIPT" not in target.read_text()
    assert "MISSING" not in target.read_text()
    assert "FAILED" not in target.read_text()
    assert all(f.exists() for f in files)


def test_all_missing_segments_are_reported_without_success_artifact(tmp_path, caplog, monkeypatch):
    files = [tmp_path / "missing-1.mkv", tmp_path / "missing-2.mkv"]
    transcriber = Transcriber("test-model", tmp_path / "transcripts", enable_diarization=False)
    monkeypatch.setattr("webex_obs.transcriber.shutil.which", lambda _: "/synthetic/ffmpeg")
    assert transcriber.transcribe_files([str(f) for f in files]) is None
    assert "missing-1.mkv" in caplog.text
    assert "missing-2.mkv" in caplog.text
    assert "No segments transcribed successfully" in caplog.text
    assert not list(transcriber.transcripts_dir.glob("*.txt"))


def test_silence_only_session_does_not_authorize_cleanup(tmp_path, speech_modules, monkeypatch):
    media = tmp_path / "synthetic-silence.mkv"
    media.write_bytes(b"synthetic media")
    transcriber = Transcriber("test-model", tmp_path / "transcripts", enable_diarization=False)
    monkeypatch.setattr("webex_obs.transcriber.shutil.which", lambda _: "/synthetic/ffmpeg")
    speech_modules["inference"].return_value = {"text": "", "segments": []}
    assert transcriber.transcribe_files([str(media)]) is None
    assert media.exists()
    assert not list(transcriber.transcripts_dir.glob("*.txt"))


def test_model_download_panic_preserves_media_and_explains_recovery(tmp_path, speech_modules, monkeypatch, caplog):
    media = tmp_path / "synthetic.mkv"
    media.write_bytes(b"synthetic media")
    transcriber = Transcriber("synthetic/model", tmp_path / "transcripts", enable_diarization=False)
    monkeypatch.setattr("webex_obs.transcriber.shutil.which", lambda _: "/synthetic/ffmpeg")
    speech_modules["inference"].side_effect = RuntimeError(
        'Task error: task 1 panicked with message "range end out of bounds: 100 <= 10"'
    )

    assert transcriber.transcribe_files([str(media)]) is None
    assert media.read_bytes() == b"synthetic media"
    assert not list(transcriber.transcripts_dir.glob("*.txt"))
    assert "Possible Hugging Face Xet model-download failure" in caplog.text
    assert "prefetch-model synthetic/model" in caplog.text
    assert "restart does not retry failed jobs" in caplog.text
