"""AppKit user interface. Import only when running in a macOS user session.

AppKit owns the main thread; the meeting lifecycle runs in a background thread.
Requests from that thread are handed to the main run loop through a short timer.
"""

import queue
import threading
from pathlib import Path

import AppKit
import Foundation
import objc

from .config import DEFAULT_ENV_FILE
from .control_dialog import manual_consent_dialog, recording_menu_choices, startup_dialog
from .settings_store import save_settings
from .service_control import stop_launch_agent
from .ui_banner import UIBanner

class _MenuTarget(Foundation.NSObject):
    @objc.python_method
    def configure(self, ui):
        self.ui = ui

    def recordingAction_(self, sender):
        threading.Thread(
            target=self.ui.perform_recording_action,
            args=(str(sender.representedObject()),), name="recording-menu-action", daemon=True,
        ).start()

    def menuNeedsUpdate_(self, menu):
        self.ui._update_recording_menu()

    def settings_(self, sender):
        self.ui.show_settings()

    def saveSettings_(self, sender):
        AppKit.NSApp.stopModalWithCode_(1)

    def cancelSettings_(self, sender):
        AppKit.NSApp.stopModalWithCode_(0)

    def controlChoice_(self, sender):
        AppKit.NSApp.stopModalWithCode_(sender.tag())

    def quit_(self, sender):
        if self.ui.daemon.recorder.is_recording:
            return
        try:
            stop_launch_agent()
        except Exception as exc:
            self.ui._alert("Webex OBS Companion", "Could not quit", str(exc), ["OK"])
        else:
            AppKit.NSApp.terminate_(None)

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
                              + (("Select Video Source…", "select_window"),)):
            item = self._add(menu, label, "recordingAction:")
            item.setRepresentedObject_(choice)
            self.recording_items[choice] = item
        self._add(menu, "Settings…", "settings:")
        self.quit_item = self._add(menu, "Quit (stop service)", "quit:")
        self._update_recording_menu()
        self.item.setMenu_(menu)
        self.timer = Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.15, self.target, "tick:", None, True
        )
        threading.Thread(target=self.daemon.start, name="meeting-lifecycle", daemon=True).start()
        app.run()

    def _add(self, menu, title, selector):
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, selector, "")
        if selector:
            item.setTarget_(self.target)
        menu.addItem_(item)
        return item

    def _tick(self):
        try:
            # Avoid nested prompts stealing the consent notice's modal timeout.
            while not self._control_modal_active:
                func, result, done = self.requests.get_nowait()
                try:
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
            mode = "Recording video" if video else "Recording audio"
            self.item.button().setToolTip_(mode + " • " + title if recording else "Ready for calls")

    def _available_recording_choices(self):
        recording = self.daemon.recorder.is_recording
        transitioning = (
            self.daemon._manual_start_event.is_set()
            or self.daemon._manual_stop_event.is_set()
            or (self.daemon._active_session is not None and not recording)
        )
        return recording_menu_choices(
            recording, self.daemon.recorder.is_video_recording is True,
            transitioning, self.daemon._window_prompt_pending.is_set(),
        ), transitioning

    def _update_recording_menu(self):
        choices, transitioning = self._available_recording_choices()
        available = {choice for _, choice in choices}
        for choice, item in self.recording_items.items():
            item.setHidden_(choice not in available)
        self.quit_item.setHidden_(self.daemon.recorder.is_recording or transitioning)

    def perform_recording_action(self, choice):
        choices, _ = self._available_recording_choices()
        if choice not in {action for _, action in choices}:
            return
        if choice == "stop_discard":
            self.daemon.request_discard_confirmation()
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
        result, done = [], threading.Event()
        self.requests.put((func, result, done))
        done.wait()
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
                ("Transcripts folder", "transcripts_dir", "text"),
                ("Retention (days)", "retention_days", "text"),
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
        config = self.daemon.config
        sections = self._settings_sections()
        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (800, 490)), AppKit.NSWindowStyleMaskTitled,
            AppKit.NSBackingStoreBuffered, False,
        )
        window.setTitle_("Webex OBS Companion Settings")
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
                inputs[key] = (field, kind)

        table.reloadData()
        table.selectRowIndexes_byExtendingSelection_(Foundation.NSIndexSet.indexSetWithIndex_(0), False)
        self._select_settings_category(0)
        footer = AppKit.NSTextField.labelWithString_(
            "Changes apply on OK. OBS connection changes wait for a recording to finish."
        )
        footer.setFrame_(((20, 39), (550, 20)))
        content.addSubview_(footer)
        for title, action, x in (("Cancel", "cancelSettings:", 613), ("OK", "saveSettings:", 705)):
            button = AppKit.NSButton.alloc().initWithFrame_(((x, 20), (78, 32)))
            button.setTitle_(title)
            button.setBezelStyle_(AppKit.NSBezelStyleRounded)
            button.setTarget_(self.target)
            button.setAction_(action)
            content.addSubview_(button)
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        window.makeKeyAndOrderFront_(None)
        try:
            accepted = AppKit.NSApp.runModalForWindow_(window) == 1
        finally:
            window.orderOut_(None)
            self._settings_panes = []
            self._sidebar_source = None
        if not accepted:
            return
        values = {}
        for key, (field, kind) in inputs.items():
            if kind == "bool":
                value = bool(field.state())
            elif kind == "window_behavior":
                value = "prompt" if field.indexOfSelectedItem() == 0 else "always_switch"
            else:
                value = str(field.stringValue())
            original = getattr(config, key)
            if value != (bool(original) if kind == "bool" else str(original or "")):
                values[key] = value
        if not values:
            return
        try:
            updated = save_settings(values, Path(DEFAULT_ENV_FILE), current=config)
            try:
                self.daemon.apply_settings(updated)
            except Exception:
                save_settings({key: getattr(config, key) for key in values}, Path(DEFAULT_ENV_FILE), current=updated)
                raise
        except Exception as exc:
            self._alert("Settings", "Could not save settings", str(exc), ["OK"])
