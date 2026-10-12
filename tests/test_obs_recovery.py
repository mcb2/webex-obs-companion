"""Synthetic v5 RPCs/events through the locked client's actual decoders."""

from unittest.mock import Mock

import pytest
from obswebsocket.core import RecvThread

from webex_obs.obs_controller import OBSController


class RecordingPeer:
    def __init__(self, root):
        self.root = root
        self.active = False
        self.duration = 1000
        self.path = None
        self.settings_path = None
        self.paths = []
        self.calls = []
        self.output_name = "simple_file_output"
        self.outputs_available = True
        self.stop_events = True
        self.lose_stop_response = False
        self.lose_start_response = False
        self.reject_stop = False
        self.fail_settings = False
        self.clients = []

    def emit(self, state, path=None, client=None):
        self.event("RecordStateChanged", {
            "outputActive": state != "STOPPED",
            "outputState": "OBS_WEBSOCKET_OUTPUT_" + state,
            "outputPath": str(path) if path else None,
        }, client)

    def event(self, name, data, client=None):
        client = client or self.clients[-1]
        event = RecvThread(client).build_event({
            "eventType": name, "eventIntent": 64, "eventData": data,
        })
        client.eventmanager.trigger(event)

    def respond(self, name, data):
        self.calls.append(name)
        if name == "GetRecordStatus":
            return True, {
                "outputActive": self.active, "outputPaused": False,
                "outputTimecode": "00:00:01.000", "outputDuration": self.duration,
                "outputBytes": 128,
            }
        if name == "GetOutputList":
            kind = "ffmpeg_output" if self.output_name.endswith("ffmpeg_output") else "ffmpeg_muxer"
            outputs = [{"outputName": self.output_name, "outputKind": kind,
                        "outputActive": self.active}] if self.outputs_available else []
            # A stream/replay/custom file output must never supply recovery paths.
            outputs.append({"outputName": "unrelated_output", "outputKind": "ffmpeg_muxer",
                            "outputActive": True})
            return True, {"outputs": outputs}
        if name == "GetOutputSettings":
            assert data == {"outputName": self.output_name}
            if self.fail_settings:
                raise ConnectionError("synthetic interrupted settings response")
            field = "url" if self.output_name.endswith("ffmpeg_output") else "path"
            return True, {"outputSettings": {field: str(self.settings_path or self.path)}}
        if name == "StartRecord":
            assert not self.active
            self.path = self.root / f"segment-{len(self.paths) + 1:02}.mkv"
            self.path.write_bytes(b"synthetic media")
            self.paths.append(self.path)
            self.active = True
            self.emit("STARTING")
            self.emit("STARTED")  # The documented start event has no path.
            if self.lose_start_response:
                raise ConnectionError("synthetic interrupted start response")
            return True, None
        if name == "StopRecord":
            if self.reject_stop:
                return False, None
            self.active = False
            if self.stop_events:
                self.emit("STOPPED", self.path)
            if self.lose_stop_response:
                raise ConnectionError("synthetic interrupted stop response")
            return True, {"outputPath": str(self.path)}
        return True, None


@pytest.fixture
def recording_peer(tmp_path, obs_v5_client, monkeypatch):
    peer = RecordingPeer(tmp_path)

    def client(*args, **kwargs):
        assert kwargs["legacy"] is False
        connection = obs_v5_client(peer.respond)
        connection.connect = Mock()
        connection.disconnect = Mock()
        peer.clients.append(connection)
        return connection

    monkeypatch.setattr("webex_obs.obs_controller.obsws", client)
    monkeypatch.setattr(OBSController, "obs_is_running", lambda self: True)
    monkeypatch.setattr("webex_obs.obs_controller.time.sleep", lambda _: None)
    def unexpected_command(*args, **kwargs):
        pytest.fail("Recovery tests must not operate OBS or the service")
    monkeypatch.setattr("webex_obs.obs_controller.subprocess.run", unexpected_command)
    controller = OBSController(exit_on_stop=False)
    assert controller.connect()
    return controller, peer


