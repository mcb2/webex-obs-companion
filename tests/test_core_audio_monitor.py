import ctypes

import pytest

from webex_obs.core_audio_monitor import (
    AudioObjectPropertyAddress,
    CoreAudioProcessMonitor,
    K_AUDIO_HARDWARE_PROPERTY_TRANSLATE_PID,
    K_AUDIO_PROCESS_PROPERTY_RUNNING_INPUT,
    K_AUDIO_PROCESS_PROPERTY_RUNNING_OUTPUT,
)


class _Function:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class _FakeCoreAudio:
    def __init__(self, has_process_api=True, input_active=True, output_active=False):
        self.has_process_api = has_process_api
        self.input_active = input_active
        self.output_active = output_active
        self.AudioObjectHasProperty = _Function(self._has_property)
        self.AudioObjectGetPropertyData = _Function(self._get_property)

    def _has_property(self, _object_id, _address):
        return self.has_process_api

    def _get_property(
        self,
        _object_id,
        address,
        _qualifier_size,
        _qualifier,
        _data_size,
        output,
    ):
        selector = ctypes.cast(
            address, ctypes.POINTER(AudioObjectPropertyAddress)
        ).contents.mSelector
        value = ctypes.cast(output, ctypes.POINTER(ctypes.c_uint32))
        if selector == K_AUDIO_HARDWARE_PROPERTY_TRANSLATE_PID:
            value.contents.value = 99
        elif selector == K_AUDIO_PROCESS_PROPERTY_RUNNING_INPUT:
            value.contents.value = int(self.input_active)
        elif selector == K_AUDIO_PROCESS_PROPERTY_RUNNING_OUTPUT:
            value.contents.value = int(self.output_active)
        else:
            return -1
        return 0


def test_reports_input_and_output_activity_for_requested_pid():
    monitor = CoreAudioProcessMonitor(
        _FakeCoreAudio(input_active=True, output_active=True)
    )

    activity = monitor.activity_for_pids([42])

    assert activity.available
    assert activity.active
    assert activity.input_pids == (42,)
    assert activity.output_pids == (42,)


def test_unavailable_process_api_falls_back_cleanly():
    monitor = CoreAudioProcessMonitor(_FakeCoreAudio(has_process_api=False))

    activity = monitor.activity_for_pids([42])

    assert not activity.available
    assert not activity.active


def test_disappearing_process_does_not_hide_other_process_audio(monkeypatch):
    monitor = CoreAudioProcessMonitor(_FakeCoreAudio(input_active=True))
    requested = []

    def translate(pid):
        requested.append(pid)
        if pid == 10:
            raise OSError("process exited")
        return 99

    monkeypatch.setattr(monitor, "_translate_pid", translate)
    activity = monitor.activity_for_pids([20, 10, 20])
    assert activity.available
    assert activity.input_pids == (20,)
    assert activity.failed_pids == (10,)
    assert requested == [10, 20]


@pytest.mark.parametrize("selector", [K_AUDIO_HARDWARE_PROPERTY_TRANSLATE_PID,
                                      K_AUDIO_PROCESS_PROPERTY_RUNNING_INPUT,
                                      K_AUDIO_PROCESS_PROPERTY_RUNNING_OUTPUT])
def test_nonzero_core_audio_status_is_failed_not_inactive(selector):
    library = _FakeCoreAudio(input_active=False, output_active=False)
    original = library._get_property

    def fail(*args):
        address = ctypes.cast(args[1], ctypes.POINTER(AudioObjectPropertyAddress)).contents
        return -1 if address.mSelector == selector else original(*args)

    library.AudioObjectGetPropertyData.callback = fail
    activity = CoreAudioProcessMonitor(library).activity_for_pids([42])
    assert activity.available
    assert not activity.active
    assert activity.failed_pids == (42,)


def test_failed_input_read_preserves_positive_output(monkeypatch):
    monitor = CoreAudioProcessMonitor(_FakeCoreAudio(output_active=True))
    original = monitor._read_boolean

    def read(object_id, selector):
        if selector == K_AUDIO_PROCESS_PROPERTY_RUNNING_INPUT:
            raise OSError("synthetic failed input read")
        return original(object_id, selector)

    monkeypatch.setattr(monitor, "_read_boolean", read)
    activity = monitor.activity_for_pids([42])
    assert activity.output_pids == (42,)
    assert activity.failed_pids == (42,)


def test_successful_zero_translation_is_confirmed_no_audio_object():
    library = _FakeCoreAudio()
    library.AudioObjectGetPropertyData.callback = lambda *args: 0
    activity = CoreAudioProcessMonitor(library).activity_for_pids([42])
    assert activity.available
    assert not activity.active
    assert not activity.failed_pids
