from __future__ import annotations

import json
import logging
import os
import pickle
import platform
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from audio_recorder import AudioRecorder
from caption_scraper import CaptionScraper
from config import SETTINGS, Settings
from email_delivery import send_summary_email_for_meeting
from meeting_intelligence import complete_llm_json, create_meeting_intelligence_provider, render_intelligence_markdown
from storage import MeetingStorage


LOGGER = logging.getLogger(__name__)
MEET_URL_RE = re.compile(r"https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}")
JOIN_DIALOG_LLM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": ["allow", "deny", "unknown"]},
        "button_label": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["decision", "button_label", "confidence", "reason"],
}
JOIN_DIALOG_LLM_SYSTEM_PROMPT = """You classify visible Google Meet modal dialogs for an unattended meeting capture bot.
Return JSON only. Decide "allow" only when the dialog is a benign consent/continue prompt needed to enter or continue a meeting, especially prompts about sharing call audio/video with a meeting assistant or plugin.
Return "deny" for destructive, security, sign-in, payment, permission escalation, leave, cancel, or stop-recording prompts.
Return "unknown" whenever unsure. The button_label must exactly match one visible button label or be null."""
KNOWN_SAFE_DIALOG_PATTERNS = [
    re.compile(r"\b(?:your\s+)?call audio (?:and|&)\s+video will be shared with\b", re.I),
    re.compile(r"\baudio (?:and|&)\s+video will be shared with\b", re.I),
]
AFFIRMATIVE_DIALOG_LABELS = {
    "accept",
    "agree",
    "allow",
    "continue",
    "got it",
    "i agree",
    "join now",
    "ok",
    "okay",
    "yes",
}
NEGATIVE_DIALOG_LABELS = {
    "cancel",
    "close",
    "deny",
    "dismiss",
    "leave",
    "no",
    "not now",
    "stop",
}
MIN_DIALOG_LLM_CONFIDENCE = 0.80
MAX_DIALOG_TEXT_CHARS = 1200


