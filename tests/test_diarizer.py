"""Offline contracts: no neural dependency imports, credentials, or model downloads."""

import sys
import types
import wave
from unittest.mock import Mock, create_autospec

import numpy as np
import pytest

from webex_obs.diarizer import DiarizationSegment, NeuralDiarizer


@pytest.fixture
def neural_dependencies(monkeypatch):
    pipeline = Mock()

    def from_pretrained(checkpoint, *, token):
        return pipeline

    factory = create_autospec(from_pretrained, side_effect=from_pretrained)
    audio = types.ModuleType("pyannote.audio")
    audio.Pipeline = types.SimpleNamespace(from_pretrained=factory)
    pyannote = types.ModuleType("pyannote")
    pyannote.__path__ = []
    pyannote.audio = audio
    torch = types.ModuleType("torch")
    torch.device = Mock(side_effect=lambda device: device)
    torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: False))
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch.from_numpy = Mock(side_effect=lambda samples: types.SimpleNamespace(
        unsqueeze=lambda dim: np.expand_dims(samples, axis=dim)
    ))
    for name, module in [("torch", torch), ("pyannote", pyannote), ("pyannote.audio", audio)]:
        monkeypatch.setitem(sys.modules, name, module)
    return factory, pipeline, torch


def write_wav(path, samples, *, channels=1, width=2, rate=16000):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(samples)
    return str(path)


@pytest.mark.parametrize("explicit,hf,legacy,expected", [
    ("synthetic-explicit", "synthetic-hf", "synthetic-legacy", "synthetic-explicit"),
    (None, "synthetic-hf", "synthetic-legacy", "synthetic-hf"),
    (None, None, "synthetic-legacy", "synthetic-legacy"),
    (None, None, None, None),
])
def test_initialization_uses_v4_token_and_local_model_once(
    neural_dependencies, monkeypatch, explicit, hf, legacy, expected
):
    for name, value in [("HF_TOKEN", hf), ("HUGGING_FACE_HUB_TOKEN", legacy)]:
        if value is not None:
            monkeypatch.setenv(name, value)
    factory, pipeline, torch = neural_dependencies
    diarizer = NeuralDiarizer(hf_token=explicit, device="cpu")
    assert diarizer._init_pyannote()
    assert diarizer._init_pyannote()
    factory.assert_called_once_with("pyannote/speaker-diarization-community-1", token=expected)
    torch.device.assert_called_once_with("cpu")
    pipeline.to.assert_called_once_with("cpu")
    assert diarizer._pyannote_pipeline is pipeline


@pytest.mark.parametrize("failure", ["none", "auth", "device", "missing-dependency"])
def test_initialization_failure_is_cached_and_does_not_log_secrets(
    neural_dependencies, monkeypatch, caplog, failure
):
    factory, pipeline, _ = neural_dependencies
    secret = "synthetic-private-token"
    if failure == "none":
        factory.return_value = None
        factory.side_effect = None
    elif failure == "auth":
        factory.side_effect = PermissionError(secret)
    elif failure == "device":
        pipeline.to.side_effect = RuntimeError(secret)
    else:
        monkeypatch.setitem(sys.modules, "pyannote.audio", None)
    diarizer = NeuralDiarizer(hf_token=secret, device="cpu")
    assert not diarizer._init_pyannote()
    assert not diarizer._init_pyannote()
    assert diarizer._pyannote_pipeline is None
    assert factory.call_count == (0 if failure == "missing-dependency" else 1)
    assert "fallback" in caplog.text
    assert secret not in caplog.text


class Annotation:
    def __init__(self, turns):
        self.turns = turns

    def itertracks(self, *, yield_label):
        assert yield_label is True
        for start, end, speaker in self.turns:
            yield types.SimpleNamespace(start=start, end=end), "track", speaker


