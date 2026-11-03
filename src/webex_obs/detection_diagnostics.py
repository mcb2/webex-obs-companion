"""Opt-in, local call-detection evidence and human annotations.

Only existing probe results are observed. UI callbacks enqueue nonmodal prompts;
no annotation changes detector or recording state. File I/O uses a bounded worker.
"""

from collections import deque
from datetime import datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import threading
import time
import uuid

from .durable_state import atomic_write

logger = logging.getLogger(__name__)


def resolve_event_time(text: str, now: datetime) -> datetime:
    """Interpret local HH:MM[:SS] as the most recent occurrence, within one hour."""
    try:
        clock = datetime.strptime(text.strip(), "%H:%M:%S" if text.count(":") == 2 else "%H:%M").time()
    except ValueError:
        raise ValueError("Enter a 24-hour local time: HH:MM or HH:MM:SS.") from None
    event = now.replace(hour=clock.hour, minute=clock.minute, second=clock.second, microsecond=0)
    if event > now:
        event -= timedelta(days=1)
    if not 0 <= now.timestamp() - event.timestamp() <= 3600:
        raise ValueError("The event must have occurred within the past 60 minutes.")
    return event


class DetectionDiagnostics:
    # Keep enough history for a one-hour-late marker and its preceding context.
    HISTORY_SECONDS = 65 * 60
    HISTORY_BYTES = 16 * 1024 * 1024
    MATCH_SECONDS = 60
    CONTEXT_SECONDS = 120

    def __init__(self, directory: Path):
        self.path = directory / "detection-reports"
        self.run_id = uuid.uuid4().hex
        self._salt = os.urandom(16)
        self.prompts = queue.Queue()
        self._records = queue.Queue(maxsize=4096)
        self._stop = threading.Event()
        self._worker = None
        self._announced = set()
        self._overflow_warned = False
        # Only the worker accesses history, pending markers, and open reports.
        self._history = deque()
        self._history_bytes = 0
        self._first_observed_at = None
        self._evicted_until = float("-inf")
        self._pending = []
        self._cases = []
        self._retry_at = 0.0

    def start(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._worker = threading.Thread(target=self._write, daemon=True,
                                        name="detection-diagnostics")
        self._worker.start()
        self.record("enabled")
        logger.info("Call detection diagnostic reports enabled: %s", self.path)

    def _write(self) -> None:
        while not self._stop.is_set() or not self._records.empty():
            try:
                payload = self._records.get(timeout=0.1)
            except queue.Empty:
                payload = None
            if payload is not None:
                self._accept(payload)
            # Process already queued detections before deciding that a marker
            # has no match; slow disk writes must not create false disagreements.
            if self._records.empty():
                self._flush_safely(time.time())
        self._flush_safely(time.time(), closing=True)

    def _accept(self, payload: dict) -> None:
        observed = payload["observed_at"]
        if self._first_observed_at is None:
            self._first_observed_at = observed
        size = len(json.dumps(payload).encode("utf-8"))
        self._history.append((payload, size))
        self._history_bytes += size
        while self._history and (self._history_bytes > self.HISTORY_BYTES or
                                self._history[0][0]["observed_at"] < observed - self.HISTORY_SECONDS):
            old, old_size = self._history.popleft()
            self._history_bytes -= old_size
            self._evicted_until = max(self._evicted_until, old["observed_at"])
        if payload["kind"] == "manual_marker":
            self._pending.append(payload)
        elif payload["kind"] == "confirmation":
            category = payload.get("category", "new_call")
            response = payload["response"]
            reason = None
            if category == "suspected_transition":
                reason = {"yes": "confirmed_suspected_transition", "no": "dismissed_suspected_transition",
                          "not_sure": "uncertain_transition"}.get(response)
            elif response in {"no", "not_sure"}:
                suffix = "startup_discovery" if category == "startup_discovery" else "start"
                reason = ("false_" if response == "no" else "uncertain_") + suffix
            if reason:
                self._new_case(payload, reason, payload["detected_at"])

    def _new_case(self, annotation: dict, reason: str, when: float) -> None:
        stamp = datetime.fromtimestamp(when).strftime("%Y-%m-%d_%H-%M-%S")
        self._cases.append(dict(annotation=annotation, reason=reason, event_at=when,
                                filename=f"{stamp}_{reason}_{uuid.uuid4().hex[:8]}.json",
                                last_saved=float("-inf")))

    def _flush_safely(self, now: float, *, closing: bool = False) -> None:
        if now < self._retry_at and not closing:
            return
        try:
            self._flush(now, closing=closing)
        except OSError:
            self._retry_at = now + 60
            logger.exception("Could not save diagnostic report; check LOG_DIR access and free space.")

    def _flush(self, now: float, *, closing: bool = False) -> None:
        for marker in list(self._pending):
            when = marker["occurred_at"]
            # Allow the normal detector to catch up before declaring a mismatch.
            if not closing and now < when + self.MATCH_SECONDS:
                continue
            kind = "detected_start" if marker["event"] == "start" else "detected_end"
            nearby = [row for row, _ in self._history
                      if abs(row.get("detected_at", row["observed_at"]) - when) <= self.MATCH_SECONDS]
            matches = [row for row in nearby if row["kind"] == kind and
                       not row.get("blocked") and row.get("category") != "startup_discovery"]
            if not matches:
                reason = ("missed_" if self._matching_window_complete(when, now) else "unverified_") + marker["event"]
                if marker["event"] == "start":
                    polls = [row for row in nearby if row["kind"] == "poll"]
                    closest = min(polls, key=lambda row: abs(row["observed_at"] - when), default={})
                    blocked = [p for p in closest.get("evidence", []) if p.get("active") is True and
                               (p["platform"] in closest.get("suppressed", {}) or
                                "" in closest.get("suppressed", {}))]
                    if blocked or any(row["kind"] == "detected_start" and row.get("blocked") for row in nearby):
                        reason = "suppression_blocked_start"
                    elif any(row["kind"] == "detected_start" and row.get("category") == "startup_discovery"
                             for row in nearby):
                        reason = "startup_rediscovery"
                    elif any(row["kind"] == "suspected_transition" for row in nearby):
                        reason = "confirmed_suspected_transition"
                self._new_case(marker, reason, when)
            self._pending.remove(marker)
        for case in list(self._cases):
            complete = now >= case["event_at"] + self.CONTEXT_SECONDS
            if closing or complete or now - case["last_saved"] >= 5:
                self._save_case(case, now, complete=complete)
                case["last_saved"] = now
            if complete or closing:
                self._cases.remove(case)

    def _matching_window_complete(self, when: float, now: float) -> bool:
        return (now >= when + self.MATCH_SECONDS and self._first_observed_at is not None and
                self._first_observed_at <= when - self.MATCH_SECONDS and
                self._evicted_until < when - self.MATCH_SECONDS and not self._overflow_warned)

    @staticmethod
    def _compact(rows: list[dict]) -> list[dict]:
        """Collapse unchanged per-sensor/state observations while retaining timing."""
        output, previous = [], {}
        for row in rows:
            key = (row["kind"], row.get("platform"), row.get("sensor"), row.get("pid"))
            state = {k: v for k, v in row.items()
                     if k not in {"run_id", "observed_at", "monotonic", "elapsed_seconds"}}
            last = previous.get(key)
            if last is not None and last[0] == state:
                entry = last[1]
                entry["last_observed_at"] = row["observed_at"]
                entry["observations"] += 1
                if "elapsed_seconds" in row:
                    entry["max_elapsed_seconds"] = max(entry["max_elapsed_seconds"], row["elapsed_seconds"])
            else:
                entry = dict(state, observed_at=row["observed_at"], observations=1)
                if "elapsed_seconds" in row:
                    entry["max_elapsed_seconds"] = row["elapsed_seconds"]
                output.append(entry)
                previous[key] = (state, entry)
        return output

    def _save_case(self, case: dict, now: float, *, complete: bool) -> None:
        when = case["event_at"]
        rows = [row for row, _ in self._history
                if when - self.CONTEXT_SECONDS <= row["observed_at"] <= when + self.CONTEXT_SECONDS]
        descriptions = {
            "false_start": "User answered No to an automatically detected call start.",
            "uncertain_start": "User answered Not sure; this is not a confirmed detector error.",
            "missed_start": "User reported a call start with no detected start within 60 seconds.",
            "missed_end": "User reported a call end with no detected end within 60 seconds.",
            "unverified_start": "Insufficient diagnostic coverage to verify the reported call start.",
            "unverified_end": "Insufficient diagnostic coverage to verify the reported call end.",
            "suppression_blocked_start": "User reported a start while call activity was suppressed by a recording decline.",
            "startup_rediscovery": "Reported start coincides with call activity found at companion startup.",
            "false_startup_discovery": "User denied that a call was in progress when startup discovered activity.",
            "uncertain_startup_discovery": "User was unsure whether startup activity represented an ongoing call.",
            "confirmed_suspected_transition": "User confirmed a suspected call change; no automatic recording split was made.",
            "dismissed_suspected_transition": "User rejected the weaker call-transition heuristic.",
            "uncertain_transition": "User was unsure about a suspected call transition.",
        }
        nearby = [dict(kind=row["kind"], platform=row.get("platform"),
                       at=row.get("detected_at", row["observed_at"]),
                       category=row.get("category", "new_call"), blocked=row.get("blocked", False))
                  for row in rows if row["kind"] in {"detected_start", "detected_end", "suspected_transition"}]
        blocking_rules = set()
        for row in rows:
            if row["kind"] == "poll":
                for evidence in row.get("evidence", []):
                    if evidence.get("active") is not True and evidence.get("decision_rule"):
                        blocking_rules.add(evidence["platform"] + ":" + evidence["decision_rule"])
                for platform in row.get("suppressed", {}):
                    blocking_rules.add((platform or "all") + ":recording_declined")
                if row.get("departed"):
                    blocking_rules.add("awaiting_two_positive_return_polls")
        report = dict(
            summary=descriptions[case["reason"]], reason=case["reason"], run_id=self.run_id,
            blocking_rules=sorted(blocking_rules),
            event_time=datetime.fromtimestamp(when).astimezone().isoformat(),
            event_at=when, annotation=case["annotation"], matching_tolerance_seconds=self.MATCH_SECONDS,
            nearby_detections=nearby, context_seconds=self.CONTEXT_SECONDS,
            updated_at=now, following_context_complete=complete,
            matching_window_complete=self._matching_window_complete(when, now),
            prior_context_complete=(self._first_observed_at is not None and
                                    self._first_observed_at <= when - self.CONTEXT_SECONDS and
                                    self._evicted_until < when - self.CONTEXT_SECONDS),
            queue_overflow=self._overflow_warned, evidence=self._compact(rows),
        )
        atomic_write(self.path / case["filename"], json.dumps(report, indent=2))
        if case["last_saved"] == float("-inf"):
            logger.info("Saved call detection report: %s", self.path / case["filename"])

    def close(self) -> None:
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=2)
            if self._worker.is_alive():
                logger.warning("Diagnostic report shutdown timed out; pending evidence may be incomplete.")

    def record(self, kind: str, **fields) -> None:
        payload = dict(kind=kind, run_id=self.run_id, observed_at=time.time(),
                       monotonic=time.monotonic(), **fields)
        try:
            self._records.put_nowait(payload)
        except queue.Full:
            if not self._overflow_warned:
                self._overflow_warned = True
                logger.warning("Diagnostic log queue full; some evidence was dropped.")

    def sensor(self, platform: str, sensor: str, value, ok: bool, elapsed: float) -> None:
        fields = dict(platform=platform, sensor=sensor, ok=ok, elapsed_seconds=elapsed)
        if ok and sensor == "audio" and value is not None:
            fields.update(available=value.available, input_pids=value.input_pids,
                          output_pids=value.output_pids, failed_pids=value.failed_pids)
        elif ok and sensor == "window":
            fields.update(present=bool(value), title_token=(hashlib.blake2b(
                value.encode(), key=self._salt, digest_size=8).hexdigest() if value else None))
        elif ok and sensor == "sockets":
            fields["ports"] = [port for _, port, _ in value]
        self.record("sensor", **fields)

    def detected(self, platform: str, *, restart: bool = False, category: str = "new_call",
                 blocked: bool = False, blocking_rule: str | None = None) -> None:
        # Declining recording causes repeated candidate confirmation. Ask once per
        # detector call, rearmed only by a verified end or the quick-gap shortcut.
        if platform in self._announced and not restart:
            return
        self._announced.add(platform)
        event = dict(event_id=uuid.uuid4().hex, platform=platform,
                     detected_at=time.time(), restart=restart, category=category,
                     blocked=blocked, blocking_rule=blocking_rule)
        self.record("detected_start", **event)
        self.prompts.put(event)

    def suspected(self, platform: str, *, blocked: bool, rule: str) -> None:
        event = dict(event_id=uuid.uuid4().hex, platform=platform, detected_at=time.time(),
                     category="suspected_transition", blocked=blocked, blocking_rule=rule)
        self.record("suspected_transition", **event)
        self.prompts.put(event)

    def ended(self, platform: str) -> None:
        self._announced.discard(platform)
        self.record("detected_end", platform=platform)

    def answer(self, event: dict, response: str | None) -> None:
        if response in {"yes", "no", "not_sure"}:
            self.record("confirmation", **event, response=response)

    def marker(self, kind: str, occurred_at: datetime) -> None:
        if kind not in {"start", "end"}:
            raise ValueError("Unknown call marker")
        age = time.time() - occurred_at.timestamp()
        if not 0 <= age <= 3600:
            raise ValueError("The event must have occurred within the past 60 minutes.")
        self.record("manual_marker", event=kind, occurred_at=occurred_at.timestamp())
