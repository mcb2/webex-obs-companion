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
from .settings_store import save_settings
from .ui_banner import UIBanner

class _MenuTarget(Foundation.NSObject):
    @objc.python_method
    def configure(self, ui):
        self.ui = ui

    def controls_(self, sender):
        threading.Thread(target=self.ui.daemon._handle_dialog_request, daemon=True).start()

    def video_(self, sender):
        threading.Thread(target=self.ui.daemon.recorder.switch_to_video_mode, daemon=True).start()

    def stop_(self, sender):
        self.ui.daemon._handle_stop_transcribe_request()

    def settings_(self, sender):
        self.ui.show_settings()

    def quit_(self, sender):
        if not self.ui.daemon.recorder.is_recording:
            AppKit.NSApp.terminate_(None)

    def tick_(self, timer):
        self.ui._tick()

    def timeout_(self, timer):
        AppKit.NSApp.stopModalWithCode_(AppKit.NSAlertFirstButtonReturn)


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
        self.status_item = self._add(menu, "Ready for calls", None)
        self.status_item.setEnabled_(False)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self.controls_item = self._add(menu, "Recording controls…", "controls:")
        self.video_item = self._add(menu, "Switch to video", "video:")
        self.stop_item = self._add(menu, "Stop & transcribe", "stop:")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._add(menu, "Settings…", "settings:")
        self.quit_item = self._add(menu, "Quit", "quit:")
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
        self.status_item.setTitle_(("Recording: " if recording else "Idle: ") + title)
        self.video_item.setEnabled_(recording)
        self.stop_item.setEnabled_(recording)
        self.quit_item.setEnabled_(not recording)
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
    def _alert(title, message, informative, buttons, timeout=None):
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(message)
        alert.setInformativeText_(informative)
        alert.setAlertStyle_(AppKit.NSAlertStyleInformational)
        for button in buttons:
            alert.addButtonWithTitle_(button)
        alert.window().setTitle_(title)
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        timer = None
        if timeout:
            # A modal alert spins a nested main run loop, so the timer still fires.
            timer = Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                timeout, _TIMEOUT_TARGET, "timeout:", None, False
            )
        try:
            return alert.runModal() - AppKit.NSAlertFirstButtonReturn
        finally:
            if timer:
                timer.invalidate()

    def show_startup_prompt(self, meeting_title="Webex Session"):
        index = self._on_main(lambda: self._alert(
            "Webex OBS Companion", "Recording started", meeting_title + "\n\n"
            "Recording laws vary by location. Obtain permission from all participants "
            "when required by applicable law.\n\nAudio continues automatically after 15 seconds.",
            ["Keep Audio", "Switch to Video", "Cancel & Discard"], timeout=15,
        ))
        return {0: "keep_audio", 1: "switch_video", 2: "cancel"}.get(index, "keep_audio")

    def show_control_prompt(self, is_recording=True, meeting_title="Webex Session"):
        buttons = (["Stop & Transcribe", "Switch to Video", "Cancel & Discard"]
                   if is_recording else ["Start Audio Rec", "Start Video Rec", "Close Menu"])
        index = self._on_main(lambda: self._alert(
            "Webex OBS Companion", "Recording controls" if is_recording else "Start recording",
            meeting_title, buttons, timeout=25,
        ))
        choices = (["stop_transcribe", "switch_video", "cancel"] if is_recording
                   else ["start_audio", "start_video", "close"])
        return choices[index] if 0 <= index < 3 else "close"

    def show_notification(self, title, message):
        UIBanner.show_notification(title, message)

    def show_settings(self):
        config = self.daemon.config
        sections = [
            ("Webex delivery", [
                ("Bot access token", "webex_access_token", "secret"),
                ("Recipient email", "webex_recipient_email", "text"),
                ("My Agent email", "my_agent_email", "text"),
                ("Space / room ID", "webex_room_id", "text"),
            ]),
            ("OBS recording", [
                ("WebSocket host", "obs_ws_host", "text"),
                ("WebSocket port", "obs_ws_port", "text"),
                ("WebSocket password", "obs_ws_password", "secret"),
                ("Restart OBS for each call", "relaunch_obs_per_call", "bool"),
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
        rows = sum(len(entries) + 1 for _, entries in sections)
        height = rows * 32 + 20
        view = AppKit.NSView.alloc().initWithFrame_(((0, 0), (570, height)))
        inputs = {}
        index = 0
        for heading, entries in sections:
            y = height - 30 - index * 32
            section_label = AppKit.NSTextField.labelWithString_(heading)
            section_label.setFrame_(((5, y), (550, 24)))
            view.addSubview_(section_label)
            index += 1
            for label, key, kind in entries:
                y = height - 30 - index * 32
                caption = AppKit.NSTextField.labelWithString_(label)
                caption.setFrame_(((10, y), (190, 24)))
                view.addSubview_(caption)
                if kind == "bool":
                    field = AppKit.NSButton.alloc().initWithFrame_(((205, y), (24, 24)))
                    field.setButtonType_(AppKit.NSButtonTypeSwitch)
                    field.setState_(bool(getattr(config, key)))
                else:
                    cls = AppKit.NSSecureTextField if kind == "secret" else AppKit.NSTextField
                    field = cls.alloc().initWithFrame_(((205, y), (350, 24)))
                    field.setStringValue_(str(getattr(config, key) or ""))
                view.addSubview_(field)
                inputs[key] = (field, kind)
                index += 1
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(((0, 0), (590, 430)))
        scroll.setHasVerticalScroller_(True)
        scroll.setDocumentView_(view)
        scroll.contentView().scrollToPoint_((0, height - 430))
        scroll.reflectScrolledClipView_(scroll.contentView())
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Settings")
        alert.setInformativeText_("Changes apply on OK. OBS connection changes wait for a recording to finish. Set the recording output folder in OBS as well.")
        alert.setAccessoryView_(scroll)
        alert.addButtonWithTitle_("OK")
        alert.addButtonWithTitle_("Cancel")
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
            return
        values = {}
        for key, (field, kind) in inputs.items():
            value = bool(field.state()) if kind == "bool" else str(field.stringValue())
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


_TIMEOUT_TARGET = _MenuTarget.alloc().init()
