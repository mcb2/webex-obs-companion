"""Webex OBS Companion package."""
import os

# Enforce local-processing privacy before MLX, Hugging Face, or pyannote imports.
# Hugging Face caches its telemetry setting at import time.
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["PYANNOTE_METRICS_ENABLED"] = "0"
os.environ["OTEL_SDK_DISABLED"] = "true"

__version__ = "0.2.0"
