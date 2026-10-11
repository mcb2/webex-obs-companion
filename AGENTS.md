# Repository development instructions

## Scope and baseline

- This repository is `mcb2/webex-obs-companion`. Use `main` as the baseline.
- Inspect the current code, tests, and working-tree changes before proposing or implementing changes. Preserve unrelated user work.
- Treat conversation history as context to verify, not proof of current behavior. Do not encode historical bugs or proposed architecture as established facts.
- Keep changes focused and minimal. Avoid unrelated refactors, formatting churn, dependency upgrades, and speculative frameworks.

## Project and code map

- Python 3.11+ macOS application; the current MLX Whisper transcription backend requires Apple Silicon.
- `src/webex_obs/daemon.py` coordinates detection, recording, controls, and post-processing.
- `process_monitor.py` and `core_audio_monitor.py` implement Webex, Zoom, and Teams detection using window and media evidence. Dynamic OBS window following is Webex-specific.
- `recording_backend.py` defines `RecordingBackend` and the backend factory. OBS WebSocket is currently the only implemented recording backend; native recording is prospective.
- `obs_controller.py` and `obs_maintenance.py` handle OBS capture, recovery, source selection, and idle maintenance.
- `session.py` manages session titles and filenames; `post_processing.py` queues completed-session work.
- `transcriber.py` and `diarizer.py` provide local transcription and speaker labeling; neural diarization is optional and has an acoustic fallback. Speaker labels do not establish participant identities.
- `macos_ui.py`, `control_dialog.py`, `ui_banner.py`, and `hotkey_listener.py` provide PyObjC/AppKit UI and recording controls.
- `config.py` and `settings_store.py` validate and persist configuration; `service_control.py` manages the LaunchAgent; `webex_client.py` handles Webex delivery.
- Consult `README.md`, `SETUP.md`, and `ARCHITECTURE.md`, then verify relevant details against implementation and tests.

## Implementation conventions

- Follow surrounding Python style and naming. Add useful type hints to new interfaces and document non-obvious lifecycle constraints.
- Keep recording implementation behind the existing backend contract and factory. A future native backend must preserve lifecycle semantics and return all completed segments from `stop_recording()`.
- Keep AppKit work on the main thread. Keep blocking capture, transcription, and network work off the UI thread; preserve synchronization around shared lifecycle state.
- Use module loggers and actionable error messages. Handle expected integration failures explicitly; do not silently swallow failures or report success without confirmation.
- Preserve configuration names, defaults, compatibility aliases, and validated atomic settings updates unless the requested change requires otherwise.
- Update relevant user documentation and `.env.example` when behavior or configuration changes. Keep examples free of real credentials.

## Reliability and privacy

- Prioritize recording integrity over convenience. Verify start/stop state, preserve recovery segments, and avoid duplicate starts or conflicting manual and automatic actions.
- Preserve active-session settings where required, including deferring OBS connection changes until recording stops.
- Keep OBS maintenance guarded against active capture and pending starts. Do not introduce unbounded retries or busy loops.
- Do not delete or discard media until recording has verifiably stopped. Preserve recoverable files when stop, transcription, or delivery fails.
- Preserve consent reminders, discard confirmation, detection grace periods, and suppression behavior unless explicitly changing them with regression coverage.
- Keep speech processing local. Treat Webex delivery as a separate configured outbound action and respect delivery-disabled settings. Do not add cloud processing or telemetry without explicit authorization.
- Treat recordings, transcripts, meeting titles, participant details, and logs as sensitive. Use synthetic or anonymized fixtures; do not copy private meeting content into tests, commits, or review artifacts.
- Never expose or commit `.env`, Webex/Hugging Face tokens, OBS passwords, authorization headers, or other secrets. Preserve masked UI fields and restrictive credential-file permissions; avoid dumping configuration or environment values into logs.

## Validation

- Use `uv` and the committed `uv.lock`. Base environment: `uv sync --locked`; optional neural diarization: `uv sync --locked --extra neural-diarization`. Runtime audio processing also requires `ffmpeg`.
- Run focused tests while iterating, then `uv run --locked pytest` before completing code changes. Tests live in `tests/` and use pytest plus unittest/mocks.
- Add meaningful regression tests for bug fixes and changed behavior, especially detection, recording recovery, concurrency, settings, segment handling, and deletion safeguards.
- Mock OBS, process/audio/window probes, model inference, Webex requests, and filesystem effects where appropriate. Automated tests must not record a real meeting, send messages, alter the installed service, or delete personal files.
- For UI, permissions, capture, or hardware-dependent changes, describe and perform applicable macOS manual checks when available. Automated success alone does not establish real-device behavior.
- Report exact checks and results, including skips and unavailable integrations. Do not claim checks were run when they were not. Documentation-only changes need content and diff validation, not application execution.

## Git and delivery

- Keep work local and uncommitted unless the user requests a commit.
- Never push or open a pull request, including a draft, without explicit user approval. Do not merge, publish releases, or rewrite remote history without explicit authorization.
- Before handing off, inspect the final diff and status for unrelated changes, secrets, generated media, and accidental lockfile changes.
- Explain what changed, why, validation performed, and any remaining limitations. Distinguish implemented behavior from future plans.
