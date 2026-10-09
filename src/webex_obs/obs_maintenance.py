"""Idle-only OBS maintenance, driven by the recording lifecycle (not a timer thread)."""

import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)


class OBSMaintenance:
    def __init__(self):
        self.last_refresh = time.monotonic()
        self.last_busy = self.last_refresh
        self.retry_after = 0.0

    def mark_busy(self) -> None:
        self.last_busy = time.monotonic()

    def maintain(self, controller, interval_minutes: int, start_pending) -> bool:
        now = time.monotonic()
        # Protect :00 and :30, including five minutes before and after each.
        minute = datetime.now().minute
        if (controller.exit_on_stop or not interval_minutes
                or controller.address not in {"localhost", "127.0.0.1", "::1"}
                or start_pending() or controller.is_recording
                or now - self.last_refresh < interval_minutes * 60
                or now - self.last_busy < 120 or now < self.retry_after
                or minute % 30 < 5 or minute % 30 >= 25):
            return False
        if not controller.obs_is_running():
            self.retry_after = now + 120
            if controller.connect():
                self.last_refresh = time.monotonic()
                return True
            return False
        # Recheck the request after potentially slow WebSocket checks, immediately
        # before quitting. A request during shutdown is served after reconnecting.
        if not controller.outputs_confirmed_idle() or start_pending():
            self.mark_busy()
            return False
        self.retry_after = now + 120
        logger.info("Idle OBS maintenance restart beginning (interval %d minutes).", interval_minutes)
        if not controller.quit_obs(idle_guard=lambda: (
                not start_pending() and controller.outputs_confirmed_idle()
                and not start_pending())):
            logger.warning("Idle OBS restart deferred: graceful shutdown did not complete.")
            return False
        if not controller.connect():
            logger.warning("Idle OBS restart could not reconnect; recording start will retry.")
            return False
        self.last_refresh = time.monotonic()
        logger.info("Idle OBS maintenance complete; OBS is ready for recording.")
        return True
