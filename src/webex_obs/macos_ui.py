"""AppKit user interface. Import only when running in a macOS user session.

AppKit owns the main thread; the meeting lifecycle runs in a background thread.
Requests from that thread are handed to the main run loop through a short timer.
"""

import queue
import threading
import logging
import time
from datetime import datetime
from pathlib import Path

import AppKit
import Foundation
import objc

from .config import DEFAULT_ENV_FILE
from .control_dialog import manual_consent_dialog, recording_menu_choices, startup_dialog
from .settings_store import SettingsRollbackError
from .runtime_state import ApplicationStopping
from .service_control import stop_launch_agent
from .ui_banner import UIBanner

logger = logging.getLogger(__name__)


class _MenuTarget(Foundation.NSObject):
    @objc.python_method
    def configure(self, ui):
        self.ui = ui

    def recordingAction_(self, sender):
        generation = self.ui.daemon._window_session_generation
        threading.Thread(
            target=self.ui.perform_recording_action,
            args=(str(sender.representedObject()), generation), name="recording-menu-action", daemon=True,
        ).start()

    def diagnosticMarker_(self, sender):
        self.ui.show_diagnostic_marker(str(sender.representedObject()))

    def diagnosticChoice_(self, sender):
        callback = self.ui._diagnostic_actions.get(sender.tag())
        if callback:
            callback()

    def menuNeedsUpdate_(self, menu):
        self.ui._update_recording_menu()

    def settings_(self, sender):
        self.ui.show_settings()

    def saveSettings_(self, sender):
        self.ui.save_settings()

    def cancelSettings_(self, sender):
        self.ui.close_settings()

    def windowWillClose_(self, notification):
        self.ui._forget_diagnostic_window(notification.object())
        if notification.object() == self.ui._settings_window:
            self.ui._clear_settings()

    def controlChoice_(self, sender):
        AppKit.NSApp.stopModalWithCode_(sender.tag())

    def quit_(self, sender):
        if self.ui.daemon.recorder.is_recording:
            return
        self.ui._quit_service = True
        self.ui.daemon.request_shutdown()

    def applicationShouldTerminate_(self, sender):
        self.ui.daemon.request_shutdown()
        return AppKit.NSTerminateCancel

    def tick_(self, timer):
        self.ui._tick()

    def controlTimeout_(self, timer):
        if AppKit.NSApp.modalWindow() == timer.userInfo():
            AppKit.NSApp.stopModalWithCode_(1)


class _SettingsSidebar(Foundation.NSObject):
    @objc.python_method
    def configure(self, ui, titles):
        self.ui = ui
        self.titles = titles

    def numberOfRowsInTableView_(self, table):
        return len(self.titles)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        return self.titles[row]

    def tableViewSelectionDidChange_(self, notification):
        row = notification.object().selectedRow()
        if row >= 0:
            self.ui._select_settings_category(row)


