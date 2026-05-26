from __future__ import annotations

from types import SimpleNamespace
import json
import logging
import tempfile
import unittest
from unittest.mock import patch

from audio_recorder import AudioRecorder
from meet_bot import MeetBot
from storage import MeetingStorage


class MeetBotPlatformTests(unittest.TestCase):
    def test_auto_audio_backend_uses_sounddevice_on_macos(self):
        bot = MeetBot.__new__(MeetBot)
        bot.settings = SimpleNamespace(audio_backend="auto")

        with patch("meet_bot.platform.system", lambda: "Darwin"):
            self.assertFalse(bot._uses_pulseaudio())

    def test_macos_requires_explicit_audio_input_device(self):
        settings = SimpleNamespace(audio_backend="sounddevice", audio_input_device=None)
        bot = MeetBot.__new__(MeetBot)
        bot.settings = settings
        audio = AudioRecorder(settings=settings, device=None)
        logger = logging.getLogger("test")
        logger.disabled = True

        with patch("meet_bot.platform.system", lambda: "Darwin"):
            enabled = bot._audio_capture_available(audio, logger)

        self.assertFalse(enabled)
        self.assertIn("AUDIO_INPUT_DEVICE is not set", audio.get_status()["errors"][0])

    def test_chrome_major_version_is_parsed_from_browser_output(self):
        bot = MeetBot.__new__(MeetBot)
        bot.settings = SimpleNamespace(chrome_version_main=None, chrome_binary_path=None)
        bot._read_chrome_version = lambda: "Google Chrome 148.0.7778.179"

        self.assertEqual(bot._chrome_version_main(), 148)

    def test_chrome_major_version_env_override_wins(self):
        bot = MeetBot.__new__(MeetBot)
        bot.settings = SimpleNamespace(chrome_version_main=148, chrome_binary_path=None)
        bot._read_chrome_version = lambda: "Google Chrome 149.0.0.0"

        self.assertEqual(bot._chrome_version_main(), 148)

    def test_guest_name_prompt_is_filled_before_join(self):
        element = FakeElement()
        bot = MeetBot.__new__(MeetBot)
        bot.settings = SimpleNamespace(bot_display_name="MeetRead Bot")
        bot.driver = FakeDriver(
            {
                'input[aria-label*="Your name" i]': [element],
            }
        )
        logger = logging.getLogger("test")
        logger.disabled = True

        bot._enter_guest_name(logger)

        self.assertTrue(element.cleared)
        self.assertEqual(element.typed_text, "MeetRead Bot")

    def test_guest_name_prompt_uses_text_input_fallback(self):
        element = FakeElement()
        bot = MeetBot.__new__(MeetBot)
        bot.driver = FakeDriver(
            {
                '//input[not(@type) or @type="text"]': [element],
            }
        )

        self.assertIs(bot._find_guest_name_input(timeout=1), element)

    def test_meet_media_permissions_are_denied(self):
        bot = MeetBot.__new__(MeetBot)
        driver = FakeDriver({})

        bot._block_meet_media_permissions(driver)

        self.assertEqual(
            driver.cdp_calls,
            [
                (
                    "Browser.setPermission",
                    {
                        "permission": {"name": "microphone"},
                        "setting": "denied",
                        "origin": "https://meet.google.com",
                    },
                ),
                (
                    "Browser.setPermission",
                    {
                        "permission": {"name": "camera"},
                        "setting": "denied",
                        "origin": "https://meet.google.com",
                    },
                ),
            ],
        )

    def test_pulseaudio_recorder_uses_portaudio_pulse_device(self):
        bot = MeetBot.__new__(MeetBot)
        bot.settings = SimpleNamespace(audio_backend="pulseaudio", audio_input_device=None)

        audio = bot._create_audio_recorder("MeetBot_test")

        self.assertEqual(audio.device, "pulse")
        self.assertEqual(audio.get_status()["pulse_source"], "MeetBot_test.monitor")

    def test_return_to_home_screen_alone_does_not_end_meeting(self):
        bot = MeetBot.__new__(MeetBot)
        bot._page_text = lambda: "Return to home screen"

        self.assertFalse(bot._meeting_left_detected())

    def test_explicit_left_meeting_text_ends_meeting(self):
        bot = MeetBot.__new__(MeetBot)
        bot._page_text = lambda: "You've left the meeting"

        self.assertTrue(bot._meeting_left_detected())

    def test_removed_from_meeting_text_ends_meeting(self):
        bot = MeetBot.__new__(MeetBot)
        bot.settings = SimpleNamespace(treat_removal_as_meeting_end=True)
        bot._page_text = lambda: "You were removed from the meeting"

        self.assertTrue(bot._meeting_left_detected())

    def test_removed_from_meeting_text_can_be_ignored(self):
        bot = MeetBot.__new__(MeetBot)
        bot.settings = SimpleNamespace(treat_removal_as_meeting_end=False)
        bot._page_text = lambda: "You were removed from the meeting"

        self.assertFalse(bot._meeting_left_detected())

    def test_captions_use_keyboard_shortcut_fallback(self):
        body = FakeElement()
        bot = MeetBot.__new__(MeetBot)
        bot.driver = FakeDriver({"body": [body]})
        bot._captions_enabled = lambda: body.typed_text == "c"
        logger = logging.getLogger("test")
        logger.disabled = True

        with tempfile.TemporaryDirectory() as tmpdir:
            bot._enable_captions(logger, meeting_dir=tmpdir)

        self.assertTrue(body.clicked)
        self.assertEqual(body.typed_text, "c")

    def test_join_denied_is_detected_from_visible_text(self):
        bot = MeetBot.__new__(MeetBot)
        bot.driver = FakeDriver({}, visible_text="Someone in the call denied your request to join")

        self.assertTrue(bot._join_denied_detected())

    def test_inside_meeting_requires_visible_call_controls(self):
        bot = MeetBot.__new__(MeetBot)
        bot.driver = FakeDriver({}, visible_text="People captions Leave call hidden in source")

        self.assertTrue(bot._inside_meeting())

    def test_denied_page_is_not_inside_meeting(self):
        bot = MeetBot.__new__(MeetBot)
        bot.driver = FakeDriver({}, visible_text="Someone in the call denied your request to join")

        self.assertFalse(bot._inside_meeting())

    def test_generate_meeting_intelligence_writes_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = MeetBot.__new__(MeetBot)
            bot.settings = SimpleNamespace(meeting_intelligence_enabled=True, meeting_intelligence_provider="rule_based")
            bot.storage = MeetingStorage(tmpdir)
            meeting_dir = bot.storage.create_meeting_dir("Product Sync")
            metadata = {"meeting_id": "m1", "title": "Product Sync", "transcript_file": "transcript_final.json"}

            updates = bot._generate_meeting_intelligence(
                meeting_dir,
                [
                    {
                        "index": 1,
                        "timestamp": "00:01:00",
                        "speaker": "Alice",
                        "text": "Alice will send the product launch notes tomorrow.",
                    }
                ],
                metadata,
            )

            self.assertEqual(updates["meeting_intelligence_status"], "completed")
            self.assertTrue((meeting_dir / "meeting_intelligence.md").exists())
            result = json.loads((meeting_dir / "meeting_intelligence.json").read_text())
            self.assertEqual(result["provider"], "rule_based")
            self.assertEqual(result["action_items"][0]["owner"], "Alice")

    def test_generate_meeting_intelligence_can_be_disabled(self):
        bot = MeetBot.__new__(MeetBot)
        bot.settings = SimpleNamespace(meeting_intelligence_enabled=False)

        updates = bot._generate_meeting_intelligence("/tmp", [], {})

        self.assertEqual(updates["meeting_intelligence_status"], "disabled")

    def test_generate_meeting_intelligence_falls_back_when_llm_fails(self):
        class FailingProvider:
            def analyze(self, lines, metadata):
                raise RuntimeError("llm unavailable")

        class FallbackProvider:
            def analyze(self, lines, metadata):
                return {
                    "provider": "rule_based",
                    "title": "Fallback",
                    "summary": "Fallback summary.",
                    "key_points": [],
                    "decisions": [],
                    "risks": [],
                    "action_items": [],
                    "questions": [],
                    "blockers": [],
                    "topics": [],
                }

        def fake_factory(provider_name, settings=None):
            if provider_name == "llm":
                return FailingProvider()
            return FallbackProvider()

        with tempfile.TemporaryDirectory() as tmpdir:
            bot = MeetBot.__new__(MeetBot)
            bot.settings = SimpleNamespace(
                meeting_intelligence_enabled=True,
                meeting_intelligence_provider="llm",
                meeting_llm_fallback_provider="rule_based",
            )
            bot.storage = MeetingStorage(tmpdir)
            meeting_dir = bot.storage.create_meeting_dir("Fallback")

            with patch("meet_bot.create_meeting_intelligence_provider", fake_factory), patch(
                "meet_bot.LOGGER.exception"
            ):
                updates = bot._generate_meeting_intelligence(meeting_dir, [], {"title": "Fallback"})

            self.assertEqual(updates["meeting_intelligence_status"], "completed_with_fallback")
            result = json.loads((meeting_dir / "meeting_intelligence.json").read_text())
            self.assertEqual(result["provider"], "rule_based")
            self.assertEqual(result["fallback_reason"], "llm unavailable")

    def test_send_summary_email_delegates_to_delivery_module(self):
        bot = MeetBot.__new__(MeetBot)
        bot.settings = SimpleNamespace(summary_email_enabled=True)

        with patch("meet_bot.send_summary_email_for_meeting", return_value={"summary_email_status": "sent"}) as send:
            updates = bot._send_summary_email("/tmp/meeting", {"title": "Product Sync"})

        self.assertEqual(updates["summary_email_status"], "sent")
        send.assert_called_once_with("/tmp/meeting", {"title": "Product Sync"}, settings=bot.settings)


class FakeElement:
    def __init__(self):
        self.cleared = False
        self.clicked = False
        self.typed_text = ""

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def clear(self):
        self.cleared = True

    def click(self):
        self.clicked = True

    def send_keys(self, text):
        self.typed_text += text


class FakeDriver:
    def __init__(self, elements_by_selector, visible_text=""):
        self.elements_by_selector = elements_by_selector
        self.cdp_calls = []
        self.visible_text = visible_text

    def find_elements(self, strategy, selector):
        return self.elements_by_selector.get(selector, [])

    def execute_cdp_cmd(self, command, params):
        self.cdp_calls.append((command, params))

    def execute_script(self, script):
        return self.visible_text
