from __future__ import annotations

import json
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from storage import MeetingStorage, sanitize_title


class StorageTests(unittest.TestCase):
    def test_sanitize_title_is_filesystem_safe(self):
        self.assertEqual(sanitize_title("Q3 Planning / Sync: East?"), "Q3_Planning_Sync_East")

    def test_finalize_transcripts_writes_json_and_text(self):
        with TemporaryDirectory() as tmp:
            storage = MeetingStorage(tmp)
            meeting = SimpleNamespace(
                meeting_title="Q3 Planning",
                meet_url="https://meet.google.com/abc-defg-hij",
                calendar_event_id="evt-1",
                start_time=datetime(2026, 5, 23, tzinfo=timezone.utc),
                end_time=datetime(2026, 5, 23, 1, tzinfo=timezone.utc),
                attendees=["bob@example.com"],
                organizer="alice@example.com",
            )
            meeting_dir = storage.create_meeting_dir(meeting.meeting_title, meeting.start_time)
            storage.write_metadata(meeting_dir, storage.initial_metadata(meeting, meeting_id="m1"))
            updates = storage.finalize_transcripts(
                meeting_dir,
                [{"index": 1, "timestamp": "00:00:01", "speaker": "Alice", "text": "Hi"}],
            )

            self.assertEqual(updates["total_lines"], 1)
            self.assertEqual(json.loads((meeting_dir / "transcript_final.json").read_text())[0]["text"], "Hi")
            self.assertEqual((meeting_dir / "transcript_final.txt").read_text(), "[00:00:01] Alice: Hi\n")

    def test_write_meeting_intelligence_writes_json_and_markdown(self):
        with TemporaryDirectory() as tmp:
            storage = MeetingStorage(tmp)
            meeting_dir = storage.create_meeting_dir("Q3 Planning")

            updates = storage.write_meeting_intelligence(
                meeting_dir,
                {"provider": "rule_based", "summary": "Summary"},
                "# Summary\n",
            )

            self.assertEqual(updates["meeting_intelligence_file"], "meeting_intelligence.json")
            self.assertEqual(updates["meeting_intelligence_provider"], "rule_based")
            self.assertEqual(
                json.loads((meeting_dir / "meeting_intelligence.json").read_text())["summary"],
                "Summary",
            )
            self.assertEqual((meeting_dir / "meeting_intelligence.md").read_text(), "# Summary\n")