class PulseAudioSink:
    def __init__(self, sink_name: str):
        self.sink_name = sink_name
        self.module_id: str | None = None
        self._previous_env: dict[str, str | None] = {}

    def setup(self) -> None:
        # Chrome binds to the default PulseAudio sink at launch, so this runs before webdriver starts.
        load = subprocess.run(
            [
                "pactl",
                "load-module",
                "module-null-sink",
                f"sink_name={self.sink_name}",
                f"sink_properties=device.description={self.sink_name}",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.module_id = load.stdout.strip()
        subprocess.run(["pactl", "set-default-sink", self.sink_name], check=True)
        subprocess.run(["pactl", "set-default-source", f"{self.sink_name}.monitor"], check=True)
        self._set_audio_env()

    def _set_audio_env(self) -> None:
        # ALSA's pulse plugin and Chrome both honor these variables. Setting them here keeps
        # browser playback and the sounddevice capture stream share the same per-meeting PulseAudio sink.
        updates = {
            "PULSE_SINK": self.sink_name,
            "PULSE_SOURCE": f"{self.sink_name}.monitor",
        }
        for key, value in updates.items():
            self._previous_env.setdefault(key, os.environ.get(key))
            os.environ[key] = value

    def diagnostics(self) -> str:
        commands = [
            ["pactl", "get-default-sink"],
            ["pactl", "get-default-source"],
            ["pactl", "list", "short", "sinks"],
            ["pactl", "list", "short", "sources"],
        ]
        output: list[str] = []
        for command in commands:
            try:
                result = subprocess.run(
                    command,
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                output.append(f"$ {' '.join(command)}\n{result.stdout.strip()}")
            except Exception as exc:
                output.append(f"$ {' '.join(command)}\nfailed: {exc}")
        return "\n".join(output)

    def cleanup(self) -> None:
        if self.module_id:
            subprocess.run(["pactl", "unload-module", self.module_id], check=False)
        for key, value in self._previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class MeetBot:
    def __init__(self, settings: Settings = SETTINGS):
        self.settings = settings
        self.storage = MeetingStorage(settings.meetings_output_dir)
        self.driver = None
        self.display = None
        self.pulse_sink: PulseAudioSink | None = None
        self._join_blocker: dict[str, Any] | None = None

    def run(self, meeting: Any) -> dict[str, Any]:
        meeting_id = str(uuid4())
        sink_name = f"MeetBot_{meeting_id.replace('-', '')[:12]}"
        meeting_dir = self.storage.create_meeting_dir(meeting.meeting_title, meeting.start_time)
        logger = self.storage.setup_meeting_logger(meeting_dir, f"meet_bot.{meeting_id}")
        metadata = self.storage.initial_metadata(meeting, meeting_id=meeting_id)
        self.storage.write_metadata(meeting_dir, metadata)
        stop_event = threading.Event()
        audio = self._create_audio_recorder(sink_name)
        captions = CaptionScraper()
        audio_thread = None
        caption_thread = None
        audio_enabled = self._audio_capture_available(audio, logger)
        joined_at: datetime | None = None
        status = "failed"
        self._join_blocker = None

        try:
            logger.info(
                "Meeting bot run started: title=%s url=%s start_time=%s end_time=%s",
                getattr(meeting, "meeting_title", getattr(meeting, "title", "")),
                getattr(meeting, "meet_url", ""),
                getattr(meeting, "start_time", None),
                getattr(meeting, "end_time", None),
            )
            self.pulse_sink = PulseAudioSink(sink_name)
            try:
                self.pulse_sink.setup()
                logger.info("PulseAudio configured for meeting audio:\n%s", self.pulse_sink.diagnostics())
            except FileNotFoundError:
                audio_enabled = False
                audio.record_error("pactl not found; PulseAudio recording disabled")
                logger.warning("pactl was not found; PulseAudio recording is disabled.")
            except Exception as exc:
                audio_enabled = False
                audio.record_error(f"PulseAudio setup failed: {exc}")
                logger.exception("PulseAudio setup failed; continuing without audio recording")
            self._start_display()
            self.driver = self._launch_browser()
            self._sign_in(logger, meeting_dir)
            admission_status = self._join_meeting(meeting.meet_url, logger, meeting_dir)
            if admission_status != "joined":
                status = admission_status
                logger.warning("Meeting was not joined: %s", admission_status)
            else:
                self._enable_captions(logger, meeting_dir)
                joined_at = datetime.now(timezone.utc)
                metadata["actual_join_time"] = joined_at.isoformat()
                self.storage.update_metadata(meeting_dir, actual_join_time=joined_at.isoformat())

                # Audio and captions share the same stop_event and start timestamp so final artifacts align.
                if audio_enabled:
                    audio_thread = threading.Thread(target=audio.start, args=(meeting_dir, stop_event), daemon=True)
                caption_thread = threading.Thread(
                    target=captions.start,
                    args=(self.driver, meeting_dir, stop_event, joined_at),
                    daemon=True,
                )
                if audio_thread:
                    audio_thread.start()
                caption_thread.start()
                logger.info("Capture workers started: audio_enabled=%s captions_enabled=%s", bool(audio_thread), True)
                status = self._wait_for_meeting_end(meeting, stop_event, logger, meeting_dir)
        except TwoFactorRequiredError:
            logger.exception("Google sign-in requires 2FA; aborting")
            status = "failed"
        except Exception:
            logger.exception("Meet bot failed")
            status = "partial" if joined_at else "failed"
        finally:
            stop_event.set()
            for thread in (audio_thread, caption_thread):
                if thread:
                    thread.join(timeout=120)
            result = self._finish(meeting_dir, metadata, status, joined_at, audio, captions)
            logger.info(
                "Meeting bot run finished: status=%s duration_seconds=%s audio_file=%s transcript_file=%s total_lines=%s speakers=%s",
                result.get("status"),
                result.get("duration_seconds"),
                result.get("audio_file"),
                result.get("transcript_file"),
                result.get("total_lines"),
                result.get("speakers_detected"),
            )
            self._cleanup()
        return result

    def _start_display(self) -> None:
        if platform.system() != "Linux":
            LOGGER.info("pyvirtualdisplay is only used on Linux; using the local display")
            return
        try:
            from pyvirtualdisplay import Display
        except ImportError:
            LOGGER.info("pyvirtualdisplay not installed; assuming a display is already available")
            return
        self.display = Display(visible=False, size=(1920, 1080))
        self.display.start()

    def _launch_browser(self):
        import undetected_chromedriver as uc

        options = uc.ChromeOptions()
        chrome_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--autoplay-policy=no-user-gesture-required",
            "--window-size=1920,1080",
        ]
        if getattr(self.settings, "chrome_headless", False):
            chrome_args.append("--headless=new")
        chrome_args.append("--alsa-output-device=pulse")
        for arg in chrome_args:
            options.add_argument(arg)
        options.add_experimental_option(
            "prefs",
            {
                "profile.default_content_setting_values.media_stream_mic": 2,
                "profile.default_content_setting_values.media_stream_camera": 2,
                "profile.managed_default_content_settings.media_stream_mic": 2,
                "profile.managed_default_content_settings.media_stream_camera": 2,
            },
        )
        kwargs: dict[str, Any] = {"options": options}
        version_main = self._chrome_version_main()
        if version_main:
            LOGGER.info("Using Chrome major version %s for undetected_chromedriver", version_main)
            kwargs["version_main"] = version_main
        if getattr(self.settings, "chrome_binary_path", None):
            kwargs["browser_executable_path"] = self.settings.chrome_binary_path
        if getattr(self.settings, "chrome_user_data_dir", None):
            kwargs["user_data_dir"] = self.settings.chrome_user_data_dir
        if getattr(self.settings, "chrome_profile_directory", None):
            options.add_argument(f"--profile-directory={self.settings.chrome_profile_directory}")
        driver = uc.Chrome(**kwargs)
        self._block_meet_media_permissions(driver)
        return driver

    def _block_meet_media_permissions(self, driver: Any) -> None:
        for permission in ("microphone", "camera"):
            try:
                driver.execute_cdp_cmd(
                    "Browser.setPermission",
                    {
                        "permission": {"name": permission},
                        "setting": "denied",
                        "origin": "https://meet.google.com",
                    },
                )
            except Exception:
                LOGGER.debug("Could not block %s permission through Chrome DevTools", permission, exc_info=True)

    def _create_audio_recorder(self, sink_name: str) -> AudioRecorder:
        return AudioRecorder(sink_name=sink_name, settings=self.settings, device="pulse")

    def _audio_capture_available(self, audio: AudioRecorder, logger: logging.Logger) -> bool:  # noqa: ARG002
        return True

    def _chrome_version_main(self) -> int | None:
        if getattr(self.settings, "chrome_version_main", None):
            return self.settings.chrome_version_main
        version_output = self._read_chrome_version()
        if not version_output:
            return None
        match = re.search(r"(\d+)\.", version_output)
        return int(match.group(1)) if match else None

    def _read_chrome_version(self) -> str | None:
        candidates = []
        if getattr(self.settings, "chrome_binary_path", None):
            candidates.append(self.settings.chrome_binary_path)
        candidates.extend(["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"])

        for candidate in candidates:
            executable = candidate if Path(candidate).exists() else shutil.which(candidate)
            if not executable:
                continue
            try:
                result = subprocess.run(
                    [executable, "--version"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except Exception:
                LOGGER.debug("Could not read Chrome version from %s", executable, exc_info=True)
                continue
            output = result.stdout.strip() or result.stderr.strip()
            if output:
                return output
        return None

    def _sign_in(self, logger: logging.Logger, meeting_dir: Path) -> None:
        if getattr(self.settings, "skip_google_login", False):
            logger.info("Skipping automated Google sign-in because SKIP_GOOGLE_LOGIN=true")
            return
        if self._already_signed_in():
            logger.info("Google account already signed in")
            return
        if self._load_cookies():
            self.driver.get("https://accounts.google.com/")
            self.driver.refresh()
            self._sleep(3)
            if self._already_signed_in():
                logger.info("Google account session restored from cookies")
                return
        if not self.settings.bot_google_account_email or not self.settings.bot_google_account_password:
            raise RuntimeError("BOT_GOOGLE_ACCOUNT_EMAIL and BOT_GOOGLE_ACCOUNT_PASSWORD are required")
        self.driver.get("https://accounts.google.com/signin/v2/identifier")
        self._click_first(
            [
                '//div[contains(text(), "Use another account")]/ancestor::*[@role="link" or @role="button"]',
                '//span[contains(text(), "Use another account")]/ancestor::*[@role="link" or @role="button"]',
            ],
            timeout=5,
        )
        self._type_when_available('input[type="email"], input#identifierId', self.settings.bot_google_account_email)
        self._click_first(["#identifierNext", '//span[contains(text(), "Next")]/ancestor::button'], timeout=15)
        if not self._wait_for_password_page(timeout=60):
            self._dump_debug_page(meeting_dir, "google_signin_no_password")
            raise RuntimeError(
                "Google sign-in did not show the password field. "
                "Check google_signin_no_password.png/html in the meeting folder, or use a signed-in Chrome profile."
            )
        self._raise_if_2fa()
        self._type_when_available('input[type="password"], input[name="Passwd"]', self.settings.bot_google_account_password)
        self._click_first(["#passwordNext", '//span[contains(text(), "Next")]/ancestor::button'], timeout=15)
        self._sleep(8)
        self._raise_if_2fa()
        if self._signin_blocked():
            self._dump_debug_page(meeting_dir, "google_signin_blocked")
            raise RuntimeError("Google blocked automated sign-in; use a persistent signed-in Chrome profile.")
        self._save_cookies()
        logger.info("Google account sign-in completed")

    def _already_signed_in(self) -> bool:
        self.driver.get("https://myaccount.google.com/")
        self._sleep(3)
        return "accounts.google.com" not in getattr(self.driver, "current_url", "")

    def _wait_for_password_page(self, timeout: int) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._raise_if_2fa()
            if self._signin_blocked():
                return False
            if self._find_first(['input[type="password"], input[name="Passwd"]'], timeout=1):
                return True
            time.sleep(1)
        return False

    def _signin_blocked(self) -> bool:
        markers = [
            "couldn't sign you in",
            "could not sign you in",
            "this browser or app may not be secure",
            "to help keep your account safe",
            "verify it’s you",
            "verify it's you",
            "captcha",
        ]
        page = self._page_text().lower()
        return any(marker in page for marker in markers)

    def _dump_debug_page(self, meeting_dir: Path, name: str) -> None:
        try:
            (meeting_dir / f"{name}.html").write_text(self.driver.page_source or "", encoding="utf-8")
        except Exception:
            LOGGER.exception("Could not write debug HTML")
        try:
            self.driver.save_screenshot(str(meeting_dir / f"{name}.png"))
        except Exception:
            LOGGER.exception("Could not write debug screenshot")

    def _join_meeting(self, meet_url: str, logger: logging.Logger, meeting_dir: Path) -> str:
        self.driver.get(meet_url)
        self._sleep(5)
        self._prepare_prejoin_screen(logger)
        joined = self._click_first(
            [
                'button[aria-label*="Join now" i]',
                'button[aria-label*="Ask to join" i]',
                'button[aria-label*="Request to join" i]',
                'button[jsname="Qx7uuf"]',
                '//span[contains(text(), "Join now")]/ancestor::button',
                '//span[contains(text(), "Ask to join")]/ancestor::button',
                '//span[contains(text(), "Request to join")]/ancestor::button',
                '//button[contains(., "Join now")]',
                '//button[contains(., "Ask to join")]',
                '//button[contains(., "Request to join")]',
            ],
            timeout=30,
        )
        if not joined:
            logger.warning("Could not find or click the Meet join button. url=%s", getattr(self.driver, "current_url", ""))
            self._dump_debug_page(meeting_dir, "meet_join_failed")
            return "failed"
        deadline = time.monotonic() + self.settings.lobby_wait_minutes * 60
        while time.monotonic() < deadline:
            if self._join_denied_detected():
                logger.warning("Meet join request was denied")
                self._dump_debug_page(meeting_dir, "meet_join_denied")
                return "denied"
            blocker_status = self._resolve_post_join_blocking_dialog(logger)
            if blocker_status == "resolved":
                self._sleep(3)
                continue
            if blocker_status == "unresolved":
                logger.warning("Meet join blocked by unresolved dialog")
                self._dump_debug_page(meeting_dir, "meet_blocking_dialog_unresolved")
                return "blocked_by_dialog"
            if self._inside_meeting():
                logger.info("Admitted to Google Meet")
                return "joined"
            if self._waiting_for_host_detected():
                self._sleep(30)
                continue
            self._sleep(5)
        self._dump_debug_page(meeting_dir, "meet_not_admitted")
        return "not_admitted"

    def _prepare_prejoin_screen(self, logger: logging.Logger) -> None:
        self._dismiss_permission_dialogs()
        self._enter_guest_name(logger)
        self._set_mic_camera(enabled=False)
        self._dismiss_permission_dialogs()

    def _enter_guest_name(self, logger: logging.Logger) -> None:
        name = (getattr(self.settings, "bot_display_name", "") or "").strip()
        if not name:
            return
        element = self._find_guest_name_input(timeout=10)
        if not element:
            return
        try:
            element.clear()
            element.send_keys(name)
            logger.info("Entered Meet guest display name")
        except Exception:
            logger.exception("Could not enter Meet guest display name")

    def _find_guest_name_input(self, timeout: int = 10):
        selectors = [
            'input[aria-label*="Your name" i]',
            'input[placeholder*="Your name" i]',
            'input[aria-label*="name" i]',
            'input[placeholder*="name" i]',
            (
                '//input[not(@type) or @type="text"]'
                '[contains(translate(@aria-label, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "name")'
                ' or contains(translate(@placeholder, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "name")]'
            ),
        ]
        element = self._find_first(selectors, timeout=timeout)
        if element:
            return element
        return self._find_first(['//input[not(@type) or @type="text"]'], timeout=2)

    def _enable_captions(self, logger: logging.Logger, meeting_dir: Path | None = None) -> None:
        # Meet changes these controls often; keep every selector fallback active and logged.
        selectors = [
            'button[aria-label="Turn on captions"]',
            'button[aria-label*="caption" i]',
            '[data-tooltip*="caption" i]',
            '[jsname="r8qRAd"]',
        ]
        if self._captions_enabled():
            logger.info("Captions already enabled")
            return
        if self._click_first(selectors, timeout=20):
            logger.info("Captions enabled")
            return
        if self._send_keyboard_key("c"):
            self._sleep(3)
            if self._captions_enabled():
                logger.info("Captions enabled with keyboard shortcut")
                return
            logger.warning("Tried captions keyboard shortcut, but captions did not appear enabled")
        if meeting_dir:
            self._dump_debug_page(meeting_dir, "meet_captions_failed")
        logger.warning("Could not enable captions; caption scraper will keep retrying DOM discovery")

    def _wait_for_meeting_end(
        self,
        meeting: Any,
        stop_event: threading.Event,
        logger: logging.Logger,
        meeting_dir: Path | None = None,
    ) -> str:
        end_time = getattr(meeting, "end_time", None)
        if not end_time:
            end_time = datetime.now(timezone.utc) + timedelta(hours=self.settings.max_meeting_duration_hours)
        joined_monotonic = time.monotonic()
        hard_cutoff = end_time + timedelta(minutes=15)
        max_cutoff = datetime.now(timezone.utc) + timedelta(hours=self.settings.max_meeting_duration_hours)
        hard_cutoff = min(hard_cutoff, max_cutoff)
        while datetime.now(timezone.utc) < hard_cutoff and not stop_event.is_set():
            if time.monotonic() - joined_monotonic < 90:
                time.sleep(5)
                continue
            if self._meeting_left_detected():
                logger.info("Meeting end detected by DOM")
                return "completed"
            current_url = getattr(self.driver, "current_url", "")
            if current_url and "meet.google.com" not in current_url:
                logger.info("Meeting end detected by URL change: %s", current_url)
                if meeting_dir:
                    self._dump_debug_page(meeting_dir, "meet_url_changed")
                return "completed"
            time.sleep(5)
        logger.info("Meeting stopped at hard cutoff")
        return "completed"

    def _meeting_left_detected(self) -> bool:
        page = self._page_text().lower()
        explicit_markers = [
            "you've left the meeting",
            "you left the meeting",
            "you've left the call",
            "you left the call",
        ]
        if any(marker in page for marker in explicit_markers):
            return True
        removal_markers = [
            "you've been removed from the meeting",
            "you were removed from the meeting",
            "you've been removed from the call",
            "you were removed from the call",
            "removed you from the meeting",
            "removed you from the call",
        ]
        removal_as_end = getattr(getattr(self, "settings", None), "treat_removal_as_meeting_end", True)
        if removal_as_end and any(marker in page for marker in removal_markers):
            return True
        return "return to home screen" in page and ("rejoin" in page or "you left" in page)

    def _join_denied_detected(self) -> bool:
        page = self._page_text().lower()
        denied_markers = [
            "denied your request to join",
            "request to join was denied",
            "you can't join this call",
            "you cannot join this call",
        ]
        return any(marker in page for marker in denied_markers)

    def _waiting_for_host_detected(self) -> bool:
        page = self._page_text().lower()
        waiting_markers = [
            "please wait until a meeting host brings you into the call",
            "meeting host brings you into the call",
            "someone will let you in",
            "waiting to be let in",
            "waiting for the host",
            "you'll join the call when someone lets you in",
            "you’ll join the call when someone lets you in",
            "ask to join",
            "asking to join",
        ]
        return any(marker in page for marker in waiting_markers)

    def _blocking_join_dialog_detected(self) -> bool:
        return bool(self._visible_blocking_dialogs())

    def _resolve_post_join_blocking_dialog(self, logger: logging.Logger) -> str:
        dialogs = self._visible_blocking_dialogs()
        if not dialogs:
            return "none"
        dialog = dialogs[-1]
        pattern_label = self._known_safe_dialog_button_label(dialog)
        if pattern_label and self._click_dialog_button(dialog, pattern_label):
            self._record_join_blocker("resolved", "pattern", "known_safe_dialog", dialog)
            logger.info("Accepted known-safe post-join Meet dialog: button=%s", pattern_label)
            return "resolved"
        llm_decision = self._classify_join_dialog_with_llm(dialog, logger)
        if self._llm_decision_allows(dialog, llm_decision) and self._click_dialog_button(dialog, llm_decision["button_label"]):
            self._record_join_blocker("resolved", "llm", llm_decision.get("reason") or "llm_allow", dialog)
            logger.info(
                "Accepted LLM-classified post-join Meet dialog: button=%s confidence=%.2f",
                llm_decision["button_label"],
                llm_decision.get("confidence", 0.0),
            )
            return "resolved"
        reason = (llm_decision or {}).get("reason") or "unrecognized_dialog"
        classifier = "llm" if llm_decision else "none"
        self._record_join_blocker("unresolved", classifier, reason, dialog)
        return "unresolved"

    def _visible_blocking_dialogs(self) -> list[dict[str, Any]]:
        dialogs: list[dict[str, Any]] = []
        seen: set[str] = set()
        try:
            elements = self.driver.find_elements("xpath", '//*[@aria-modal="true" or @role="dialog"]')
        except Exception:
            return dialogs
        for element in elements:
            if not self._element_visible(element):
                continue
            text = self._element_text(element)
            buttons = self._dialog_buttons(element)
            if not text or not buttons:
                continue
            aria_modal = (self._element_attribute(element, "aria-modal") or "").lower() == "true"
            if not aria_modal and not self._dialog_has_actionable_button(buttons):
                continue
            key = f"{text}|{','.join(button['label'] for button in buttons)}"
            if key in seen:
                continue
            seen.add(key)
            dialogs.append({"text": text, "buttons": buttons, "aria_modal": aria_modal})
        return dialogs

    def _dialog_buttons(self, dialog: Any) -> list[dict[str, Any]]:
        buttons: list[dict[str, Any]] = []
        try:
            elements = dialog.find_elements("xpath", './/button|.//*[@role="button"]')
        except Exception:
            return buttons
        seen: set[str] = set()
        for element in elements:
            if not self._element_visible(element) or not self._element_enabled(element):
                continue
            label = self._button_label(element)
            if not label:
                continue
            key = label.lower()
            if key in seen:
                continue
            seen.add(key)
            buttons.append({"label": label, "element": element})
        return buttons

    def _dialog_has_actionable_button(self, buttons: list[dict[str, Any]]) -> bool:
        for button in buttons:
            normalized = self._normalize_button_label(button["label"])
            if normalized and normalized not in {"close", "dismiss"}:
                return True
        return False

    def _known_safe_dialog_button_label(self, dialog: dict[str, Any]) -> str | None:
        text = dialog["text"]
        if not any(pattern.search(text) for pattern in KNOWN_SAFE_DIALOG_PATTERNS):
            return None
        return self._first_affirmative_button_label(dialog)

    def _first_affirmative_button_label(self, dialog: dict[str, Any]) -> str | None:
        for button in dialog["buttons"]:
            normalized = self._normalize_button_label(button["label"])
            if normalized in AFFIRMATIVE_DIALOG_LABELS:
                return button["label"]
        return None

    def _classify_join_dialog_with_llm(self, dialog: dict[str, Any], logger: logging.Logger) -> dict[str, Any] | None:
        if not self._dialog_llm_enabled():
            return None
        payload = {
            "dialog_text": self._clip_dialog_text(dialog["text"]),
            "button_labels": [button["label"] for button in dialog["buttons"]],
        }
        try:
            result = complete_llm_json(
                [
                    {"role": "system", "content": JOIN_DIALOG_LLM_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": "Classify this Google Meet blocker dialog:\n" + json.dumps(payload, ensure_ascii=False),
                    },
                ],
                self.settings,
                schema_name="meet_dialog_blocker",
                schema=JOIN_DIALOG_LLM_SCHEMA,
            )
        except Exception as exc:
            logger.warning("LLM join dialog classification failed; using safe fallback: %s", exc)
            return None
        return {
            "decision": str(result.get("decision") or "unknown").strip().lower(),
            "button_label": _clean_dialog_value(result.get("button_label")),
            "confidence": _safe_float(result.get("confidence")),
            "reason": _clean_dialog_value(result.get("reason")) or "llm_classification",
        }

    def _dialog_llm_enabled(self) -> bool:
        if not getattr(self.settings, "meet_dialog_llm_enabled", True):
            return False
        provider = str(getattr(self.settings, "meeting_intelligence_provider", "") or "").strip().lower().replace("-", "_")
        return provider == "llm"

    def _llm_decision_allows(self, dialog: dict[str, Any], decision: dict[str, Any] | None) -> bool:
        if not decision:
            return False
        label = decision.get("button_label") or ""
        normalized = self._normalize_button_label(label)
        if decision.get("decision") != "allow":
            return False
        if decision.get("confidence", 0.0) < MIN_DIALOG_LLM_CONFIDENCE:
            return False
        if normalized in NEGATIVE_DIALOG_LABELS:
            return False
        visible_labels = {self._normalize_button_label(button["label"]) for button in dialog["buttons"]}
        return normalized in visible_labels and bool(normalized)

    def _click_dialog_button(self, dialog: dict[str, Any], label: str) -> bool:
        normalized = self._normalize_button_label(label)
        if normalized in NEGATIVE_DIALOG_LABELS:
            return False
        for button in dialog["buttons"]:
            if self._normalize_button_label(button["label"]) == normalized:
                button["element"].click()
                return True
        return False

    def _record_join_blocker(
        self,
        status: str,
        classifier: str,
        reason: str,
        dialog: dict[str, Any],
    ) -> None:
        self._join_blocker = {
            "join_blocker_status": status,
            "join_blocker_classifier": classifier,
            "join_blocker_reason": _clean_dialog_value(reason) or "unknown",
            "join_blocker_text_excerpt": self._clip_dialog_text(dialog.get("text", "")),
        }

    def _join_blocker_metadata(self) -> dict[str, Any]:
        return dict(getattr(self, "_join_blocker", None) or {})

    def _button_label(self, element: Any) -> str:
        for attribute in ("aria-label", "data-tooltip", "title"):
            value = self._element_attribute(element, attribute)
            if value:
                return _clean_dialog_value(value)
        return _clean_dialog_value(self._element_text(element))

    def _element_text(self, element: Any) -> str:
        try:
            return _clean_dialog_value(element.text)
        except Exception:
            text = self._element_attribute(element, "textContent")
            return _clean_dialog_value(text)

    def _element_attribute(self, element: Any, name: str) -> str:
        try:
            return str(element.get_attribute(name) or "")
        except Exception:
            return ""

    def _element_visible(self, element: Any) -> bool:
        try:
            return bool(element.is_displayed())
        except Exception:
            return False

    def _element_enabled(self, element: Any) -> bool:
        try:
            return bool(element.is_enabled())
        except Exception:
            return False

    def _normalize_button_label(self, label: str) -> str:
        return " ".join(str(label or "").strip().lower().split())

    def _clip_dialog_text(self, text: str) -> str:
        text = _clean_dialog_value(text)
        return text[:MAX_DIALOG_TEXT_CHARS]

    def _finish(
        self,
        meeting_dir: Path,
        metadata: dict[str, Any],
        status: str,
        joined_at: datetime | None,
        audio: AudioRecorder,
        captions: CaptionScraper,
    ) -> dict[str, Any]:
        leave_time = datetime.now(timezone.utc)
        lines = captions.get_lines()
        transcript_updates = self.storage.finalize_transcripts(meeting_dir, lines)
        metadata.update(transcript_updates)
        intelligence_updates = self._generate_meeting_intelligence(meeting_dir, lines, metadata)
        audio_status = audio.get_status()
        final_status = status
        if status == "completed" and (audio_status.get("errors") or not audio_status.get("audio_file")):
            final_status = "partial"
        metadata.update(
            intelligence_updates,
            **self._join_blocker_metadata(),
            status=final_status,
            actual_leave_time=leave_time.isoformat(),
            duration_seconds=int((leave_time - joined_at).total_seconds()) if joined_at else 0,
            audio_file=audio_status.get("audio_file"),
            audio_duration_seconds=audio.get_duration_seconds(),
            audio_silent_chunks=audio_status.get("silent_chunks", 0),
        )
        if audio_status.get("errors"):
            metadata["audio_errors"] = audio_status["errors"]
        if audio_status.get("mp3_file"):
            metadata["audio_mp3_file"] = audio_status["mp3_file"]
        metadata.update(self._send_summary_email(meeting_dir, metadata))
        self.storage.write_metadata(meeting_dir, metadata)
        return metadata

    def _send_summary_email(self, meeting_dir: Path, metadata: dict[str, Any]) -> dict[str, Any]:
        return send_summary_email_for_meeting(meeting_dir, metadata, settings=self.settings)

    def _generate_meeting_intelligence(
        self,
        meeting_dir: Path,
        lines: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        if not getattr(self.settings, "meeting_intelligence_enabled", True):
            return {"meeting_intelligence_status": "disabled"}
        provider_name = getattr(self.settings, "meeting_intelligence_provider", "rule_based")
        try:
            provider = create_meeting_intelligence_provider(provider_name, settings=self.settings)
            result = provider.analyze(lines, metadata)
            markdown = render_intelligence_markdown(result)
            updates = self.storage.write_meeting_intelligence(meeting_dir, result, markdown)
            updates["meeting_intelligence_status"] = "completed"
            return updates
        except Exception as exc:
            LOGGER.exception("Meeting intelligence generation failed")
            fallback_provider_name = getattr(self.settings, "meeting_llm_fallback_provider", "")
            if provider_name == "llm" and fallback_provider_name and fallback_provider_name != "llm":
                try:
                    fallback = create_meeting_intelligence_provider(fallback_provider_name, settings=self.settings)
                    result = fallback.analyze(lines, metadata)
                    result["fallback_reason"] = str(exc)
                    markdown = render_intelligence_markdown(result)
                    updates = self.storage.write_meeting_intelligence(meeting_dir, result, markdown)
                    updates["meeting_intelligence_status"] = "completed_with_fallback"
                    updates["meeting_intelligence_error"] = str(exc)
                    return updates
                except Exception as fallback_exc:
                    LOGGER.exception("Fallback meeting intelligence generation failed")
                    return {
                        "meeting_intelligence_status": "failed",
                        "meeting_intelligence_error": f"{exc}; fallback failed: {fallback_exc}",
                    }
            return {
                "meeting_intelligence_status": "failed",
                "meeting_intelligence_error": str(exc),
            }

    def _cleanup(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                LOGGER.exception("Failed to quit webdriver")
        if self.display:
            try:
                self.display.stop()
            except Exception:
                LOGGER.exception("Failed to stop virtual display")
        if self.pulse_sink:
            self.pulse_sink.cleanup()

    def _load_cookies(self) -> bool:
        path = self.settings.bot_session_path
        if not path.exists():
            return False
        self.driver.get("https://accounts.google.com/")
        with path.open("rb") as handle:
            for cookie in pickle.load(handle):
                try:
                    self.driver.add_cookie(cookie)
                except Exception:
                    LOGGER.debug("Skipping incompatible cookie", exc_info=True)
        return True

    def _save_cookies(self) -> None:
        self.settings.bot_session_path.parent.mkdir(parents=True, exist_ok=True)
        with self.settings.bot_session_path.open("wb") as handle:
            pickle.dump(self.driver.get_cookies(), handle)

    def _raise_if_2fa(self) -> None:
        markers = ["2-Step Verification", "Verify it", "Get a verification code", "Use your phone"]
        if any(self._page_contains(marker) for marker in markers):
            raise TwoFactorRequiredError("Google account requires 2FA")

    def _dismiss_permission_dialogs(self) -> None:
        selectors = [
            'button[aria-label*="Dismiss" i]',
            'button[aria-label*="Got it" i]',
            'button[aria-label*="Not now" i]',
            'button[aria-label*="No thanks" i]',
            '//span[contains(text(), "Got it")]/ancestor::button',
            '//span[contains(text(), "Dismiss")]/ancestor::button',
            '//span[contains(text(), "Not now")]/ancestor::button',
            '//span[contains(text(), "No thanks")]/ancestor::button',
            '//span[contains(text(), "Continue without microphone and camera")]/ancestor::button',
            '//span[contains(text(), "Continue without microphone")]/ancestor::button',
            '//span[contains(text(), "Continue without camera")]/ancestor::button',
        ]
        for _ in range(4):
            if not self._click_first(selectors, timeout=2):
                return
            self._sleep(1)

    def _set_mic_camera(self, enabled: bool) -> None:
        labels = ["microphone", "camera"]
        for label in labels:
            self._click_first([f'button[aria-label*="Turn off {label}" i]'], timeout=3)
        if enabled:
            for label in labels:
                self._click_first([f'button[aria-label*="Turn on {label}" i]'], timeout=3)

    def _inside_meeting(self) -> bool:
        if self._join_denied_detected() or self._waiting_for_host_detected() or self._blocking_join_dialog_detected():
            return False
        in_call_controls = [
            'button[aria-label*="Leave call" i]',
            'button[data-tooltip*="Leave call" i]',
            'button[aria-label*="Turn on captions" i]',
            'button[aria-label*="Turn off captions" i]',
        ]
        if self._find_first(in_call_controls, timeout=1):
            return True
        return False

    def _captions_enabled(self) -> bool:
        selectors = [
            'button[aria-label*="Turn off captions" i]',
            'button[data-tooltip*="Turn off captions" i]',
        ]
        return self._find_first(selectors, timeout=1) is not None

    def _type_when_available(self, selector: str, text: str, timeout: int = 30) -> None:
        element = self._find_first([selector], timeout=timeout)
        if not element:
            raise RuntimeError(f"Element not found: {selector}")
        element.clear()
        element.send_keys(text)

    def _click_first(self, selectors: list[str], timeout: int = 10) -> bool:
        element = self._find_first(selectors, timeout=timeout)
        if not element:
            return False
        element.click()
        return True

    def _send_keyboard_key(self, key: str) -> bool:
        try:
            bodies = self.driver.find_elements("tag name", "body")
            if not bodies:
                return False
            bodies[0].click()
            bodies[0].send_keys(key)
            return True
        except Exception:
            LOGGER.debug("Could not send keyboard shortcut %s", key, exc_info=True)
            return False

    def _find_first(self, selectors: list[str], timeout: int = 10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for selector in selectors:
                try:
                    if selector.startswith("//"):
                        elements = self.driver.find_elements("xpath", selector)
                    else:
                        elements = self.driver.find_elements("css selector", selector)
                    for element in elements:
                        if element.is_displayed() and element.is_enabled():
                            return element
                except Exception:
                    continue
            time.sleep(0.5)
        return None

    def _page_contains(self, text: str) -> bool:
        return text.lower() in self._page_text().lower()

    def _page_text(self) -> str:
        try:
            return self.driver.execute_script("return document.body ? document.body.innerText : ''") or ""
        except Exception:
            return ""

    def _sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class TwoFactorRequiredError(RuntimeError):
    pass


def _clean_dialog_value(value: Any) -> str:
    return " ".join(str(value or "").split())


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))
