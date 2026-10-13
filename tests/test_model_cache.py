import io
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from rich.console import Console

from webex_obs import cli
from webex_obs.model_cache import artifact_valid, invalid_artifacts, model_cache_path


@pytest.fixture
def model_artifacts():
    config = {key: 2 for key in (
        "n_mels", "n_audio_ctx", "n_audio_state", "n_audio_head", "n_audio_layer",
        "n_vocab", "n_text_ctx", "n_text_state", "n_text_head", "n_text_layer",
    )}
    header = json.dumps({"synthetic.weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
    return {"config.json": json.dumps(config).encode(),
            "weights.safetensors": len(header).to_bytes(8, "little") + header + b"\0" * 4}


def populate(directory, artifacts):
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in artifacts.items():
        (directory / name).write_bytes(data)


@pytest.mark.parametrize("first,second", [
    ("owner-one/same-model", "owner-two/same-model"),
    ("owner/model", "owner-model"), ("A/model", "a/model"),
])
def test_full_repository_identity_avoids_collisions(tmp_path, first, second):
    assert model_cache_path(first, tmp_path) != model_cache_path(second, tmp_path)


def test_unusual_names_never_escape_cache_root(tmp_path):
    for name in ("../../outside", "/absolute/path", "org/雪 & model", ".", "", "a/" * 500):
        path = model_cache_path(name, tmp_path)
        assert path.parent == tmp_path
        assert len(path.name) < 255


@pytest.mark.parametrize("damage", ["missing-config", "missing-weights", "empty", "html", "truncated", "bad-offsets", "bad-dimensions"])
def test_missing_or_corrupt_artifacts_are_not_complete(tmp_path, model_artifacts, damage):
    populate(tmp_path, model_artifacts)
    if damage.startswith("missing-"):
        (tmp_path / ("config.json" if damage == "missing-config" else "weights.safetensors")).unlink()
    elif damage == "bad-dimensions":
        (tmp_path / "config.json").write_text('{"n_mels": 0}')
    else:
        weights = tmp_path / "weights.safetensors"
        weights.write_bytes({"empty": b"", "html": b"<html>error</html>",
                             "truncated": model_artifacts[weights.name][:-1],
                             "bad-offsets": model_artifacts[weights.name].replace(b"[0, 4]", b"[9, 4]")}[damage])
    assert invalid_artifacts(tmp_path)


@pytest.mark.parametrize("failure", ["404-config", "404-weights", "exception-config", "empty-weights", "corrupt-config", "truncated-weights", "none"])
def test_prefetch_reports_incomplete_required_artifacts(tmp_path, monkeypatch, model_artifacts, failure):
    monkeypatch.setattr(cli, "MODELS_CACHE_DIR", tmp_path)
    monkeypatch.setattr(cli, "_check_ffmpeg", lambda: True)
    output = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=output, color_system=None))
    def download(url, dest, token):
        if failure == "404-config" and dest.name == "config.json" or failure == "404-weights" and dest.name == "weights.safetensors":
            return False
        if failure == "exception-config" and dest.name == "config.json":
            raise OSError("synthetic secret token must not be printed")
        data = model_artifacts[dest.name]
        if failure == "empty-weights" and dest.name == "weights.safetensors":
            data = b""
        elif failure == "corrupt-config" and dest.name == "config.json":
            data = b"{broken"
        elif failure == "truncated-weights" and dest.name == "weights.safetensors":
            data = data[:-1]
        dest.write_bytes(data)
        return True
    monkeypatch.setattr(cli, "_stream_download_file", download)
    if failure == "none":
        cli.prefetch_model("org/model", hf_token="synthetic token")
        assert "successfully" in output.getvalue()
        assert not invalid_artifacts(model_cache_path("org/model", tmp_path))
    else:
        with pytest.raises(cli.typer.Exit) as exc:
            cli.prefetch_model("org/model", hf_token="synthetic token")
        assert exc.value.exit_code == 1
        assert "incomplete" in output.getvalue()
        assert "successfully" not in output.getvalue()
        assert "secret token" not in output.getvalue()


def test_prefetch_repairs_corrupt_artifact_and_reuses_valid_files(tmp_path, monkeypatch, model_artifacts):
    monkeypatch.setattr(cli, "MODELS_CACHE_DIR", tmp_path)
    monkeypatch.setattr(cli, "_check_ffmpeg", lambda: True)
    target = model_cache_path("org/model", tmp_path)
    populate(target, model_artifacts)
    (target / "config.json").write_text("{}")
    download = Mock(side_effect=lambda url, dest, token: dest.write_bytes(model_artifacts[dest.name]))
    monkeypatch.setattr(cli, "_stream_download_file", download)
    cli.prefetch_model("org/model", hf_token=None)
    assert [c.args[1].name for c in download.call_args_list] == ["config.json"]
    assert not invalid_artifacts(target)
    cli.prefetch_model("org/model", hf_token=None)
    assert download.call_count == 1


@pytest.mark.parametrize("damage", [False, True])
def test_transcriber_uses_only_complete_repository_specific_cache(tmp_path, monkeypatch, speech_modules, model_artifacts, damage):
    monkeypatch.setattr("webex_obs.transcriber.MODELS_CACHE_DIR", tmp_path)
    # A legacy basename cache has no reliable owner identity and must be ignored.
    populate(tmp_path / "model", model_artifacts)
    populate(model_cache_path("other/model", tmp_path), model_artifacts)
    transcriber = speech_modules["Transcriber"]("org/model", tmp_path, enable_diarization=False)
    assert transcriber.model_target == "org/model"
    target = model_cache_path("org/model", tmp_path)
    populate(target, model_artifacts)
    if damage:
        (target / "config.json").write_text("not json")
    transcriber = speech_modules["Transcriber"]("org/model", tmp_path, enable_diarization=False)
    assert transcriber.model_target == ("org/model" if damage else str(target))


@pytest.mark.parametrize("status,data,length", [(404, b"", 0), (200, b"", 0), (200, b"partial", 100), (200, b"complete", 8)])
def test_stream_download_does_not_replace_existing_file_with_partial_data(tmp_path, monkeypatch, status, data, length):
    target = tmp_path / "weights.safetensors"
    target.write_bytes(b"previous synthetic weights")
    response = Mock(status_code=status, headers={"content-length": str(length)})
    response.iter_content.return_value = [data]
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    get = Mock(return_value=response)
    monkeypatch.setattr(cli.requests, "get", get)
    if status == 404:
        assert cli._stream_download_file("https://synthetic.invalid/model", target) is False
    elif not data or len(data) != length:
        with pytest.raises(OSError):
            cli._stream_download_file("https://synthetic.invalid/model", target)
    else:
        assert cli._stream_download_file("https://synthetic.invalid/model", target) is True
    assert target.read_bytes() == (data if status == 200 and len(data) == length and data else b"previous synthetic weights")
    assert not list(tmp_path.glob(".webex-obs-*"))
    assert get.call_args.kwargs["headers"]["Accept-Encoding"] == "identity"