@pytest.mark.parametrize("legacy,empty", [(False, False), (True, False), (False, True)])
def test_neural_input_and_output_contract(neural_dependencies, tmp_path, monkeypatch, legacy, empty):
    _, pipeline, torch = neural_dependencies
    pcm = np.array([-32768, -16384, 0, 16384, 32767], dtype="<i2")
    path = write_wav(tmp_path / "synthetic.wav", pcm.tobytes())
    turns = [] if empty else [(0.0, 0.5, "SPEAKER_00"), (0.4, 1.0, "SPEAKER_01")]
    annotation = Annotation(turns)
    pipeline.return_value = annotation if legacy else types.SimpleNamespace(
        speaker_diarization=annotation,
        exclusive_speaker_diarization=Annotation([(0, 1, "unused")]),
    )
    diarizer = NeuralDiarizer(device="cpu")
    fallback = Mock()
    monkeypatch.setattr(diarizer, "_acoustic_fallback_diarization", fallback)
    assert diarizer.diarize_audio(path) == [DiarizationSegment(*turn) for turn in turns]
    audio = pipeline.call_args.args[0]
    assert set(audio) == {"waveform", "sample_rate"}
    assert audio["sample_rate"] == 16000
    assert audio["waveform"].shape == (1, len(pcm))
    assert audio["waveform"].dtype == np.float32
    np.testing.assert_array_equal(audio["waveform"][0], pcm.astype(np.float32) / 32768.0)
    torch.from_numpy.assert_called_once()
    fallback.assert_not_called()
    assert (tmp_path / "synthetic.wav").exists()


@pytest.mark.parametrize("failure", ["inference", "output", "stereo", "rate", "width", "empty"])
def test_neural_run_failure_uses_acoustic_fallback(
    neural_dependencies, tmp_path, monkeypatch, caplog, failure
):
    _, pipeline, _ = neural_dependencies
    path = write_wav(
        tmp_path / "synthetic.wav", b"" if failure == "empty" else b"\0" * 8,
        channels=2 if failure == "stereo" else 1,
        rate=8000 if failure == "rate" else 16000,
        width=1 if failure == "width" else 2,
    )
    if failure == "inference":
        pipeline.side_effect = RuntimeError("synthetic-private-inference-detail")
    else:
        pipeline.return_value = object()
    diarizer = NeuralDiarizer(device="cpu")
    fallback = Mock(return_value=[DiarizationSegment(0, 1, "SPEAKER_00")])
    monkeypatch.setattr(diarizer, "_acoustic_fallback_diarization", fallback)
    assert diarizer.diarize_audio(path) == fallback.return_value
    fallback.assert_called_once_with(path)
    assert "Falling back to acoustic clustering" in caplog.text
    assert "synthetic-private-inference-detail" not in caplog.text


@pytest.mark.parametrize("frequency,speaker", [(300, "SPEAKER_00"), (2200, "SPEAKER_01")])
def test_unavailable_neural_backend_keeps_real_acoustic_fallback(
    neural_dependencies, tmp_path, frequency, speaker
):
    factory, _, _ = neural_dependencies
    factory.side_effect = ModuleNotFoundError(name="synthetic-neural-dependency")
    samples = (2000 * np.sin(2 * np.pi * frequency * np.arange(48000) / 16000)).astype("<i2")
    path = write_wav(tmp_path / "synthetic.wav", samples.tobytes())
    diarizer = NeuralDiarizer(device="cpu")
    assert diarizer.diarize_audio(path) == [
        DiarizationSegment(0, 2, speaker), DiarizationSegment(1, 3, speaker)
    ]


@pytest.mark.parametrize("mps,cuda,expected", [(True, True, "mps"), (False, True, "cuda"), (False, False, "cpu")])
def test_auto_device_selection(neural_dependencies, mps, cuda, expected):
    _, _, torch = neural_dependencies
    torch.backends.mps.is_available = lambda: mps
    torch.cuda.is_available = lambda: cuda
    assert NeuralDiarizer()._get_device() == expected
