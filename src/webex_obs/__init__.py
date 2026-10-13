"""Webex OBS Companion package."""
import os

# Enforce local-processing privacy before MLX, Hugging Face, or pyannote imports.
# Hugging Face caches its telemetry setting at import time.
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["PYANNOTE_METRICS_ENABLED"] = "0"
os.environ["OTEL_SDK_DISABLED"] = "true"

# Hugging Face reads this at import time. The legacy hf_transfer switch does
# not disable the Rust Xet downloader; use ordinary HTTP for model downloads.
os.environ["HF_HUB_DISABLE_XET"] = "1"

__version__ = "0.2.0"
