import logging
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .transcriber import Transcriber
from .webex_client import WebexClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PostProcessingJob:
    """Everything needed to process one finalized recording session."""

    media_files: tuple[str, ...]
    meeting_title: str
    output_stem: str
    transcriber: Transcriber
    webex: WebexClient
    delivery_enabled: bool
    recordings_dir: Path
    retention_days: int


class PostProcessingWorker:
    """Process completed meetings in FIFO order on one background thread."""

    def __init__(self, processor: Callable[[PostProcessingJob], None]):
        self._processor = processor
        self._queue: queue.Queue[PostProcessingJob | object] = queue.Queue()
        self._sentinel = object()
        self._thread: threading.Thread | None = None
        self._shutdown_requested = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="post-processing",
            daemon=True,
        )
        self._thread.start()

    def submit(self, job: PostProcessingJob) -> None:
        if self._shutdown_requested:
            raise RuntimeError("Post-processing worker is shutting down")
        self.start()
        self._queue.put(job)

    def shutdown(self, wait: bool = False) -> None:
        if self._thread is None:
            return
        if not self._shutdown_requested:
            self._shutdown_requested = True
            self._queue.put(self._sentinel)
        if wait:
            self._thread.join()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._sentinel:
                    return
                try:
                    self._processor(item)
                except Exception:
                    logger.exception(
                        "Post-processing failed for meeting '%s'.",
                        item.meeting_title,
                    )
            finally:
                self._queue.task_done()
