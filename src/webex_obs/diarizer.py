import logging
import os
import subprocess
import wave
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)


@dataclass
class DiarizationSegment:
    start: float
    end: float
    speaker: str


@dataclass
class SpokenSegment:
    start: float
    end: float
    speaker: str
    text: str


class NeuralDiarizer:
    def __init__(self, hf_token: str | None = None, device: str = "auto"):
        self.hf_token = hf_token
        self.device = device
        self._pyannote_pipeline = None
        self._pyannote_init_attempted = False

    def _get_device(self) -> str:
        if self.device != "auto":
            return self.device
        try:
            import torch
            if torch.backends.mps.is_available():
                return "mps"
            elif torch.cuda.is_available():
                return "cuda"
        except ImportError:
            pass
        return "cpu"

    def _init_pyannote(self) -> bool:
        """Initialize pyannote.audio pipeline if available."""
        if self._pyannote_pipeline is not None:
            return True
        if self._pyannote_init_attempted:
            return False

        self._pyannote_init_attempted = True

        try:
            import torch
            from pyannote.audio import Pipeline

            token = self.hf_token or os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
            logger.info("Initializing pyannote.audio speaker diarization pipeline...")
            
            pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-community-1",
                token=token,
            )
            if pipeline is None:
                raise RuntimeError("Pretrained diarization pipeline was not loaded")
            device = torch.device(self._get_device())
            pipeline.to(device)
            self._pyannote_pipeline = pipeline
            logger.info(f"pyannote.audio loaded on device: {device}")
            return True
        except ModuleNotFoundError as e:
            logger.warning(
                "Neural diarization dependency '%s' is not installed. "
                "Run 'uv sync --locked --extra neural-diarization' from the repository, "
                "then restart the service. Using acoustic speaker-label fallback.",
                e.name,
            )
            return False
        except Exception as e:
            logger.warning(
                "pyannote.audio could not be initialized (%s). Verify HF_TOKEN and "
                "that the Hugging Face conditions for "
                "pyannote/speaker-diarization-community-1 were accepted. Using acoustic "
                "speaker-label fallback.",
                type(e).__name__,
            )
            return False

    def extract_wav_16k(self, media_path: str, out_wav_path: str) -> bool:
        """Extract 16kHz mono WAV from any media container via ffmpeg."""
        cmd = [
            "ffmpeg",
            "-y",
            "-i", media_path,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            out_wav_path,
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            return res.returncode == 0
        except Exception as e:
            logger.error(f"Failed to extract 16kHz WAV from {media_path}: {e}")
            return False

    def diarize_audio(self, wav_path: str) -> List[DiarizationSegment]:
        """Perform speaker diarization on 16kHz mono WAV file."""
        # Method 1: pyannote.audio pipeline
        if self._init_pyannote() and self._pyannote_pipeline is not None:
            try:
                import numpy as np
                import torch

                # ffmpeg already produced PCM. In-memory input avoids pyannote 4's
                # TorchCodec/FFmpeg shared-library decoding requirement on macOS.
                with wave.open(wav_path, "rb") as wf:
                    if (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) != (1, 2, 16000):
                        raise ValueError("Diarization requires 16 kHz mono PCM16 WAV")
                    audio_bytes = wf.readframes(wf.getnframes())
                if not audio_bytes:
                    raise ValueError("Diarization WAV is empty")
                samples = np.frombuffer(audio_bytes, dtype="<i2").astype(np.float32) / 32768.0
                audio = {"waveform": torch.from_numpy(samples).unsqueeze(0), "sample_rate": 16000}
                output = self._pyannote_pipeline(audio)
                # pyannote 4 returns DiarizeOutput; legacy pipelines return Annotation.
                diarization = getattr(output, "speaker_diarization", output)
                segments = []
                for turn, _, speaker in diarization.itertracks(yield_label=True):
                    segments.append(DiarizationSegment(start=turn.start, end=turn.end, speaker=speaker))
                return segments
            except Exception as e:
                logger.error(
                    "pyannote diarization run failed (%s). Falling back to acoustic clustering.",
                    type(e).__name__,
                )

        # Method 2: Energy & Pitch Acoustic Clustering Fallback
        return self._acoustic_fallback_diarization(wav_path)

    def _acoustic_fallback_diarization(self, wav_path: str) -> List[DiarizationSegment]:
        """Fast, robust acoustic VAD and energy clustering fallback."""
        try:
            import wave
            import numpy as np

            with wave.open(wav_path, "rb") as wf:
                sample_rate = wf.getframerate()
                n_frames = wf.getnframes()
                audio_bytes = wf.readframes(n_frames)

            audio_data = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
            duration = len(audio_data) / sample_rate

            # 2.0-second sliding windows
            win_len = int(sample_rate * 2.0)
            hop_len = int(sample_rate * 1.0)
            segments = []

            for i in range(0, len(audio_data) - win_len + 1, hop_len):
                start_sec = i / sample_rate
                end_sec = (i + win_len) / sample_rate
                chunk = audio_data[i : i + win_len]
                rms = np.sqrt(np.mean(chunk**2))

                # Simple voice activity gate
                if rms > 300:
                    # Estimate primary spectral centroid
                    fft_mag = np.abs(np.fft.rfft(chunk))
                    freqs = np.fft.rfftfreq(len(chunk), 1.0 / sample_rate)
                    centroid = np.sum(freqs * fft_mag) / (np.sum(fft_mag) + 1e-9)

                    # Simple 2-speaker acoustic cluster partition (low vs high pitch)
                    speaker_label = "SPEAKER_00" if centroid < 1400 else "SPEAKER_01"
                    segments.append(DiarizationSegment(start=start_sec, end=end_sec, speaker=speaker_label))

            if not segments:
                segments.append(DiarizationSegment(start=0.0, end=duration, speaker="SPEAKER_00"))

            return segments
        except Exception as e:
            logger.error(f"Acoustic fallback failed: {e}")
            return [DiarizationSegment(start=0.0, end=3600.0, speaker="SPEAKER_00")]

    @staticmethod
    def align_whisper_segments(
        whisper_segments: List[dict], diarization_segments: List[DiarizationSegment]
    ) -> List[SpokenSegment]:
        """
        Merge Whisper ASR text segments with Diarization speaker intervals
        based on maximum timestamp overlap.
        """
        if not diarization_segments:
            return [
                SpokenSegment(
                    start=seg.get("start", 0.0),
                    end=seg.get("end", 0.0),
                    speaker="SPEAKER_00",
                    text=seg.get("text", "").strip(),
                )
                for seg in whisper_segments
            ]

        results: List[SpokenSegment] = []

        for w_seg in whisper_segments:
            w_start = float(w_seg.get("start", 0.0))
            w_end = float(w_seg.get("end", 0.0))
            w_text = w_seg.get("text", "").strip()
            if not w_text:
                continue

            # Calculate overlap with every diarization segment
            best_speaker = "SPEAKER_00"
            max_overlap = 0.0

            for d_seg in diarization_segments:
                overlap_start = max(w_start, d_seg.start)
                overlap_end = min(w_end, d_seg.end)
                overlap = max(0.0, overlap_end - overlap_start)

                if overlap > max_overlap:
                    max_overlap = overlap
                    best_speaker = d_seg.speaker

            # If no direct overlap, find closest temporal segment
            if max_overlap == 0.0 and diarization_segments:
                midpoint = (w_start + w_end) / 2.0
                closest = min(diarization_segments, key=lambda s: abs((s.start + s.end) / 2.0 - midpoint))
                best_speaker = closest.speaker

            results.append(
                SpokenSegment(
                    start=w_start,
                    end=w_end,
                    speaker=best_speaker,
                    text=w_text,
                )
            )

        return results

    @staticmethod
    def format_transcript(aligned_segments: List[SpokenSegment], title: str = "Webex Meeting") -> str:
        """
        Consolidate consecutive turns by the same speaker and format into clean Markdown.
        """
        if not aligned_segments:
            return ""

        # Consolidate consecutive segments from the same speaker
        consolidated: List[SpokenSegment] = []
        curr = None

        for seg in aligned_segments:
            if curr is None:
                curr = SpokenSegment(start=seg.start, end=seg.end, speaker=seg.speaker, text=seg.text)
            elif curr.speaker == seg.speaker and (seg.start - curr.end) <= 3.0:
                curr.end = seg.end
                curr.text = f"{curr.text} {seg.text}"
            else:
                consolidated.append(curr)
                curr = SpokenSegment(start=seg.start, end=seg.end, speaker=seg.speaker, text=seg.text)

        if curr:
            consolidated.append(curr)

        # Build formatted transcript
        unique_speakers = sorted(list({s.speaker for s in consolidated}))
        lines = [
            f"# Meeting Transcript: {title}",
            f"**Detected Speakers:** {len(unique_speakers)} ({', '.join(unique_speakers)})\n",
            "---",
            "### Dialogue Transcript\n",
        ]

        for s in consolidated:
            start_min = int(s.start // 60)
            start_sec = int(s.start % 60)
            time_str = f"{start_min:02d}:{start_sec:02d}"
            lines.append(f"**[{time_str}] {s.speaker}:** {s.text}\n")

        return "\n".join(lines)