class MacOSUI:
    def __init__(self, daemon):
        self.daemon = daemon
        self.requests = queue.Queue()
        self.target = None
        self.item = None
        self._status_images = {}
        self._icon_state = None
        self._control_modal_active = False
        self._shutdown_started_at = None
        self._quit_service = False
        self._unload_started = False
        self._unload_done = threading.Event()
        self._settings_saving = False
        self._settings_window = None
        self._settings_config = None
        self._settings_inputs = {}
        self._settings_panes = []
        self._sidebar_source = None
        self._diagnostic_windows = []
        self._diagnostic_actions = {}
        self._diagnostic_tag = 0

    @staticmethod
    def _status_image(badge_symbol=None):
        """Compose a template image so both symbols follow the menu bar theme."""
        base = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "record.circle", "Meeting recorder"
        )
        badge = None
        if badge_symbol:
            badge = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                badge_symbol, None
            )
        if base is None or (badge_symbol and badge is None):
            return None

        def draw_fitted(symbol, rect):
            (x, y), (width, height) = rect
            size = symbol.size()
            scale = min(width / size.width, height / size.height)
            fitted = (size.width * scale, size.height * scale)
            symbol.drawInRect_fromRect_operation_fraction_(
                ((x + (width - fitted[0]) / 2, y + (height - fitted[1]) / 2), fitted),
                AppKit.NSZeroRect, AppKit.NSCompositingOperationSourceOver, 1.0,
            )

        def draw(_rect):
            draw_fitted(base, ((0, 0), (18, 18)))
            if badge is not None:
                # A transparent gap keeps the small badge distinct from the circle.
                AppKit.NSRectFillUsingOperation(
                    ((11, 0), (13, 11)), AppKit.NSCompositingOperationClear
                )
                draw_fitted(badge, ((12, 0), (11, 10)))
            return True

        image = AppKit.NSImage.imageWithSize_flipped_drawingHandler_((24, 18), False, draw)
        image.setTemplate_(True)
        return image

    def _update_status_icon(self, recording, video):
        state = "video" if recording and video else "audio" if recording else "idle"
        if state == self._icon_state:
            return
        button = self.item.button()
        if button is None:
            return
        image = self._status_images.get(state)
        button.setImage_(image)
        button.setTitle_("" if image is not None else {
            "idle": "●", "audio": "● A", "video": "● V",
        }[state])
        button.setAccessibilityLabel_({
            "idle": "Meeting recorder: not recording",
            "audio": "Meeting recorder: recording audio",
            "video": "Meeting recorder: recording video",
        }[state])
        self._icon_state = state

    def run(self):
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        self.target = _MenuTarget.alloc().init()
        self.target.configure(self)
        self.item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        self._status_images = {
            "idle": self._status_image(),
            "audio": self._status_image("mic.fill"),
            "video": self._status_image("video.fill"),
        }
        self._update_status_icon(False, False)
        menu = AppKit.NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        menu.setDelegate_(self.target)
        self.recording_items = {}
        for label, choice in (recording_menu_choices(False)
                              + recording_menu_choices(True)
                              + (("Select Video Source…", "select_window"),
                                 ("Delete Recording After Transcription", "delete_after_transcription"))):
            item = self._add(menu, label, "recordingAction:")
            item.setRepresentedObject_(choice)
            self.recording_items[choice] = item
        if self.daemon.diagnostics is not None:
            for label, kind in (("Diagnostic: New call started…", "start"),
                                ("Diagnostic: Call ended…", "end")):
                self._add(menu, label, "diagnosticMarker:").setRepresentedObject_(kind)
        self._add(menu, "Settings…", "settings:")
        self.quit_item = self._add(menu, "Quit (stop service)", "quit:")
        self._update_recording_menu()
        self.item.setMenu_(menu)
        self.timer = Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.15, self.target, "tick:", None, True
        )
        app.setDelegate_(self.target)
        Foundation.NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, AppKit.NSModalPanelRunLoopMode)
        self.daemon.start_background()
        app.run()

    def _add(self, menu, title, selector):
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, selector, "")
        if selector:
            item.setTarget_(self.target)
        menu.addItem_(item)
        return item

    def _tick(self):
        self._tick_diagnostics()
        if self.daemon._shutdown_event.is_set():
            self._tick_shutdown()
        try:
            # Avoid nested prompts stealing the consent notice's modal timeout.
            while not self._control_modal_active:
                func, result, done = self.requests.get_nowait()
                try:
                    if self.daemon._shutdown_event.is_set():
                        result.append(ApplicationStopping("Application is shutting down"))
                    else:
                        result.append(func())
                except Exception as exc:
                    result.append(exc)
                finally:
                    done.set()
        except queue.Empty:
            pass
        recording, title = self.daemon.status()
        video = self.daemon.recorder.is_video_recording is True
        self._update_status_icon(recording, video)
        self._update_recording_menu()
        if self.item.button():
            if self.daemon.failure:
                self.item.button().setTitle_("!")
                self.item.button().setToolTip_(self.daemon.failure)
                return
            mode = "Recording video" if video else "Recording audio"
            self.item.button().setToolTip_(mode + " • " + title if recording else "Ready for calls")

    def _tick_shutdown(self):
        # Keep pumping AppKit requests while lifecycle cleanup runs off-thread.
        if self._shutdown_started_at is None:
            self._shutdown_started_at = time.monotonic()
        if AppKit.NSApp.modalWindow():
            AppKit.NSApp.stopModalWithCode_(0)
        if not self.daemon._stopped_event.is_set():
            if time.monotonic() - self._shutdown_started_at < 10.0:
                return
            self.daemon.failure = "Shutdown timed out; capture may still be active. Check OBS."
            logger.error(self.daemon.failure)
        if self._quit_service:
            if not self._unload_started:
                self._unload_started = True
                threading.Thread(target=self._unload_service, name="service-unload", daemon=True).start()
            if not self._unload_done.is_set():
                return
        AppKit.NSApp.stop_(None)

    def _unload_service(self):
        try:
            stop_launch_agent()
        except Exception:
            self.daemon.failure = "Could not unload LaunchAgent; service may restart. Check logs."
            logger.exception(self.daemon.failure)
        finally:
            self._unload_done.set()

    def _available_recording_choices(self):
        recording = self.daemon.recorder.is_recording
        transitioning = (
            self.daemon._manual_start_event.is_set()
            or self.daemon._manual_stop_event.is_set()
            or (self.daemon._active_session is not None and not recording)
        )
        choices = recording_menu_choices(
            recording, self.daemon.recorder.is_video_recording is True,
            transitioning, self.daemon._window_prompt_pending.is_set(),
        )
        if (recording and not transitioning and not self.daemon._stop_pending
                and self.daemon.config.retention_days != 0):
            choices += (("Delete Recording After Transcription", "delete_after_transcription"),)
        return choices, transitioning

    def _update_recording_menu(self):
        choices, transitioning = self._available_recording_choices()
        available = {choice for _, choice in choices}
        for choice, item in self.recording_items.items():
            item.setHidden_(choice not in available)
            if choice == "delete_after_transcription":
                item.setState_(bool(self.daemon._delete_after_transcription))
        self.quit_item.setHidden_(self.daemon.recorder.is_recording or transitioning)

    def perform_recording_action(self, choice: str, generation: int | None = None) -> None:
        choices, _ = self._available_recording_choices()
        if choice not in {action for _, action in choices}:
            return
        if choice == "stop_discard":
            self.daemon.request_discard_confirmation()
        elif choice == "delete_after_transcription":
            self.daemon.toggle_delete_after_transcription(
                self.daemon._window_session_generation if generation is None else generation
            )
        else:
            self.daemon._handle_control_choice(choice)

    def show_recording_menu(self):
        def show():
            self._update_recording_menu()
            self.item.button().performClick_(None)
        return self._on_main(show)

    def _on_main(self, func):
        if threading.current_thread() is threading.main_thread():
            return func()
        if self.daemon._shutdown_event.is_set():
            raise ApplicationStopping("Application is shutting down")
        result, done = [], threading.Event()
        self.requests.put((func, result, done))
        while not done.wait(0.1):
            if self.daemon._shutdown_event.is_set():
                raise ApplicationStopping("Application is shutting down")
        if isinstance(result[0], Exception):
            raise result[0]
        return result[0]

    @staticmethod
    def _alert(title, message, informative, buttons):
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(message)
        alert.setInformativeText_(informative)
        alert.setAlertStyle_(AppKit.NSAlertStyleInformational)
        for button in buttons:
            alert.addButtonWithTitle_(button)
        alert.window().setTitle_(title)
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        return alert.runModal() - AppKit.NSAlertFirstButtonReturn

    def _forget_diagnostic_window(self, window):
        for entry in list(self._diagnostic_windows):
            if entry[0] == window:
                self._diagnostic_windows.remove(entry)
                for tag in entry[2]:
                    self._diagnostic_actions.pop(tag, None)

    def _close_diagnostic_window(self, window):
        self._forget_diagnostic_window(window)
        window.close()

    def _position_diagnostic_window(self, window):
        screen = AppKit.NSScreen.mainScreen()
        if screen is None:
            return
        (sx, sy), (sw, sh) = screen.visibleFrame()
        (_, _), (width, height) = window.frame()
        # Keep diagnostic panels in the upper-right, away from centered consent.
        # Cascade multiple panels and clamp to the usable area (excluding Dock).
        offset = 32 * (len(self._diagnostic_windows) % 4)
        x = max(sx, sx + sw - width - 20 - offset)
        y = max(sy, sy + sh - height - 20 - offset)
        window.setFrameOrigin_((x, y))

    def _diagnostic_window(self, heading, detail, *, timeout=None):
        # Nonmodal: lifecycle polling and recording controls remain independent.
        window = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (590, 250)),
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable,
            AppKit.NSBackingStoreBuffered, False,
        )
        window.setReleasedWhenClosed_(False)
        window.setTitle_(heading)
        window.setDelegate_(self.target)
        self._position_diagnostic_window(window)
        label = AppKit.NSTextField.labelWithString_(detail)
        label.setFrame_(((20, 125), (550, 105)))
        label.setUsesSingleLineMode_(False)
        label.cell().setWraps_(True)
        window.contentView().addSubview_(label)
        self._diagnostic_windows.append(
            (window, time.monotonic() + timeout if timeout else None, [])
        )
        return window

    def _diagnostic_button(self, window, label, x, callback):
        self._diagnostic_tag += 1
        tag = self._diagnostic_tag
        self._diagnostic_actions[tag] = callback
        next(entry for entry in self._diagnostic_windows if entry[0] == window)[2].append(tag)
        button = AppKit.NSButton.alloc().initWithFrame_(((x, 20), (170, 32)))
        button.setTitle_(label)
        button.setBezelStyle_(AppKit.NSBezelStyleRounded)
        button.setTarget_(self.target)
        button.setAction_("diagnosticChoice:")
        button.setTag_(tag)
        window.contentView().addSubview_(button)

    def _tick_diagnostics(self):
        diagnostics = getattr(self.daemon, "diagnostics", None)
        if diagnostics is None:
            return
        stopping = self.daemon._shutdown_event.is_set()
        for window, deadline, _ in list(self._diagnostic_windows):
            if stopping or (deadline is not None and time.monotonic() >= deadline):
                self._close_diagnostic_window(window)
        if stopping or self._control_modal_active or AppKit.NSApp.modalWindow():
            return
        try:
            event = diagnostics.prompts.get_nowait()
        except queue.Empty:
            return
        detected = datetime.fromtimestamp(event["detected_at"]).strftime("%H:%M:%S")
        window = self._diagnostic_window(
            "Call detection diagnostics",
            f"Did a new {event['platform']} call start around {detected}?\n\n"
            "This only annotates the diagnostic log; recording is unchanged.\n"
            "Closes after 30 seconds. No response is assumed if unanswered.",
            timeout=30,
        )

        def answer(response):
            # Reject a queued click arriving after expiry, before the next tick.
            entry = next((e for e in self._diagnostic_windows if e[0] == window), None)
            if entry and time.monotonic() < entry[1]:
                diagnostics.answer(event, response)
            self._close_diagnostic_window(window)

        for index, (label, response) in enumerate((("Yes", "yes"), ("No", "no"),
                                                  ("Not sure", "not_sure"))):
            self._diagnostic_button(window, label, 20 + index * 190,
                                    lambda response=response: answer(response))
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        window.makeKeyAndOrderFront_(None)

    def show_diagnostic_marker(self, kind):
        diagnostics = self.daemon.diagnostics
        if diagnostics is None or self.daemon._shutdown_event.is_set():
            return
        window = self._diagnostic_window(
            "New call started" if kind == "start" else "Call ended",
            "When did it happen? Choose Just now, or enter a local 24-hour time.\n"
            "Use HH:MM or HH:MM:SS, within the past 60 minutes.\n"
            "Missed markers are OK. This does not change recording or detection.",
        )
        field = AppKit.NSTextField.alloc().initWithFrame_(((20, 90), (180, 26)))
        field.setStringValue_(datetime.now().strftime("%H:%M:%S"))
        window.contentView().addSubview_(field)
        error = AppKit.NSTextField.labelWithString_("")
        error.setFrame_(((20, 55), (550, 30)))
        window.contentView().addSubview_(error)

        def save(just_now):
            from .detection_diagnostics import resolve_event_time

            now = datetime.now().astimezone()
            try:
                occurred = now if just_now else resolve_event_time(str(field.stringValue()), now)
                diagnostics.marker(kind, occurred)
            except ValueError as exc:
                error.setStringValue_(str(exc))
                return
            self._close_diagnostic_window(window)

        self._diagnostic_button(window, "Just now", 20, lambda: save(True))
        self._diagnostic_button(window, "Save entered time", 210, lambda: save(False))
        self._diagnostic_button(window, "Cancel", 400, lambda: self._close_diagnostic_window(window))
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        window.makeKeyAndOrderFront_(None)

    def show_startup_prompt(self, meeting_title="Webex Session"):
        spec = startup_dialog(meeting_title)
        return self._on_main(lambda: self._control_window(spec, timeout=15))

    def show_manual_consent_prompt(self, meeting_title="Webex Session"):
        spec = manual_consent_dialog(meeting_title)
        return self._on_main(lambda: self._control_window(spec, timeout=10))

    def confirm_discard(self):
        return self._on_main(self._confirm_discard)

    @staticmethod
    def _confirm_discard():
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Stop and delete this recording?")
        alert.setInformativeText_(
            "This will permanently delete all recording segments from this session. "
            "No transcript will be created."
        )
        alert.setAlertStyle_(AppKit.NSAlertStyleWarning)
        no_button = alert.addButtonWithTitle_("No, keep recording")
        no_button.setKeyEquivalent_("\x1b")
        alert.window().setDefaultButtonCell_(no_button.cell())
        alert.addButtonWithTitle_("Yes, stop and delete")
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        return alert.runModal() == AppKit.NSAlertSecondButtonReturn

    def choose_webex_window(
        self, windows, suggested_window_id=None, displays=(), selected_display_uuid=None
    ):
        def show():
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("Select a video recording source")
            alert.setInformativeText_(
                ("A new Webex window appeared. " if suggested_window_id is not None else "")
                + "Choose a Webex window or an entire screen. Keep current leaves the video unchanged."
            )
            popup = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(
                ((0, 0), (440, 28)), False
            )
            choices = []
            for window in windows:
                size = f" — {window.width}×{window.height}" if window.area else ""
                popup.addItemWithTitle_(
                    f"{window.title}{size} ({window.owner})"
                )
                choices.append(window.window_id)
            for display in displays:
                popup.addItemWithTitle_(f"Entire screen — {display.label}")
                choices.append(display.display_uuid)
            for index, window in enumerate(windows):
                if window.window_id == suggested_window_id:
                    popup.selectItemAtIndex_(index)
                    break
            else:
                if selected_display_uuid is not None:
                    for index, choice in enumerate(choices):
                        if choice == selected_display_uuid:
                            popup.selectItemAtIndex_(index)
                            break
            alert.setAccessoryView_(popup)
            alert.addButtonWithTitle_("Record selected source")
            alert.addButtonWithTitle_("Keep current")
            AppKit.NSApp.activateIgnoringOtherApps_(True)
            if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
                return None
            index = popup.indexOfSelectedItem()
            return choices[index] if 0 <= index < len(choices) else None

        return self._on_main(show)

    def _control_window(self, spec, timeout=None):
        two_rows = len(spec.choices) > 3
        width = 570
        extra_height = 44 if two_rows else 0
        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (width, 270 + extra_height)), AppKit.NSWindowStyleMaskTitled,
            AppKit.NSBackingStoreBuffered, False,
        )
        window.setTitle_("Webex OBS Companion")
        window.setOpaque_(True)
        window.setBackgroundColor_(AppKit.NSColor.windowBackgroundColor())
        window.center()
        content = window.contentView()
        heading = AppKit.NSTextField.labelWithString_(spec.heading)
        heading.setFont_(AppKit.NSFont.boldSystemFontOfSize_(19))
        heading.setFrame_(((24, 217 + extra_height), (width - 50, 30)))
        content.addSubview_(heading)
        detail = AppKit.NSTextField.labelWithString_(spec.detail)
        detail.setFrame_(((24, 91 + extra_height), (width - 50, 115)))
        detail.setUsesSingleLineMode_(False)
        detail.cell().setWraps_(True)
        detail.cell().setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
        content.addSubview_(detail)
        for index, (label, choice) in enumerate(spec.choices):
            top_count = len(spec.choices) - 3 if two_rows else 0
            column = index if index < top_count else index - top_count
            y = 71 if index < top_count else 27
            button = AppKit.NSButton.alloc().initWithFrame_(
                ((24 + column * 180, y), (166, 34))
            )
            button.setTitle_(label)
            button.setBezelStyle_(AppKit.NSBezelStyleRounded)
            button.setTag_(index + 1)
            button.setTarget_(self.target)
            button.setAction_("controlChoice:")
            if choice in ("close", "cancel"):
                button.setKeyEquivalent_("\x1b")
            if choice == spec.default_choice:
                button.setKeyEquivalent_("\r")
            content.addSubview_(button)
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        window.makeKeyAndOrderFront_(None)
        timer = None
        if timeout:
            timer = Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                timeout, self.target, "controlTimeout:", window, False
            )
            # Scheduled timers use the default mode; explicitly include the
            # modal-panel mode so the notice really expires during runModal.
            Foundation.NSRunLoop.currentRunLoop().addTimer_forMode_(
                timer, AppKit.NSModalPanelRunLoopMode
            )
        try:
            self._control_modal_active = True
            selected = AppKit.NSApp.runModalForWindow_(window) - 1
        finally:
            self._control_modal_active = False
            if timer:
                timer.invalidate()
            window.orderOut_(None)
        return spec.choices[selected][1] if 0 <= selected < len(spec.choices) else "close"

    def show_notification(self, title, message):
        UIBanner.show_notification(title, message)

    def _select_settings_category(self, index):
        for position, pane in enumerate(self._settings_panes):
            pane.setHidden_(position != index)

    @staticmethod
    def _settings_sections():
        return [
            ("OBS recording", [
                ("WebSocket host", "obs_ws_host", "text"),
                ("WebSocket port", "obs_ws_port", "text"),
                ("WebSocket password", "obs_ws_password", "secret"),
                ("Exit OBS on recording stop", "exit_obs_on_recording_stop", "bool"),
                ("Idle restart (min; 0 = off)", "obs_idle_restart_minutes", "text"),
                ("New Webex window (video only)", "shared_window_behavior", "window_behavior"),
            ]),
            ("Transcription", [
                ("Whisper model", "whisper_model", "text"),
                ("Enable diarization", "enable_diarization", "bool"),
                ("Hugging Face token", "hf_token", "secret"),
            ]),
            ("Files and detection", [
                ("Recordings folder", "recordings_dir", "text"),
                ("Recording retention (days)", "retention_days", "text"),
                ("Transcripts folder", "transcripts_dir", "text"),
                ("Poll interval (seconds)", "poll_interval", "text"),
                ("Call-end grace (seconds)", "call_end_grace_seconds", "text"),
            ]),
            ("Keyboard shortcuts", [
                ("Start audio recording", "hotkey_audio", "text"),
                ("Start / switch to video", "hotkey_video", "text"),
                ("Select video source", "hotkey_video_source", "text"),
                ("Stop recording", "hotkey_stop_transcribe", "text"),
            ]),
            ("Webex delivery", [
                ("Deliver transcripts to Webex", "webex_delivery_enabled", "bool"),
                ("Bot access token", "webex_access_token", "secret"),
                ("Recipient email", "webex_recipient_email", "text"),
                ("My Agent email", "my_agent_email", "text"),
                ("Space / room ID", "webex_room_id", "text"),
            ]),
        ]

    def show_settings(self):
        if self._settings_saving:
            return
        if self._settings_window is not None:
            AppKit.NSApp.activateIgnoringOtherApps_(True)
            if self._settings_window.isMiniaturized():
                self._settings_window.deminiaturize_(None)
            self._settings_window.makeKeyAndOrderFront_(None)
            return
        config = self.daemon.config
        sections = self._settings_sections()
        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (800, 490)),
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
            | AppKit.NSWindowStyleMaskMiniaturizable,
            AppKit.NSBackingStoreBuffered, False,
        )
        window.setTitle_("Webex OBS Companion Settings")
        window.setReleasedWhenClosed_(False)
        window.setDelegate_(self.target)
        window.setLevel_(AppKit.NSNormalWindowLevel)
        window.setOpaque_(True)
        window.setAlphaValue_(1.0)
        window.setBackgroundColor_(AppKit.NSColor.windowBackgroundColor())
        window.center()
        content = window.contentView()

        sidebar = AppKit.NSScrollView.alloc().initWithFrame_(((0, 67), (205, 423)))
        sidebar.setHasVerticalScroller_(False)
        sidebar.setDrawsBackground_(True)
        sidebar.setBackgroundColor_(AppKit.NSColor.windowBackgroundColor())
        table = AppKit.NSTableView.alloc().initWithFrame_(((0, 0), (205, 423)))
        column = AppKit.NSTableColumn.alloc().initWithIdentifier_("settings-category")
        column.setWidth_(200)
        table.addTableColumn_(column)
        table.setHeaderView_(None)
        table.setRowHeight_(42)
        table.setAllowsEmptySelection_(False)
        table.setSelectionHighlightStyle_(AppKit.NSTableViewSelectionHighlightStyleRegular)
        table.setBackgroundColor_(AppKit.NSColor.windowBackgroundColor())
        self._sidebar_source = _SettingsSidebar.alloc().init()
        self._sidebar_source.configure(self, [heading for heading, _ in sections])
        table.setDataSource_(self._sidebar_source)
        table.setDelegate_(self._sidebar_source)
        sidebar.setDocumentView_(table)
        content.addSubview_(sidebar)
        divider = AppKit.NSBox.alloc().initWithFrame_(((205, 67), (1, 423)))
        divider.setBoxType_(AppKit.NSBoxSeparator)
        content.addSubview_(divider)

        self._settings_panes = []
        inputs = {}
        for heading, entries in sections:
            pane = AppKit.NSView.alloc().initWithFrame_(((220, 67), (565, 423)))
            content.addSubview_(pane)
            self._settings_panes.append(pane)
            title = AppKit.NSTextField.labelWithString_(heading)
            title.setFont_(AppKit.NSFont.boldSystemFontOfSize_(17))
            title.setFrame_(((15, 365), (530, 30)))
            pane.addSubview_(title)
            for row, (label, key, kind) in enumerate(entries):
                y = 316 - row * 55
                if kind == "bool":
                    field = AppKit.NSButton.alloc().initWithFrame_(((15, y), (530, 28)))
                    field.setButtonType_(AppKit.NSButtonTypeSwitch)
                    field.setTitle_(label)
                    field.setState_(bool(getattr(config, key)))
                elif kind == "window_behavior":
                    caption = AppKit.NSTextField.labelWithString_(label)
                    caption.setFrame_(((15, y), (230, 24)))
                    pane.addSubview_(caption)
                    field = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(
                        ((250, y), (295, 28)), False
                    )
                    field.addItemsWithTitles_(["Prompt me (recommended)", "Always switch"])
                    field.selectItemAtIndex_(0 if getattr(config, key) == "prompt" else 1)
                else:
                    caption = AppKit.NSTextField.labelWithString_(label)
                    caption.setFrame_(((15, y), (190, 24)))
                    pane.addSubview_(caption)
                    cls = AppKit.NSSecureTextField if kind == "secret" else AppKit.NSTextField
                    field = cls.alloc().initWithFrame_(((215, y), (330, 24)))
                    value = getattr(config, key)
                    field.setStringValue_("" if value is None else str(value))
                pane.addSubview_(field)
                if key == "retention_days":
                    note = AppKit.NSTextField.labelWithString_(
                        "0 = Do not retain recordings."
                    )
                    note.setFont_(AppKit.NSFont.systemFontOfSize_(11))
                    note.setFrame_(((15, y - 20), (530, 18)))
                    pane.addSubview_(note)
                inputs[key] = (field, kind)

        table.reloadData()
        table.selectRowIndexes_byExtendingSelection_(Foundation.NSIndexSet.indexSetWithIndex_(0), False)
        self._select_settings_category(0)
        footer = AppKit.NSTextField.labelWithString_(
            "OBS connection and recording folder changes apply to the next session."
        )
        footer.setFrame_(((20, 39), (550, 20)))
        content.addSubview_(footer)
        for title, action, x in (("Cancel", "cancelSettings:", 613), ("OK", "saveSettings:", 705)):
            button = AppKit.NSButton.alloc().initWithFrame_(((x, 20), (78, 32)))
            button.setTitle_(title)
            button.setBezelStyle_(AppKit.NSBezelStyleRounded)
            button.setTarget_(self.target)
            button.setAction_(action)
            button.setKeyEquivalent_("\x1b" if title == "Cancel" else "\r")
            content.addSubview_(button)
        # Retain controls and their original values until an explicit action;
        # a normal window returns immediately to the AppKit main run loop.
        self._settings_window = window
        self._settings_config = config
        self._settings_inputs = inputs
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        window.makeKeyAndOrderFront_(None)

    def _clear_settings(self):
        self._settings_window = None
        self._settings_config = None
        self._settings_inputs = {}
        self._settings_panes = []
        self._sidebar_source = None

    def close_settings(self):
        """Cancel/close on AppKit without affecting any recording modal dialog."""
        window = self._settings_window
        if window is not None:
            window.setDelegate_(None)
            window.close()
        self._clear_settings()

    def save_settings(self):
        if self._settings_window is None or self._settings_saving:
            return
        config = self._settings_config
        values = {}
        for key, (field, kind) in self._settings_inputs.items():
            if kind == "bool":
                value = bool(field.state())
            elif kind == "window_behavior":
                value = "prompt" if field.indexOfSelectedItem() == 0 else "always_switch"
            else:
                value = str(field.stringValue())
            original = getattr(config, key)
            if value != (bool(original) if kind == "bool" else ("" if original is None else str(original))):
                values[key] = value
        self.close_settings()
        if not values:
            return
        self._save_settings_async(values)

    def _save_settings_async(self, values):
        """Only collect controls on AppKit; persistence and integrations can block."""
        if self._settings_saving:
            return
        self._settings_saving = True

        def save():
            error = None
            try:
                self.daemon.save_settings(values, Path(DEFAULT_ENV_FILE))
            except Exception as exc:
                logger.error("Settings save/application failed (%s).", type(exc).__name__)
                error = (
                    "Settings rollback failed. Finish any recording and restart the app."
                    if isinstance(exc, SettingsRollbackError) else
                    "Settings could not be saved or applied. Previous settings were retained."
                )

            def finish():
                self._settings_saving = False
                if error:
                    self._alert("Settings", "Could not save settings", error, ["OK"])

            self.requests.put((finish, [], threading.Event()))

        threading.Thread(target=save, name="settings-apply", daemon=True).start()
