"""Scoped integration doubles; collection must not replace installed modules."""

import importlib
import os
import sys
import types
from unittest.mock import Mock

import pytest

from webex_obs.config import Config


@pytest.fixture(autouse=True)
def isolated_config_environment(monkeypatch):
    # _env_file=None does not prevent BaseSettings from reading process variables.
    names = set(Config.model_fields)
    for field in Config.model_fields.values():
        alias = field.validation_alias
        names.update(getattr(alias, "choices", []) or ([alias] if isinstance(alias, str) else []))
    normalized = {name.lower() for name in names}
    for name in list(os.environ):
        if name.lower() in normalized:
            monkeypatch.delenv(name)


@pytest.fixture
def speech_modules(monkeypatch):
    """Import speech consumers without loading Metal or downloading model weights.

    Restore both the import cache and package attributes so later tests cannot
    inherit a module referencing this fixture's inference double.
    """
    import webex_obs

    names = ("transcriber", "post_processing", "daemon")
    missing = object()
    cached = {name: sys.modules.get(f"webex_obs.{name}", missing) for name in names}
    attrs = {name: getattr(webex_obs, name, missing) for name in names}
    fake = types.ModuleType("mlx_whisper")
    fake.transcribe = Mock(return_value={"text": "Discussed the roadmap.", "segments": []})
    with monkeypatch.context() as scoped:
        scoped.setitem(sys.modules, "mlx_whisper", fake)
        try:
            for name in names:
                sys.modules.pop(f"webex_obs.{name}", None)
            transcriber = importlib.import_module("webex_obs.transcriber")
            post_processing = importlib.import_module("webex_obs.post_processing")
            daemon = importlib.import_module("webex_obs.daemon")
            yield {
                "Transcriber": transcriber.Transcriber,
                "PostProcessingJob": post_processing.PostProcessingJob,
                "PostProcessingWorker": post_processing.PostProcessingWorker,
                "WebexOBSDaemon": daemon.WebexOBSDaemon,
                "inference": fake.transcribe,
            }
        finally:
            for name in names:
                if cached[name] is missing:
                    sys.modules.pop(f"webex_obs.{name}", None)
                else:
                    sys.modules[f"webex_obs.{name}"] = cached[name]
                if attrs[name] is missing:
                    webex_obs.__dict__.pop(name, None)
                else:
                    setattr(webex_obs, name, attrs[name])
