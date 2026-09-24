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
        from .config import Config
        config = Config(_env_file=DEFAULT_ENV_FILE)
        fields = [
            ("OBS host", "obs_ws_host"), ("OBS port", "obs_ws_port"),
            ("Recordings folder", "recordings_dir"), ("Transcripts folder", "transcripts_dir"),
            ("Retention (days)", "retention_days"),
            ("Call-end grace (seconds)", "call_end_grace_seconds"),
            ("Video shortcut", "hotkey_video"), ("Controls shortcut", "hotkey_menu"),
            ("Stop shortcut", "hotkey_stop_transcribe"),
        ]
        view = AppKit.NSView.alloc().initWithFrame_(((0, 0), (500, 355)))
        inputs = {}
        for index, (label, key) in enumerate(fields):
            y = 327 - index * 32
            caption = AppKit.NSTextField.labelWithString_(label)
            caption.setFrame_(((0, y), (178, 24)))
            view.addSubview_(caption)
            field = AppKit.NSTextField.alloc().initWithFrame_(((180, y), (318, 24)))
            field.setStringValue_(str(getattr(config, key)))
            view.addSubview_(field)
            inputs[key] = field
        checks = {}
        for index, (label, key) in enumerate([
            ("Restart OBS for each call", "relaunch_obs_per_call"),
            ("Enable diarization", "enable_diarization"),
        ]):
            checkbox = AppKit.NSButton.alloc().initWithFrame_(((180, 37 - index * 29), (310, 24)))
            checkbox.setButtonType_(AppKit.NSButtonTypeSwitch)
            checkbox.setTitle_(label)
            checkbox.setState_(bool(getattr(config, key)))
            view.addSubview_(checkbox)
            checks[key] = checkbox
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Settings")
        alert.setInformativeText_("Changes take effect after restarting the service. OBS password and delivery credentials remain in the setup wizard.")
        alert.setAccessoryView_(view)
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Cancel")
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
            return
        values = {key: field.stringValue() for key, field in inputs.items()}
        values.update({key: bool(field.state()) for key, field in checks.items()})
        try:
            save_settings(values, Path(DEFAULT_ENV_FILE))
        except Exception as exc:
            self._alert("Settings", "Could not save settings", str(exc), ["OK"])
        else:
            self._alert("Settings", "Settings saved", "Restart the background service with uv run webex-obs start to apply them.", ["OK"])


_TIMEOUT_TARGET = _MenuTarget.alloc().init()
