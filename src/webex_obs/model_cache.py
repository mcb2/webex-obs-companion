"""Repository-specific MLX Whisper caches, checked without loading Metal/models."""

import hashlib
import json
import math
import re
from pathlib import Path

MODELS_CACHE_DIR = Path.home() / ".cache" / "webex_obs" / "models"
REQUIRED_ARTIFACTS = ("config.json", "weights.safetensors")
_DIMENSIONS = (
    "n_mels", "n_audio_ctx", "n_audio_state", "n_audio_head", "n_audio_layer",
    "n_vocab", "n_text_ctx", "n_text_state", "n_text_head", "n_text_layer",
)
_DTYPE_BYTES = {"BOOL": 1, "U8": 1, "I8": 1, "I16": 2, "U16": 2,
                "F16": 2, "BF16": 2, "I32": 4, "U32": 4, "F32": 4,
                "I64": 8, "U64": 8, "F64": 8}


def model_cache_path(model_name: str, root: Path = MODELS_CACHE_DIR) -> Path:
    """Hash the full repository identity; never interpret it as a filesystem path."""
    readable = re.sub(r"[^a-zA-Z0-9._-]", "-", model_name)[-80:]
    identity = hashlib.sha256(model_name.encode("utf-8")).hexdigest()
    return root / f"{readable}-{identity}"


def artifact_valid(path: Path) -> bool:
    """Check config dimensions or tensor layout and truncation, without inference.

    This is structural validation, not a weight checksum or a model load test.
    """
    try:
        if path.name == "config.json":
            config = json.loads(path.read_text(encoding="utf-8"))
            return isinstance(config, dict) and all(
                type(config.get(key)) is int and config[key] > 0 for key in _DIMENSIONS
            )
        size = path.stat().st_size
        with path.open("rb") as stream:
            header_size = int.from_bytes(stream.read(8), "little")
            if not 2 <= header_size <= min(size - 8, 16 * 1024 * 1024):
                return False
            header = json.loads(stream.read(header_size))
        if not isinstance(header, dict):
            return False
        intervals = []
        for name, tensor in header.items():
            if name == "__metadata__":
                continue
            shape = tensor["shape"]
            start, end = tensor["data_offsets"]
            if (not isinstance(shape, list)
                    or any(type(n) is not int or n < 0 for n in shape)
                    or type(start) is not int or type(end) is not int or start < 0
                    or end - start != math.prod(shape) * _DTYPE_BYTES[tensor["dtype"]]):
                return False
            intervals.append((start, end))
        offset = 0
        for start, end in sorted(intervals):
            if start != offset:
                return False
            offset = end
        return bool(intervals) and offset > 0 and offset == size - 8 - header_size
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def invalid_artifacts(directory: Path) -> list[str]:
    return [name for name in REQUIRED_ARTIFACTS if not artifact_valid(directory / name)]
