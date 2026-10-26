from datetime import datetime, timedelta, timezone
import json
import stat
from unittest.mock import Mock

import pytest

from webex_obs.core_audio_monitor import AudioActivity
from webex_obs.detection_diagnostics import DetectionDiagnostics, resolve_event_time


@pytest.mark.parametrize("now,text,expected", [
    ("2026-10-26T12:30:00+00:00", "12:29:45", "2026-10-26T12:29:45+00:00"),
    ("2026-10-26T00:10:00+00:00", "23:45", "2026-10-25T23:45:00+00:00"),
    ("2026-10-26T12:30:00+00:00", "11:30:00", "2026-10-26T11:30:00+00:00"),
])
def test_past_clock_time_including_midnight(now, text, expected):
    assert resolve_event_time(text, datetime.fromisoformat(now)).isoformat() == expected


@pytest.mark.parametrize("text", ["11:29:59", "12:31", "25:00", "bad", "12:20:99"])
def test_invalid_future_or_old_times_rejected(text):
    with pytest.raises(ValueError):
        resolve_event_time(text, datetime(2026, 10, 26, 12, 30, tzinfo=timezone.utc))


def test_missed_response_is_not_an_answer_and_redetection_is_deduplicated(tmp_path):
    diagnostics = DetectionDiagnostics(tmp_path)
    diagnostics.record = Mock()
    diagnostics.detected("webex")
    event = diagnostics.prompts.get_nowait()
    diagnostics.detected("webex")
    assert diagnostics.prompts.empty()
    diagnostics.answer(event, None)
    assert diagnostics.record.call_count == 1
    diagnostics.answer(event, "not_sure")
    assert diagnostics.record.call_args.kwargs["response"] == "not_sure"
    diagnostics.ended("webex")
    diagnostics.detected("webex")
    assert not diagnostics.prompts.empty()


def test_private_log_sanitizes_sensor_values_and_preserves_prior_evidence(tmp_path):
    diagnostics = DetectionDiagnostics(tmp_path)
    diagnostics.start()
    diagnostics.sensor("webex", "window", "Synthetic private meeting", True, 0.2)
    diagnostics.sensor("webex", "audio", AudioActivity(available=True, input_pids=(42,)), True, 0.1)
    diagnostics.sensor("webex", "sockets", [("Synthetic private process", 5004, True)], True, 0.1)
    diagnostics.sensor("webex", "window", None, False, 2.0)
    occurred = datetime.now().astimezone() - timedelta(minutes=50)
    diagnostics.marker("start", occurred)
    diagnostics.marker("end", occurred + timedelta(minutes=10))
    diagnostics.close()
    text = diagnostics.path.read_text()
    records = [json.loads(line) for line in text.splitlines()]
    assert "Synthetic private" not in text
    assert records[1]["title_token"]
    assert records[2]["input_pids"] == [42]
    assert records[3]["ports"] == [5004]
    assert records[4]["ok"] is False
    assert records[5]["occurred_at"] == occurred.timestamp()
    assert records[6]["event"] == "end"
    assert stat.S_IMODE(diagnostics.path.stat().st_mode) == 0o600
    assert not diagnostics._worker.is_alive()


def test_manual_marker_does_not_infer_other_events(tmp_path):
    diagnostics = DetectionDiagnostics(tmp_path)
    diagnostics.record = Mock()
    diagnostics.marker("start", datetime.now().astimezone() - timedelta(minutes=45))
    diagnostics.record.assert_called_once()
    assert diagnostics.prompts.empty()
    with pytest.raises(ValueError):
        diagnostics.marker("end", datetime.now().astimezone() - timedelta(minutes=61))
