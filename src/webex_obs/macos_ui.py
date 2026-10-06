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
from .control_dialog import control_dialog, startup_dialog
from .settings_store import save_settings
from .service_control import stop_launch_agent
from .ui_banner import UIBanner

class _MenuTarget(Foundation.NSObject):
    @objc.python_method
    def configure(self, ui):
        self.ui = ui

    def controls_(self, sender):
        is_recording, title = self.ui.daemon.control_prompt_state()
        choice = self.ui.show_control_prompt(is_recording=is_recording, meeting_title=title)
        if choice != "close":
            threading.Thread(
                target=self.ui.daemon._handle_control_choice, args=(choice,), daemon=True
            ).start()

    def stop_(self, sender):
        self.ui.daemon._handle_stop_transcribe_request()

    def selectWindow_(self, sender):
        threading.Thread(
            target=self.ui.daemon.request_window_selection, name="select-webex-window", daemon=True
        ).start()

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

    def run(self):
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        self.target = _MenuTarget.alloc().init()
        self.target.configure(self)
        self.item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_("record.circle", "Meeting recorder")
        if image is not None:
            image.setTemplate_(True)
            self.item.button().setImage_(image)
        else:
            self.item.button().setTitle_("●")
        menu = AppKit.NSMenu.alloc().init()
        self.controls_item = self._add(menu, "Recording controls…", "controls:")
        self.select_window_item = self._add(menu, "Select Webex recording window…", "selectWindow:")
        self.stop_item = self._add(menu, "Stop & Transcribe", "stop:")
        self._add(menu, "Settings…", "settings:")
        self.quit_item = self._add(menu, "Quit (stop service)", "quit:")
        self.stop_item.setHidden_(True)
        self.select_window_item.setHidden_(True)
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
            while True:
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
        self.stop_item.setHidden_(not recording)
        self.select_window_item.setHidden_(self.daemon.recorder.is_video_recording is not True)
        self.quit_item.setHidden_(recording)
        if self.item.button():
            self.item.button().setToolTip_("Recording • " + title if recording else "Ready for calls")

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

    def show_control_prompt(self, is_recording=True, meeting_title="Webex Session"):
        spec = control_dialog(is_recording, meeting_title)
        return self._on_main(lambda: self._control_window(spec))

    def choose_webex_window(self, windows, suggested_window_id=None):
        def show():
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("Select a Webex recording window")
            alert.setInformativeText_(
                ("A new Webex window appeared. " if suggested_window_id is not None else "")
                + "Choose the window to record. Keep current leaves the video unchanged."
            )
            popup = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(
                ((0, 0), (440, 28)), False
            )
            for window in windows:
                size = f" — {window.width}×{window.height}" if window.area else ""
                popup.addItemWithTitle_(
                    f"{window.title}{size} ({window.owner})"
                )
            for index, window in enumerate(windows):
                if window.window_id == suggested_window_id:
                    popup.selectItemAtIndex_(index)
                    break
            alert.setAccessoryView_(popup)
            alert.addButtonWithTitle_("Record selected window")
            alert.addButtonWithTitle_("Keep current")
            AppKit.NSApp.activateIgnoringOtherApps_(True)
            if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
                return None
            index = popup.indexOfSelectedItem()
            return windows[index].window_id if 0 <= index < len(windows) else None

        return self._on_main(show)

    def _control_window(self, spec, timeout=None):
        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (570, 270)), AppKit.NSWindowStyleMaskTitled,
            AppKit.NSBackingStoreBuffered, False,
        )
        window.setTitle_("Webex OBS Companion")
        window.setOpaque_(True)
        window.setBackgroundColor_(AppKit.NSColor.windowBackgroundColor())
        window.center()
        content = window.contentView()
        heading = AppKit.NSTextField.labelWithString_(spec.heading)
        heading.setFont_(AppKit.NSFont.boldSystemFontOfSize_(19))
        heading.setFrame_(((24, 217), (520, 30)))
        content.addSubview_(heading)
        detail = AppKit.NSTextField.labelWithString_(spec.detail)
        detail.setFrame_(((24, 91), (520, 115)))
        detail.setUsesSingleLineMode_(False)
        detail.cell().setWraps_(True)
        detail.cell().setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
        content.addSubview_(detail)
        for index, (label, choice) in enumerate(spec.choices):
            button = AppKit.NSButton.alloc().initWithFrame_(
                ((24 + index * 180, 27), (166, 34))
            )
            button.setTitle_(label)
            button.setBezelStyle_(AppKit.NSBezelStyleRounded)
            button.setTag_(index + 1)
            button.setTarget_(self.target)
            button.setAction_("controlChoice:")
            if choice in ("close", "cancel"):
                button.setKeyEquivalent_("\x1b")
            content.addSubview_(button)
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        window.makeKeyAndOrderFront_(None)
        timer = None
        if timeout:
            timer = Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                timeout, self.target, "controlTimeout:", None, False
            )
        try:
            selected = AppKit.NSApp.runModalForWindow_(window) - 1
        finally:
            if timer:
                timer.invalidate()
            window.orderOut_(None)
        return spec.choices[selected][1] if 0 <= selected < len(spec.choices) else "close"

    def show_notification(self, title, message):
        UIBanner.show_notification(title, message)

    def _select_settings_category(self, index):
        for position, pane in enumerate(self._settings_panes):
            pane.setHidden_(position != index)

    def show_settings(self):
        config = self.daemon.config
        sections = [
            ("Webex delivery", [
                ("Deliver transcripts to Webex", "webex_delivery_enabled", "bool"),
                ("Bot access token", "webex_access_token", "secret"),
                ("Recipient email", "webex_recipient_email", "text"),
                ("My Agent email", "my_agent_email", "text"),
                ("Space / room ID", "webex_room_id", "text"),
            ]),
            ("OBS recording", [
                ("WebSocket host", "obs_ws_host", "text"),
                ("WebSocket port", "obs_ws_port", "text"),
                ("WebSocket password", "obs_ws_password", "secret"),
                ("Exit OBS on recording stop", "exit_obs_on_recording_stop", "bool"),
                ("New Webex window", "shared_window_behavior", "window_behavior"),
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
                ("Video shortcut", "hotkey_video", "text"),
                ("Controls shortcut", "hotkey_menu", "text"),
                ("Stop shortcut", "hotkey_stop_transcribe", "text"),
            ]),
        ]
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
                    caption.setFrame_(((15, y), (190, 24)))
                    pane.addSubview_(caption)
                    field = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(
                        ((215, y), (330, 28)), False
                    )
                    field.addItemsWithTitles_(["Prompt me", "Always switch"])
                    field.selectItemAtIndex_(0 if getattr(config, key) == "prompt" else 1)
                else:
                    caption = AppKit.NSTextField.labelWithString_(label)
                    caption.setFrame_(((15, y), (190, 24)))
                    pane.addSubview_(caption)
                    cls = AppKit.NSSecureTextField if kind == "secret" else AppKit.NSTextField
                    field = cls.alloc().initWithFrame_(((215, y), (330, 24)))
                    field.setStringValue_(str(getattr(config, key) or ""))
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