@pytest.mark.parametrize("name", ["simple_file_output", "simple_ffmpeg_output",
                                  "adv_file_output", "adv_ffmpeg_output"])
@pytest.mark.parametrize("events_present", [False, True])
def test_interrupted_stop_returns_live_target_once(recording_peer, name, events_present):
    controller, peer = recording_peer
    peer.output_name = name
    peer.stop_events = events_present
    peer.lose_stop_response = True
    assert controller.start_recording()
    assert controller.recorded_files == [str(peer.path)]
    assert controller.stop_recording() == [str(peer.path)]
    assert controller.stop_verified
    assert controller.stop_recording() == []
    assert peer.calls.count("StartRecord") == peer.calls.count("StopRecord") == 1


def test_stop_event_recovers_path_when_live_target_unavailable(recording_peer):
    controller, peer = recording_peer
    peer.outputs_available = False
    assert controller.start_recording()
    assert controller.recorded_files == []
    first = peer.path
    peer.active = False
    peer.emit("STOPPED", first)
    peer.emit("STOPPED", first)
    assert controller.ensure_recording()
    second = peer.path
    # Delayed duplicates for the first file cannot revoke the second output.
    peer.emit("STOPPED", first)
    peer.emit("STOPPED", first)
    assert controller.ensure_recording()
    assert controller.stop_recording() == [str(first), str(second)]
    assert peer.calls.count("StartRecord") == 2


def test_missing_stop_events_and_obs_restart_preserve_every_segment(recording_peer):
    controller, peer = recording_peer
    unrelated = peer.root / "unrelated.mkv"
    unrelated.write_bytes(b"synthetic unrelated media")
    assert controller.start_recording()
    # Unexpected output stop with no response or event.
    peer.active = False
    assert controller.ensure_recording()
    # OBS exits: the replacement server has forgotten the old output path.
    old_client = peer.clients[-1]
    controller.disconnect()
    peer.active = False
    peer.path = None
    assert controller.connect()
    peer.emit("STOPPED", unrelated, client=old_client)
    assert controller.ensure_recording()
    peer.stop_events = False
    peer.lose_stop_response = True
    assert controller.stop_recording() == [str(path) for path in peer.paths]
    assert len(peer.paths) == 3
    assert controller.stop_recording() == []
    assert unrelated.read_bytes() == b"synthetic unrelated media"


def test_reconnected_external_output_is_never_stopped_or_adopted(recording_peer):
    controller, peer = recording_peer
    assert controller.start_recording()
    original = peer.path
    controller.disconnect()
    peer.path = peer.root / "external.mkv"
    peer.path.write_bytes(b"synthetic external media")
    assert controller.connect()
    peer.emit("STOPPED", peer.path)
    assert not controller.ensure_recording()
    assert controller.stop_recording() == []
    assert "StopRecord" not in peer.calls
    assert controller.recorded_files == [str(original)]
    peer.active = False
    assert controller.stop_recording() == [str(original)]


def test_lost_start_response_and_events_do_not_establish_ownership(recording_peer):
    controller, peer = recording_peer
    peer.lose_start_response = True
    assert not controller.start_recording()
    assert controller.is_recording
    assert controller.recorded_files == []
    assert controller.stop_recording() == []
    peer.active = False
    peer.emit("STOPPED", peer.path)
    assert controller.stop_recording() == []
    assert controller.stop_verified
    assert "GetOutputSettings" not in peer.calls
    assert "StopRecord" not in peer.calls
    assert peer.path.exists()


def test_settings_response_interrupted_then_retried_before_obs_exits(recording_peer):
    controller, peer = recording_peer
    peer.fail_settings = True
    assert controller.start_recording()
    assert controller.recorded_files == []
    peer.fail_settings = False
    assert controller.ensure_recording()
    original = peer.path
    controller.disconnect()
    peer.active = False
    peer.path = None
    assert controller.connect()
    assert controller.stop_recording() == [str(original)]


