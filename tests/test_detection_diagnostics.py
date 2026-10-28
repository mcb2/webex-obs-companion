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


def test_private_reports_sanitize_values_and_no_continuous_log_is_written(tmp_path):
    diagnostics = DetectionDiagnostics(tmp_path)
    diagnostics.start()
    diagnostics.sensor("webex", "window", "Synthetic private meeting", True, 0.2)
    diagnostics.sensor("webex", "audio", AudioActivity(available=True, input_pids=(42,)), True, 0.1)
    diagnostics.sensor("webex", "sockets", [("Synthetic private process", 5004, True)], True, 0.1)
    diagnostics.sensor("webex", "window", None, False, 2.0)
    diagnostics.detected("webex")
    diagnostics.answer(diagnostics.prompts.get_nowait(), "no")
    diagnostics.close()
    files = list(diagnostics.path.glob("*.json"))
    assert len(files) == 1
    text = files[0].read_text()
    report = json.loads(text)
    assert "Synthetic private" not in text
    assert report["reason"] == "false_start"
    sensors = [row for row in report["evidence"] if row["kind"] == "sensor"]
    assert sensors[0]["title_token"]
    assert sensors[1]["input_pids"] == [42]
    assert sensors[2]["ports"] == [5004]
    assert sensors[3]["ok"] is False
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert not (tmp_path / "detection-diagnostics.jsonl").exists()
    assert not diagnostics._worker.is_alive()


def test_manual_marker_does_not_infer_other_events(tmp_path):
    diagnostics = DetectionDiagnostics(tmp_path)
    diagnostics.record = Mock()
    diagnostics.marker("start", datetime.now().astimezone() - timedelta(minutes=45))
    diagnostics.record.assert_called_once()
    assert diagnostics.prompts.empty()
    with pytest.raises(ValueError):
        diagnostics.marker("end", datetime.now().astimezone() - timedelta(minutes=61))


def accept(diagnostics, kind, at, **fields):
    diagnostics._accept(dict(kind=kind, observed_at=at, monotonic=at, run_id="synthetic", **fields))


def reports(diagnostics):
    return [json.loads(path.read_text()) for path in diagnostics.path.glob("*.json")]


def test_agreement_timeout_and_missing_manual_markers_do_not_save_reports(tmp_path):
    diagnostics = DetectionDiagnostics(tmp_path)
    accept(diagnostics, "enabled", 0)
    accept(diagnostics, "detected_start", 100, detected_at=100, platform="webex")
    accept(diagnostics, "confirmation", 101, detected_at=100, response="yes")
    accept(diagnostics, "detected_end", 200, platform="webex")
    accept(diagnostics, "detected_start", 250, detected_at=250, platform="webex")
    diagnostics._flush(1000, closing=True)
    assert reports(diagnostics) == []


@pytest.mark.parametrize("kind", ["start", "end"])
def test_manual_just_now_waits_for_detector_and_matching_marker_saves_nothing(tmp_path, kind):
    diagnostics = DetectionDiagnostics(tmp_path)
    accept(diagnostics, "enabled", 0)
    accept(diagnostics, "manual_marker", 100, occurred_at=100, event=kind)
    diagnostics._flush(159)
    assert reports(diagnostics) == []
    accept(diagnostics, "detected_" + kind, 110, platform="webex")
    diagnostics._flush(160)
    assert diagnostics._pending == []
    assert reports(diagnostics) == []


@pytest.mark.parametrize("kind", ["start", "end"])
def test_late_missed_marker_saves_only_event_context(tmp_path, kind):
    diagnostics = DetectionDiagnostics(tmp_path)
    accept(diagnostics, "enabled", 0)
    accept(diagnostics, "sensor", 50, platform="webex", sensor="window", present=False)
    accept(diagnostics, "sensor", 99, platform="webex", sensor="window", present=True)
    accept(diagnostics, "sensor", 1000, platform="webex", sensor="window", present=False)
    accept(diagnostics, "manual_marker", 3100, occurred_at=100, event=kind)
    diagnostics._flush(3100)
    report, = reports(diagnostics)
    assert report["reason"] == "missed_" + kind
    assert report["annotation"]["observed_at"] == 3100
    assert report["event_at"] == 100
    assert len(report["evidence"]) == 3
    assert all(row["observed_at"] <= 220 for row in report["evidence"])
    assert report["following_context_complete"]


def test_report_adds_following_context_and_compacts_repeated_states(tmp_path):
    diagnostics = DetectionDiagnostics(tmp_path)
    accept(diagnostics, "enabled", 0)
    accept(diagnostics, "confirmation", 200, detected_at=200, response="not_sure")
    diagnostics._flush(200)
    assert reports(diagnostics)[0]["reason"] == "uncertain_start"
    assert not reports(diagnostics)[0]["following_context_complete"]
    for at in (201, 202, 205):
        accept(diagnostics, "sensor", at, platform="webex", sensor="window", present=True, elapsed_seconds=0.2)
    accept(diagnostics, "sensor", 210, platform="webex", sensor="window", present=False, elapsed_seconds=2.0)
    diagnostics._flush(320)
    report, = reports(diagnostics)
    assert report["following_context_complete"]
    assert report["prior_context_complete"]
    sensors = [row for row in report["evidence"] if row["kind"] == "sensor"]
    assert len(sensors) == 2
    assert sensors[0]["observations"] == 3
    assert sensors[0]["last_observed_at"] == 205
    assert sensors[1]["present"] is False
    assert diagnostics._cases == []


def test_history_keeps_hour_late_context_and_enforces_byte_limit(tmp_path):
    diagnostics = DetectionDiagnostics(tmp_path)
    accept(diagnostics, "sensor", 0, present=True)
    accept(diagnostics, "sensor", 3700, present=False)
    assert len(diagnostics._history) == 2
    diagnostics.HISTORY_BYTES = 180
    accept(diagnostics, "sensor", 3701, present=False)
    assert diagnostics._history_bytes <= 180
    accept(diagnostics, "confirmation", 3702, detected_at=3700, response="no")
    diagnostics._flush(3820)
    report, = reports(diagnostics)
    assert not report["prior_context_complete"]


def test_shutdown_before_match_window_is_uncertain_not_a_confirmed_miss(tmp_path):
    diagnostics = DetectionDiagnostics(tmp_path)
    accept(diagnostics, "manual_marker", 100, occurred_at=100, event="start")
    diagnostics._flush(110, closing=True)
    report, = reports(diagnostics)
    assert report["reason"] == "unverified_start"
    assert not report["following_context_complete"]


def test_marker_from_before_diagnostic_start_is_unverified_not_a_missed_detection(tmp_path):
    diagnostics = DetectionDiagnostics(tmp_path)
    accept(diagnostics, "enabled", 1000)
    accept(diagnostics, "manual_marker", 1100, occurred_at=500, event="end")
    diagnostics._flush(1100)
    report, = reports(diagnostics)
    assert report["reason"] == "unverified_end"
    assert not report["matching_window_complete"]
    assert not report["prior_context_complete"]
