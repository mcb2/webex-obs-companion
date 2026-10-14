"""Recording destination checks use synthetic RPCs and temporary folders only."""
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from webex_obs.config import Config
from webex_obs.obs_controller import OBSController
from webex_obs.recording_backend import OBSRecordingBackend


@pytest.mark.parametrize("mode", ["Webex-Audio", "Webex-Video"])
def test_directory_created_applied_and_verified_before_start(tmp_path, obs_v5_client, mode):
    directory = tmp_path / "new" / "Recordings"
    state = {"directory": str(tmp_path / "old")}
    calls = []

    def rpc(name, data):
        calls.append(name)
        if name == "GetRecordDirectory":
            return True, {"recordDirectory": state["directory"]}
        if name == "SetRecordDirectory":
            assert directory.is_dir()
            state["directory"] = data["recordDirectory"]
        if name == "StartRecord":
            assert state["directory"] == str(directory)
        return True, {}

    controller = OBSController(recording_directory=directory)
    controller.ws = obs_v5_client(rpc)
    with patch.object(controller, "_refresh_recording_status", side_effect=[False, True]), \
         patch.object(controller, "switch_scene", return_value=True), \
         patch.object(controller, "bind_webex_video_window", return_value=True), \
         patch.object(controller, "_restart_audio_capture"):
        assert controller.start_recording(mode)
    assert calls == ["GetRecordDirectory", "SetRecordDirectory", "GetRecordDirectory", "StartRecord"]
    assert list(directory.iterdir()) == []  # No write-check file left behind.


@pytest.mark.parametrize("failure", ["get-rejected", "set-rejected", "mismatch", "missing", "not-directory", "not-writable"])
def test_directory_failure_blocks_start(tmp_path, obs_v5_client, caplog, failure):
    directory = tmp_path / "recordings"
    if failure == "not-directory":
        directory.write_text("synthetic")
    calls = []

    def rpc(name, data):
        calls.append(name)
        if name == "GetRecordDirectory":
            if failure == "get-rejected":
                return False, None
            return True, {} if failure == "missing" else {"recordDirectory": str(tmp_path / "old")}
        if name == "SetRecordDirectory":
            return failure != "set-rejected", None
        raise AssertionError(f"Unexpected RPC: {name}")

    controller = OBSController(recording_directory=directory)
    controller.ws = obs_v5_client(rpc)
    with patch.object(controller, "_refresh_recording_status", return_value=False), \
         patch.object(controller, "switch_scene", return_value=True), \
         patch.object(controller, "_restart_audio_capture"), \
         patch("webex_obs.obs_controller.tempfile.TemporaryFile") as probe:
        if failure == "not-writable":
            probe.side_effect = PermissionError("synthetic")
        assert not controller.start_recording()
    assert "StartRecord" not in calls
    assert "recording start blocked" in caplog.text
    assert str(directory) not in caplog.text


@pytest.mark.parametrize("active", [True, None])
def test_active_or_unknown_output_never_changes_directory(tmp_path, active):
    controller = OBSController(recording_directory=tmp_path / "new")
    controller.ws = Mock()
    with patch.object(controller, "_refresh_recording_status", return_value=active):
        assert not controller.start_recording()
    controller.ws.call.assert_not_called()
    assert not controller.recording_directory.exists()


def test_matching_directory_needs_no_set_request(tmp_path, obs_v5_client):
    calls = []

    def rpc(name, data):
        calls.append(name)
        return True, {"recordDirectory": str(tmp_path)}

    controller = OBSController(recording_directory=tmp_path)
    controller.ws = obs_v5_client(rpc)
    assert controller._apply_recording_directory()
    assert calls == ["GetRecordDirectory", "GetRecordDirectory"]


def test_live_change_stays_pending_through_recovery_and_failed_stop(tmp_path):
    original, updated = tmp_path / "original", tmp_path / "updated"
    controller = Mock(recording_directory=original, is_recording=True,
                      address="localhost", port=4455, password="", stop_verified=False)
    backend = OBSRecordingBackend(controller)
    backend.configure(Config(_env_file=None, recordings_dir=updated))
    backend.ensure_recording()
    backend.start_recording()
    backend.stop_recording()
    assert backend.recording_directory == original
    assert backend._pending_recordings_dir == updated

    controller.is_recording = False
    controller.stop_verified = True
    controller._recording_active_status.return_value = False
    backend.stop_recording()
    assert backend.recording_directory == original  # Available to the completed job.
    backend.start_recording()
    assert backend.recording_directory == updated
    assert backend._pending_recordings_dir is None


def test_recovery_reapplies_original_directory_after_obs_change(tmp_path, obs_v5_client):
    original, updated = tmp_path / "original", tmp_path / "updated"
    state = {"directory": str(tmp_path / "external-change")}
    sets = []

    def rpc(name, data):
        if name == "GetRecordDirectory":
            return True, {"recordDirectory": state["directory"]}
        if name == "SetRecordDirectory":
            state["directory"] = data["recordDirectory"]
            sets.append(state["directory"])
        return True, {}

    controller = OBSController(recording_directory=original)
    controller.is_recording = True
    controller.ws = obs_v5_client(rpc)
    backend = OBSRecordingBackend(controller)
    backend.configure(Config(_env_file=None, recordings_dir=updated))
    with patch.object(controller, "_refresh_recording_status", side_effect=[False, False, True]), \
         patch.object(controller, "switch_scene", return_value=True), \
         patch.object(controller, "_restart_audio_capture"):
        assert backend.ensure_recording()
    assert sets == [str(original)]
    assert backend.recording_directory == original
    assert not updated.exists()