def test_changed_live_file_target_revokes_ownership(recording_peer):
    controller, peer = recording_peer
    assert controller.start_recording()
    original = peer.path
    peer.path = peer.root / "external.mkv"
    peer.path.write_bytes(b"synthetic external media")
    # No duration decrease and no stop/start events: settings still detect it.
    assert not controller.ensure_recording()
    assert controller.stop_recording() == []
    assert controller.recorded_files == [str(original)]
    assert "StopRecord" not in peer.calls


def test_file_split_event_retains_original_and_new_path(recording_peer):
    controller, peer = recording_peer
    assert controller.start_recording()
    first = peer.path
    second = peer.root / "split.mkv"
    second.write_bytes(b"synthetic split media")
    peer.event("RecordFileChanged", {"newOutputPath": str(second)})
    peer.event("RecordFileChanged", {"newOutputPath": str(second)})
    # OBS output settings can retain the original path after an internal split.
    assert controller.ensure_recording()
    peer.path = second
    assert controller.stop_recording() == [str(first), str(second)]


def test_unknown_paths_are_not_guessed_from_files_in_recording_directory(recording_peer):
    controller, peer = recording_peer
    peer.outputs_available = False
    peer.stop_events = False
    peer.lose_stop_response = True
    assert controller.start_recording()
    unrelated = peer.root / "recent.mkv"
    unrelated.write_bytes(b"synthetic unrelated media")
    assert controller.stop_recording() == []
    assert controller.stop_verified
    assert peer.path.exists() and unrelated.exists()


def test_late_event_keeps_session_order(recording_peer):
    controller, peer = recording_peer
    peer.outputs_available = False
    assert controller.start_recording()
    first = peer.path
    first_segment = controller._segment_index
    peer.active = False
    assert controller.ensure_recording()
    second = peer.path
    # Simulate the callback having tagged a final event before a recovery start,
    # but delivery to the lifecycle worker being delayed until afterward.
    controller._record_events.put((controller._connection_generation, first_segment,
                                  "RecordStateChanged", {"outputActive": False,
                                  "outputState": "OBS_WEBSOCKET_OUTPUT_STOPPED",
                                  "outputPath": str(first)}))
    assert controller.stop_recording() == [str(first), str(second)]


def test_receiver_callback_does_not_wait_for_lifecycle_lock(recording_peer):
    import threading
    from webex_obs.recording_backend import OBSRecordingBackend

    controller, peer = recording_peer
    peer.outputs_available = False
    assert controller.start_recording()
    backend = OBSRecordingBackend(controller)
    peer.active = False
    with backend._lifecycle_lock:
        receiver = threading.Thread(target=peer.emit, args=("STOPPED", peer.path))
        receiver.start()
        receiver.join(1)
        assert not receiver.is_alive()
        assert controller.recorded_files == []  # Callback only enqueues.
    assert backend.stop_recording() == [str(peer.path)]


@pytest.mark.parametrize("mode", ["unsupported", "multiple", "remote", "relative"])
def test_unidentifiable_file_target_never_adopts_directory_contents(recording_peer, mode):
    controller, peer = recording_peer
    if mode == "unsupported":
        peer.output_name = "custom_recording_output"
    original_respond = peer.respond

    def respond(name, data):
        accepted, response = original_respond(name, data)
        if mode == "multiple" and name == "GetOutputList":
            response["outputs"].append({"outputName": "adv_file_output", "outputActive": True})
        elif name == "GetOutputSettings" and mode in {"remote", "relative"}:
            response["outputSettings"]["path"] = "rtmp://synthetic.invalid/media" if mode == "remote" else "relative.mkv"
        return accepted, response

    # Rebuild the synthetic wire peer with the altered response shape.
    # The handler is captured by the fixture's installed-client transport.
    peer.respond = respond
    controller.disconnect()
    assert controller.connect()
    assert controller.start_recording()
    assert controller.recorded_files == []
    peer.stop_events = False
    peer.lose_stop_response = True
    assert controller.stop_recording() == []
    assert controller.stop_verified
    assert peer.path.exists()


