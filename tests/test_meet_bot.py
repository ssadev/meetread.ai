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
        bot.settings = SimpleNamespace()

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
        leave_button = FakeElement()
        bot = MeetBot.__new__(MeetBot)
        bot.driver = FakeDriver({'button[aria-label*="Leave call" i]': [leave_button]}, visible_text="People captions")

        self.assertTrue(bot._inside_meeting())

    def test_post_join_consent_dialog_is_not_inside_meeting(self):
        leave_button = FakeElement()
        join_button = FakeElement(text="Join now")
        dialog = FakeElement(
            text="Your call audio and video will be shared with Read AI",
            attrs={"aria-modal": "true"},
            children_by_selector={'.//button|.//*[@role="button"]': [join_button]},
        )
        bot = MeetBot.__new__(MeetBot)
        bot.driver = FakeDriver(
            {
                'button[aria-label*="Leave call" i]': [leave_button],
                '//*[@aria-modal="true" or @role="dialog"]': [dialog],
            },
            visible_text="People captions",
        )

        self.assertTrue(bot._blocking_join_dialog_detected())
        self.assertFalse(bot._inside_meeting())

    def test_known_safe_post_join_consent_dialog_can_be_accepted(self):
        join_button = FakeElement(text="Join now")
        dialog = FakeElement(
            text="Your call audio and video will be shared with Read AI",
            attrs={"aria-modal": "true"},
            children_by_selector={'.//button|.//*[@role="button"]': [join_button]},
        )
        bot = MeetBot.__new__(MeetBot)
        bot.settings = SimpleNamespace(meeting_intelligence_provider="rule_based", meet_dialog_llm_enabled=True)
        bot.driver = FakeDriver(
            {'//*[@aria-modal="true" or @role="dialog"]': [dialog]},
            visible_text="People captions",
        )
        logger = logging.getLogger("test")
        logger.disabled = True

        self.assertEqual(bot._resolve_post_join_blocking_dialog(logger), "resolved")
        self.assertTrue(join_button.clicked)
        self.assertEqual(bot._join_blocker_metadata()["join_blocker_classifier"], "pattern")

    def test_unknown_dialog_is_unresolved_when_llm_is_disabled(self):
        continue_button = FakeElement(text="Continue")
        dialog = FakeElement(
            text="A third-party add-on needs a decision",
            attrs={"aria-modal": "true"},
            children_by_selector={'.//button|.//*[@role="button"]': [continue_button]},
        )
        bot = MeetBot.__new__(MeetBot)
        bot.settings = SimpleNamespace(meeting_intelligence_provider="rule_based", meet_dialog_llm_enabled=True)
        bot.driver = FakeDriver({'//*[@aria-modal="true" or @role="dialog"]': [dialog]})
        logger = logging.getLogger("test")
        logger.disabled = True

        self.assertEqual(bot._resolve_post_join_blocking_dialog(logger), "unresolved")
        self.assertFalse(continue_button.clicked)
        self.assertEqual(bot._join_blocker_metadata()["join_blocker_status"], "unresolved")

    def test_llm_can_accept_unknown_dialog_with_exact_visible_button(self):
        continue_button = FakeElement(text="Continue")
        dialog = FakeElement(
            text="Assistant add-on needs permission to continue joining this call",
            attrs={"aria-modal": "true"},
            children_by_selector={'.//button|.//*[@role="button"]': [continue_button]},
        )
        bot = MeetBot.__new__(MeetBot)
        bot.settings = SimpleNamespace(
            meeting_intelligence_provider="llm",
            meet_dialog_llm_enabled=True,
            meeting_llm_provider="openai_compatible",
        )
        bot.driver = FakeDriver({'//*[@aria-modal="true" or @role="dialog"]': [dialog]})
        logger = logging.getLogger("test")
        logger.disabled = True

        with patch(
            "meet_bot.complete_llm_json",
            return_value={
                "decision": "allow",
                "button_label": "Continue",
                "confidence": 0.92,
                "reason": "benign meeting assistant consent",
            },
        ):
            self.assertEqual(bot._resolve_post_join_blocking_dialog(logger), "resolved")

        self.assertTrue(continue_button.clicked)
        self.assertEqual(bot._join_blocker_metadata()["join_blocker_classifier"], "llm")

    def test_llm_low_confidence_does_not_click_unknown_dialog(self):
        continue_button = FakeElement(text="Continue")
        dialog = FakeElement(
            text="Assistant add-on needs permission to continue joining this call",
            attrs={"aria-modal": "true"},
            children_by_selector={'.//button|.//*[@role="button"]': [continue_button]},
        )
        bot = MeetBot.__new__(MeetBot)
        bot.settings = SimpleNamespace(meeting_intelligence_provider="llm", meet_dialog_llm_enabled=True)
        bot.driver = FakeDriver({'//*[@aria-modal="true" or @role="dialog"]': [dialog]})
        logger = logging.getLogger("test")
        logger.disabled = True

        with patch(
            "meet_bot.complete_llm_json",
            return_value={"decision": "allow", "button_label": "Continue", "confidence": 0.5, "reason": "unsure"},
        ):
            self.assertEqual(bot._resolve_post_join_blocking_dialog(logger), "unresolved")

        self.assertFalse(continue_button.clicked)

    def test_llm_never_clicks_destructive_dialog_button(self):
        leave_button = FakeElement(text="Leave")
        dialog = FakeElement(
            text="Leave this call?",
            attrs={"aria-modal": "true"},
            children_by_selector={'.//button|.//*[@role="button"]': [leave_button]},
        )
        bot = MeetBot.__new__(MeetBot)
        bot.settings = SimpleNamespace(meeting_intelligence_provider="llm", meet_dialog_llm_enabled=True)
        bot.driver = FakeDriver({'//*[@aria-modal="true" or @role="dialog"]': [dialog]})
        logger = logging.getLogger("test")
        logger.disabled = True

        with patch(
            "meet_bot.complete_llm_json",
            return_value={"decision": "allow", "button_label": "Leave", "confidence": 0.99, "reason": "bad advice"},
        ):
            self.assertEqual(bot._resolve_post_join_blocking_dialog(logger), "unresolved")

        self.assertFalse(leave_button.clicked)

    def test_stale_dialog_elements_are_not_treated_as_visible_or_enabled(self):
        bot = MeetBot.__new__(MeetBot)
        visible_error = FakeElement(display_error=True)
        enabled_error = FakeElement(enabled_error=True)

        self.assertFalse(bot._element_visible(visible_error))
        self.assertFalse(bot._element_enabled(enabled_error))

    def test_stale_dialog_is_ignored_by_blocker_detection(self):
        dialog = FakeElement(
            text="Your call audio and video will be shared with Read AI",
            attrs={"aria-modal": "true"},
            children_by_selector={'.//button|.//*[@role="button"]': [FakeElement(text="Join now")]},
            display_error=True,
        )
        bot = MeetBot.__new__(MeetBot)
        bot.driver = FakeDriver({'//*[@aria-modal="true" or @role="dialog"]': [dialog]})

        self.assertFalse(bot._blocking_join_dialog_detected())

    def test_join_meeting_returns_blocked_by_dialog_for_unresolved_blocker(self):
        join_button = FakeElement(text="Join now")
        continue_button = FakeElement(text="Continue")
        dialog = FakeElement(
            text="A third-party add-on needs a decision",
            attrs={"aria-modal": "true"},
            children_by_selector={'.//button|.//*[@role="button"]': [continue_button]},
        )
        bot = MeetBot.__new__(MeetBot)
        bot.settings = SimpleNamespace(
            lobby_wait_minutes=1,
            meeting_intelligence_provider="rule_based",
            meet_dialog_llm_enabled=True,
        )
        bot.driver = FakeDriver(
            {
                'button[aria-label*="Join now" i]': [join_button],
                '//*[@aria-modal="true" or @role="dialog"]': [dialog],
            }
        )
        bot._sleep = lambda seconds: None
        bot._prepare_prejoin_screen = lambda logger: None
        dumped = []
        bot._dump_debug_page = lambda meeting_dir, name: dumped.append(name)
        logger = logging.getLogger("test")
        logger.disabled = True

        self.assertEqual(bot._join_meeting("https://meet.google.com/abc-defg-hij", logger, "/tmp"), "blocked_by_dialog")
        self.assertEqual(dumped, ["meet_blocking_dialog_unresolved"])

    def test_waiting_for_host_page_is_not_inside_meeting(self):
        leave_button = FakeElement()
        bot = MeetBot.__new__(MeetBot)
        bot.driver = FakeDriver(
            {'button[aria-label*="Leave call" i]': [leave_button]},
            visible_text="Please wait until a meeting host brings you into the call",
        )

        self.assertTrue(bot._waiting_for_host_detected())
        self.assertFalse(bot._inside_meeting())

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
    def __init__(
        self,
        text="",
        attrs=None,
        children_by_selector=None,
        display_error=False,
        enabled_error=False,
    ):
        self.cleared = False
        self.clicked = False
        self.typed_text = ""
        self.text = text
        self.attrs = attrs or {}
        self.children_by_selector = children_by_selector or {}
        self.display_error = display_error
        self.enabled_error = enabled_error

    def is_displayed(self):
        if self.display_error:
            raise RuntimeError("stale element")
        return True

    def is_enabled(self):
        if self.enabled_error:
            raise RuntimeError("stale element")
        return True

    def clear(self):
        self.cleared = True

    def click(self):
        self.clicked = True

    def send_keys(self, text):
        self.typed_text += text

    def find_elements(self, strategy, selector):
        return self.children_by_selector.get(selector, [])

    def get_attribute(self, name):
        if name == "textContent":
            return self.text
        return self.attrs.get(name)


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

    def get(self, url):
        self.current_url = url
