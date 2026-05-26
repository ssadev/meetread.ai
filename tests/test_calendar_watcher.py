from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from calendar_watcher import CalendarWatcher, extract_meet_url
from config import Settings, get_settings


class _Events:
    def __init__(self, items):
        self.items = items

    def list(self, **kwargs):
        self.kwargs = kwargs
        return self

    def execute(self):
        return {"items": self.items}


class _Service:
    def __init__(self, items):
        self._events = _Events(items)

    def events(self):
        return self._events


def _event(event_id: str, start: datetime) -> dict:
    return {
        "id": event_id,
        "summary": "Planning",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(minutes=30)).isoformat()},
        "organizer": {"email": "alice@example.com"},
        "attendees": [{"email": "bob@example.com"}],
        "conferenceData": {
            "entryPoints": [
                {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"},
            ]
        },
    }


class CalendarWatcherTests(unittest.TestCase):
    def test_extract_meet_url_from_conference_entry_point(self):
        event = _event("1", datetime.now(timezone.utc))
        self.assertEqual(extract_meet_url(event), "https://meet.google.com/abc-defg-hij")

    def test_poll_once_returns_imminent_unseen_meeting(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            now = datetime(2026, 5, 23, 10, 0, tzinfo=timezone.utc)
            settings = get_settings(tmp_path / "missing.env")
            watcher = CalendarWatcher(
                _Service([_event("evt-1", now + timedelta(minutes=1))]),
                settings=settings,
                seen_path=tmp_path / "seen.json",
            )

            meetings = watcher.poll_once(now)
            self.assertEqual(len(meetings), 1)
            self.assertEqual(meetings[0].calendar_event_id, "evt-1")
            self.assertEqual(meetings[0].attendees, ["bob@example.com"])
            self.assertNotIn("conferenceDataVersion", watcher.service.events().kwargs)
            self.assertEqual(watcher.poll_once(now), [])

    def test_poll_once_lookahead_covers_full_poll_interval(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            now = datetime(2026, 5, 23, 10, 0, tzinfo=timezone.utc)
            base = get_settings(tmp_path / "missing.env")
            settings = Settings(
                **{
                    **base.__dict__,
                    "poll_interval_seconds": 300,
                    "join_buffer_minutes": 2,
                }
            )
            service = _Service([_event("evt-2", now + timedelta(minutes=4))])
            watcher = CalendarWatcher(service, settings=settings, seen_path=tmp_path / "seen.json")

            meetings = watcher.poll_once(now)

            self.assertEqual(len(meetings), 1)
            self.assertEqual(meetings[0].calendar_event_id, "evt-2")
