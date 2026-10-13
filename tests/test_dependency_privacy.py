"""Verify telemetry is disabled at the dependency import boundary in fresh processes."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("entry", ["transcriber", "diarizer", "cli"])
def test_telemetry_disabled_before_dependency_imports(tmp_path, entry):
    script = textwrap.dedent('''
        import importlib
        import importlib.abc
        import importlib.util
        import os
        import sys
        import types

        expected = {"HF_HUB_DISABLE_TELEMETRY": "1", "PYANNOTE_METRICS_ENABLED": "0",
                    "OTEL_SDK_DISABLED": "true", "HF_HUB_DISABLE_XET": "1"}
        seen = set()

        def deny_network(event, args):
            if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
                raise AssertionError("Network is forbidden in this contract test")

        sys.addaudithook(deny_network)

        class Dependencies(importlib.abc.MetaPathFinder, importlib.abc.Loader):
            def find_spec(self, fullname, path=None, target=None):
                if fullname in {"mlx_whisper", "huggingface_hub", "torch", "pyannote", "pyannote.audio"}:
                    assert all(os.environ.get(k) == v for k, v in expected.items()), fullname
                    seen.add(fullname)
                    return importlib.util.spec_from_loader(fullname, self, is_package=fullname == "pyannote")

            def create_module(self, spec):
                return None

            def exec_module(self, module):
                if module.__name__ == "mlx_whisper":
                    import huggingface_hub
                elif module.__name__ == "torch":
                    module.device = lambda value: value
                elif module.__name__ == "pyannote.audio":
                    def from_pretrained(checkpoint, *, token):
                        assert checkpoint == "pyannote/speaker-diarization-community-1"
                        assert token == "synthetic-token"
                        return types.SimpleNamespace(to=lambda device: None)
                    module.Pipeline = types.SimpleNamespace(from_pretrained=from_pretrained)

        sys.meta_path.insert(0, Dependencies())
        module = importlib.import_module("webex_obs." + sys.argv[1])
        if sys.argv[1] == "diarizer":
            assert module.NeuralDiarizer(hf_token="synthetic-token", device="cpu")._init_pyannote()
            assert {"torch", "pyannote.audio"} <= seen
        elif sys.argv[1] == "transcriber":
            assert {"mlx_whisper", "huggingface_hub"} <= seen
        else:
            import huggingface_hub
            assert "huggingface_hub" in seen
        assert all(os.environ.get(k) == v for k, v in expected.items())
    ''')
    # Do not inherit real tokens, user configuration, or model-cache locations.
    env = {
        "PATH": os.defpath, "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "HF_HOME": str(tmp_path / "hf"), "HF_HUB_OFFLINE": "1",
        "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
        "HF_HUB_DISABLE_TELEMETRY": "0", "PYANNOTE_METRICS_ENABLED": "1",
        "OTEL_SDK_DISABLED": "false", "HF_HUB_DISABLE_XET": "0",
    }
    result = subprocess.run(
        [sys.executable, "-c", script, entry], env=env, cwd=tmp_path,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
