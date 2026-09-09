import datetime
import logging
import os
import shutil
import tempfile
from pathlib import Path

# Ensure Homebrew and macOS user binary paths are in PATH for ffmpeg
extra_paths = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local" / "bin")]
current_path = os.environ.get("PATH", "")
for p in extra_paths:
    if p not in current_path and os.path.exists(p):
        current_path = f"{p}:{current_path}"
os.environ["PATH"] = current_path

# Disable fast transfer CAS client that fails on corporate CDNs / expired signed URLs
os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"

# Ensure macOS system root certs and certifi bundle are active for HuggingFace downloads
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

try:
    import certifi
    if "SSL_CERT_FILE" not in os.environ:
        os.environ["SSL_CERT_FILE"] = certifi.where()
    if "REQUESTS_CA_BUNDLE" not in os.environ:
        os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
except Exception:
    pass

import mlx_whisper
from .diarizer import NeuralDiarizer

logger = logging.getLogger(__name__)

MODELS_CACHE_DIR = Path.home() / ".cache" / "webex_obs" / "models"


class Transcriber:
    def __init__(
        self,
        model_name: str,
        transcripts_dir: Path,
        enable_diarization: bool = True,
        hf_token: str | None = None,
    ):
        self.model_name = model_name
        self.transcripts_dir = Path(os.path.expanduser(str(transcripts_dir))).resolve()

        self.enable_diarization = enable_diarization
        self.hf_token = hf_token
        self.diarizer = NeuralDiarizer(hf_token=hf_token)

        # Check for locally pre-fetched model directory
        model_slug = Path(model_name).name
        local_dir = MODELS_CACHE_DIR / model_slug
        if local_dir.exists() and (local_dir / "weights.safetensors").exists():
            self.model_target = str(local_dir)
            logger.info(f"Using local pre-fetched Whisper model at: {self.model_target}")
        else:
            self.model_target = model_name

    def transcribe_files(self, media_files: list[str]) -> Path | None:
        if not media_files:
            logger.warning("No media files provided for transcription.")
            return None

        # Verify ffmpeg is available
        if not shutil.which("ffmpeg"):
            logger.error(
                "ffmpeg executable not found in PATH. "
                "Please install ffmpeg on your Mac with: brew install ffmpeg"
            )
            return None

        # Ensure transcripts directory exists
        try:
            self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(f"Transcript directory is temporarily unavailable: {e}")
            return None

        all_text_segments = []
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out_file = self.transcripts_dir / f"meeting_{timestamp}.txt"

        for idx, fpath in enumerate(media_files, start=1):
            if not os.path.exists(fpath):
                continue
            logger.info(f"Transcribing segment {idx}/{len(media_files)}: {fpath} with {self.model_target}...")

            try:
                # Transcribe with segment timestamps for alignment
                result = mlx_whisper.transcribe(
                    fpath,
                    path_or_hf_repo=self.model_target,
                    word_timestamps=True,
                    verbose=False,
                )

                whisper_segments = result.get("segments", [])
                segment_text = result.get("text", "").strip()

                if not segment_text:
                    continue

                # Neural Diarization Pipeline (with graceful fallback)
                diarized_success = False
                if self.enable_diarization and whisper_segments:
                    logger.info("Running speaker diarization on audio...")
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
                        tmp_wav_path = tmp_wav.name

                    try:
                        extracted = self.diarizer.extract_wav_16k(fpath, tmp_wav_path)
                        if extracted:
                            diar_segments = self.diarizer.diarize_audio(tmp_wav_path)
                            aligned = self.diarizer.align_whisper_segments(whisper_segments, diar_segments)
                            formatted_text = self.diarizer.format_transcript(
                                aligned, title=f"Segment {idx} ({Path(fpath).stem})"
                            )
                            all_text_segments.append(formatted_text)
                            diarized_success = True
                    except Exception as diar_err:
                        logger.warning(f"Diarization failed ({diar_err}), using raw transcript instead.")
                    finally:
                        if os.path.exists(tmp_wav_path):
                            try:
                                os.unlink(tmp_wav_path)
                            except Exception:
                                pass

                if not diarized_success:
                    all_text_segments.append(f"--- Segment {idx} ---\n{segment_text}\n")

            except Exception as e:
                logger.error(f"Failed to transcribe {fpath}: {e}")
                if "ffmpeg" in str(e).lower():
                    logger.error("ffmpeg missing. Install via: brew install ffmpeg")
                elif "SSL" in str(e) or "certificate verify failed" in str(e) or "403" in str(e):
                    logger.error(
                        "Model download / CDN verification failed. "
                        "Run 'uv run webex-obs prefetch-model' to pre-cache the model locally."
                    )

        if not all_text_segments:
            logger.warning("Transcription resulted in empty content.")
            return None

        full_transcript = "\n\n".join(all_text_segments)
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(full_transcript)

        logger.info(f"Transcript saved to: {out_file}")
        return out_file
