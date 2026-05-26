from __future__ import annotations

import json
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from meeting_intelligence import (
    LLMMeetingIntelligenceProvider,
    LLMSettings,
    RuleBasedMeetingIntelligenceProvider,
    create_meeting_intelligence_provider,
    normalize_transcript_lines,
    _openai_compatible_chat,
    regenerate_meeting_intelligence,
    render_intelligence_markdown,
)
from storage import MeetingStorage


class MeetingIntelligenceTests(unittest.TestCase):
    def test_rule_based_provider_extracts_core_sections(self):
        provider = RuleBasedMeetingIntelligenceProvider()
        result = provider.analyze(
            [
                {
                    "index": 1,
                    "timestamp": "00:01:00",
                    "speaker": "Alice",
                    "text": "We decided to launch the beta next week for the customer pilot.",
                },
                {
                    "index": 2,
                    "timestamp": "00:02:00",
                    "speaker": "Bob",
                    "text": "Bob will prepare the launch checklist by Friday.",
                },
                {
                    "index": 3,
                    "timestamp": "00:03:00",
                    "speaker": "Alice",
                    "text": "The main risk is that legal approval might block the timeline.",
                },
                {
                    "index": 4,
                    "timestamp": "00:04:00",
                    "speaker": "Carol",
                    "text": "Can we confirm the rollout owner?",
                },
            ],
            {"meeting_id": "m1", "title": "Product Sync", "transcript_file": "transcript_final.json"},
        )

        self.assertEqual(result["provider"], "rule_based")
        self.assertIn("Product Sync", result["summary"])
        self.assertEqual(result["decisions"][0]["source_timestamp"], "00:01:00")
        self.assertEqual(result["action_items"][0]["owner"], "Bob")
        self.assertEqual(result["action_items"][0]["due_date"], "by Friday")
        self.assertEqual(result["risks"][0]["source_timestamp"], "00:03:00")
        self.assertEqual(result["questions"][0]["source_speaker"], "Carol")
        self.assertGreater(result["confidence"], 0)

    def test_empty_transcript_gets_low_confidence_summary(self):
        result = RuleBasedMeetingIntelligenceProvider().analyze([], {"title": "Empty Meeting"})

        self.assertEqual(result["confidence"], 0.0)
        self.assertEqual(result["action_items"], [])
        self.assertIn("no captured transcript", result["summary"])

    def test_provider_factory_rejects_unknown_provider(self):
        with self.assertRaises(ValueError):
            create_meeting_intelligence_provider("ollama")

    def test_markdown_renders_detected_sections(self):
        result = RuleBasedMeetingIntelligenceProvider().analyze(
            [
                {
                    "index": 1,
                    "timestamp": "00:01:00",
                    "speaker": "Alice",
                    "text": "Action item: follow up with the design team tomorrow.",
                }
            ],
            {"title": "Design Sync"},
        )

        markdown = render_intelligence_markdown(result)

        self.assertIn("# Design Sync", markdown)
        self.assertIn("## Action Items", markdown)
        self.assertIn("follow up with the design team", markdown)

    def test_incremental_caption_lines_are_normalized_before_analysis(self):
        transcript = [
            {
                "index": 1,
                "timestamp": "00:00:33",
                "speaker": "Sarfaraz Ahamed",
                "text": "The US market moved up because interest rates were cut and liquidity improved.",
            },
            {
                "index": 2,
                "timestamp": "00:03:20",
                "speaker": "Sarfaraz Ahamed",
                "text": (
                    "The US market moved up because interest rates were cut and liquidity improved. "
                    "The Indian market did not move as strongly because INR weakness and FII selling acted as a drag."
                ),
            },
            {
                "index": 3,
                "timestamp": "00:03:40",
                "speaker": "Sarfaraz Ahamed",
                "text": (
                    "The US market moved up because interest rates were cut and liquidity improved. "
                    "The Indian market did not move as strongly because INR weakness and FII selling acted as a drag. "
                    "The prediction is that another interest rate cut can support a short-term swing."
                ),
            },
        ]

        segments = normalize_transcript_lines(transcript)
        result = RuleBasedMeetingIntelligenceProvider().analyze(transcript, {"title": "245pm"})
        markdown = render_intelligence_markdown(result)

        self.assertLess(len(segments), len(transcript))
        self.assertTrue(result["source"]["normalization_applied"])
        self.assertEqual(result["source"]["raw_total_lines"], 3)
        self.assertEqual(result["action_items"], [])
        self.assertNotIn("this:", markdown.lower())
        self.assertIn("Interest Rates", markdown)

    def test_long_monologue_does_not_create_false_action_item_owner(self):
        text = (
            "This will happen soon enough because interest rates are likely to move lower. "
            "This can support the US market, but the Indian market may still be affected by INR weakness. "
            "This will not be a clean entry point for every stock."
        )

        result = RuleBasedMeetingIntelligenceProvider().analyze(
            [{"index": 1, "timestamp": "00:01:00", "speaker": "Alice", "text": text}],
            {"title": "Market View"},
        )

        self.assertEqual(result["action_items"], [])

    def test_regenerate_meeting_intelligence_updates_existing_meeting_folder(self):
        with TemporaryDirectory() as tmp:
            storage = MeetingStorage(tmp)
            meeting_dir = storage.create_meeting_dir("Product Sync")
            storage.write_metadata(
                meeting_dir,
                {
                    "meeting_id": "m1",
                    "title": "Product Sync",
                    "transcript_file": "transcript_final.json",
                    "meeting_intelligence_provider": "rule_based",
                },
            )
            storage.write_transcript_json(
                meeting_dir,
                [
                    {
                        "index": 1,
                        "timestamp": "00:01:00",
                        "speaker": "Bob",
                        "text": "Bob will prepare the launch checklist by Friday.",
                    }
                ],
                final=True,
            )

            result = regenerate_meeting_intelligence(meeting_dir)
            metadata = json.loads((meeting_dir / "metadata.json").read_text())

            self.assertEqual(result["action_items"][0]["owner"], "Bob")
            self.assertEqual(metadata["meeting_intelligence_status"], "completed")
            self.assertTrue((meeting_dir / "meeting_intelligence.md").exists())

    def test_llm_provider_returns_normalized_json_contract(self):
        def fake_chat(messages, settings):
            return json.dumps(
                {
                    "summary": "The team reviewed the launch.",
                    "key_points": [{"text": "Launch is close.", "source_timestamp": "00:01:00"}],
                    "decisions": [],
                    "risks": [],
                    "action_items": [
                        {
                            "task": "Prepare launch checklist",
                            "owner": "Bob",
                            "due_date": "by Friday",
                            "source_timestamp": "00:02:00",
                            "source_speaker": "Bob",
                            "source_text": "Bob will prepare the launch checklist by Friday.",
                            "confidence": 0.9,
                        }
                    ],
                    "questions": [],
                    "blockers": [],
                    "topics": [{"title": "Launch", "summary": "Launch planning"}],
                    "confidence": 0.88,
                }
            )

        provider = LLMMeetingIntelligenceProvider(LLMSettings(model="test-model"), chat_client=fake_chat)
        result = provider.analyze(
            [
                {
                    "index": 1,
                    "timestamp": "00:02:00",
                    "speaker": "Bob",
                    "text": "Bob will prepare the launch checklist by Friday.",
                }
            ],
            {"meeting_id": "m1", "title": "Launch Sync", "transcript_file": "transcript_final.json"},
        )

        self.assertEqual(result["provider"], "llm")
        self.assertEqual(result["llm_model"], "test-model")
        self.assertEqual(result["action_items"][0]["owner"], "Bob")
        self.assertEqual(result["source"]["llm_chunk_count"], 1)

    def test_llm_provider_retries_invalid_json_once(self):
        calls = []

        def fake_chat(messages, settings):
            calls.append(messages)
            if len(calls) == 1:
                return "not json"
            return json.dumps(
                {
                    "summary": "Recovered summary.",
                    "key_points": [],
                    "decisions": [],
                    "risks": [],
                    "action_items": [],
                    "questions": [],
                    "blockers": [],
                    "topics": [],
                    "confidence": 0.7,
                }
            )

        provider = LLMMeetingIntelligenceProvider(LLMSettings(), chat_client=fake_chat)
        result = provider.analyze(
            [{"index": 1, "timestamp": "00:01:00", "speaker": "Alice", "text": "Status update."}],
            {"title": "Status"},
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(result["summary"], "Recovered summary.")

    def test_llm_provider_uses_reduce_flow_for_long_transcripts(self):
        calls = []

        def fake_chat(messages, settings):
            calls.append(messages)
            return json.dumps(
                {
                    "summary": "Chunk summary.",
                    "key_points": [],
                    "decisions": [],
                    "risks": [],
                    "action_items": [],
                    "questions": [],
                    "blockers": [],
                    "topics": [],
                    "confidence": 0.6,
                }
            )

        provider = LLMMeetingIntelligenceProvider(
            LLMSettings(max_input_chars=1000),
            chat_client=fake_chat,
        )
        transcript = [
            {"index": index, "timestamp": f"00:0{index}:00", "speaker": "Alice", "text": "Launch discussion. " * 80}
            for index in range(1, 4)
        ]

        result = provider.analyze(transcript, {"title": "Long Meeting"})

        self.assertGreater(len(calls), 1)
        self.assertGreater(result["source"]["llm_chunk_count"], 1)

    def test_provider_factory_creates_llm_provider_from_settings(self):
        settings = SimpleNamespace(
            meeting_llm_provider="openai_compatible",
            meeting_llm_base_url="http://localhost:11434/v1",
            meeting_llm_api_key="",
            meeting_llm_model="llama3.1",
            meeting_llm_temperature=0.1,
            meeting_llm_timeout_seconds=5,
            meeting_llm_max_input_chars=1234,
            meeting_llm_response_format="text",
        )

        provider = create_meeting_intelligence_provider("llm", settings=settings)

        self.assertIsInstance(provider, LLMMeetingIntelligenceProvider)
        self.assertEqual(provider.settings.max_input_chars, 1234)
        self.assertEqual(provider.settings.response_format, "text")

    def test_openai_compatible_request_uses_json_schema_by_default(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()

        def fake_urlopen(request, timeout):
            captured["body"] = json.loads(request.data.decode())
            return FakeResponse()

        with patch("meeting_intelligence.urllib.request.urlopen", fake_urlopen):
            _openai_compatible_chat([{"role": "user", "content": "hello"}], LLMSettings())

        self.assertEqual(captured["body"]["response_format"]["type"], "json_schema")
        self.assertEqual(captured["body"]["response_format"]["json_schema"]["name"], "meeting_intelligence")

    def test_openai_compatible_request_can_use_text_response_format(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()

        def fake_urlopen(request, timeout):
            captured["body"] = json.loads(request.data.decode())
            return FakeResponse()

        with patch("meeting_intelligence.urllib.request.urlopen", fake_urlopen):
            _openai_compatible_chat(
                [{"role": "user", "content": "hello"}],
                LLMSettings(response_format="text"),
            )

        self.assertEqual(captured["body"]["response_format"], {"type": "text"})

    def test_openai_compatible_reads_reasoning_content_when_content_is_empty(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": "",
                                    "reasoning_content": json.dumps({"summary": "Reasoning JSON"}),
                                }
                            }
                        ]
                    }
                ).encode()

        with patch("meeting_intelligence.urllib.request.urlopen", lambda request, timeout: FakeResponse()):
            response = _openai_compatible_chat([{"role": "user", "content": "hello"}], LLMSettings())

        self.assertEqual(json.loads(response)["summary"], "Reasoning JSON")


if __name__ == "__main__":
    unittest.main()