def test_external_start_events_revoke_ownership_without_file_settings(recording_peer):
    controller, peer = recording_peer
    peer.outputs_available = False
    assert controller.start_recording()
    # Consecutive duplicates of the accepted start do not affect ownership.
    peer.emit("STARTED")
    peer.emit("STARTED")
    assert controller.ensure_recording()
    # Our stop event was lost, but the independent start was observed.
    external = peer.root / "external.mkv"
    external.write_bytes(b"synthetic external media")
    peer.path = external
    peer.emit("STARTING")
    peer.emit("STARTED")
    assert not controller.ensure_recording()
    peer.active = False
    peer.emit("STOPPED", external)
    assert controller.stop_recording() == []
    assert "StopRecord" not in peer.calls
    assert external.exists()


def test_output_stop_during_live_path_probe_cannot_report_started(recording_peer):
    controller, peer = recording_peer
    original_respond = peer.respond
    interrupted = False

    def respond(name, data):
        nonlocal interrupted
        accepted, response = original_respond(name, data)
        if name == "GetOutputSettings" and not interrupted:
            interrupted = True
            peer.active = False
            peer.emit("STOPPED", peer.path)
        return accepted, response

    peer.respond = respond
    controller.disconnect()
    assert controller.connect()
    assert controller.start_recording()
    assert peer.active
    assert len(peer.paths) == 2  # The first stopped before start verification finished.
    assert controller.stop_recording() == [str(path) for path in peer.paths]


def test_late_event_cannot_add_paths_to_segment_that_lost_ownership(recording_peer):
    controller, peer = recording_peer
    assert controller.start_recording()
    original = peer.path
    old_segment = controller._segment_index
    peer.duration = 1
    assert not controller.ensure_recording()
    peer.active = False
    assert controller.ensure_recording()
    external = peer.root / "external.mkv"
    external.write_bytes(b"synthetic external media")
    controller._record_events.put((controller._connection_generation, old_segment,
                                  "RecordStateChanged", {"outputActive": False,
                                  "outputState": "OBS_WEBSOCKET_OUTPUT_STOPPED",
                                  "outputPath": str(external)}))
    assert controller.stop_recording() == [str(original), str(peer.path)]
    assert external.exists()


def test_stop_response_supplies_final_split_path_when_split_event_is_missing(recording_peer):
    controller, peer = recording_peer
    assert controller.start_recording()
    first = peer.path
    final = peer.root / "final-split.mkv"
    final.write_bytes(b"synthetic final split")
    # OBS's initial settings path can stay unchanged after an internal split.
    peer.settings_path = first
    peer.path = final
    peer.stop_events = False
    assert controller.stop_recording() == [str(first), str(final)]


def test_stopped_event_after_idle_poll_still_completes_owned_segment(recording_peer):
    controller, peer = recording_peer
    peer.outputs_available = False
    assert controller.start_recording()
    first = peer.path
    peer.active = False
    assert controller._refresh_recording_status() is False
    assert not controller._owns_recording
    peer.emit("STOPPED", first)
    assert controller.ensure_recording()
    assert controller.stop_recording() == [str(first), str(peer.path)]


def test_accepted_recovery_start_establishes_ownership_after_preparation_reconnect(
    recording_peer, monkeypatch
):
    controller, peer = recording_peer
    assert controller.start_recording()
    peer.active = False
    original_switch = controller.switch_scene

    def reconnect_then_switch(scene_name):
        controller.disconnect()
        assert controller.connect()
        return original_switch(scene_name)

    monkeypatch.setattr(controller, "switch_scene", reconnect_then_switch)
    assert controller.ensure_recording()
    assert not controller._ownership_lost
    assert len(peer.paths) == 2
    assert controller.stop_recording() == [str(path) for path in peer.paths]
