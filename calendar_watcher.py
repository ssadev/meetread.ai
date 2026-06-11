from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from google.auth.exceptions import TransportError
from httplib2 import HttpLib2Error

from config import SETTINGS, Settings


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CalendarMeeting:
    calendar_event_id: str
    meeting_title: str
    meet_url: str
    start_time: datetime
    end_time: datetime
    organizer: str
    attendees: list[str]


class CalendarWatcher:
    def __init__(
        self,
        calendar_service: Any,
        settings: Settings = SETTINGS,
        seen_path: str | Path | None = None,
        calendar_id: str = "primary",
    ):
        self.service = calendar_service
        self.settings = settings
        self.seen_path = Path(seen_path or settings.seen_meetings_path)
        self.calendar_id = calendar_id
        self._seen = self._load_seen()

    def poll_once(self, now: datetime | None = None) -> list[CalendarMeeting]:
        now = now or datetime.now(timezone.utc)
        lookahead_seconds = max(
            self.settings.join_buffer_minutes * 60,
            self.settings.poll_interval_seconds + 5,
        )
        horizon = now + timedelta(seconds=lookahead_seconds)
        events = self._fetch_events(now, horizon)
        meetings: list[CalendarMeeting] = []
        for event in events:
            event_id = event.get("id")
            if not event_id or event_id in self._seen:
                continue
            meeting = self.parse_event(event)
            if not meeting:
                continue
            if now <= meeting.start_time <= horizon:
                meetings.append(meeting)
                self._seen.add(event_id)
        if meetings:
            self._save_seen()
        return meetings

    def run_forever(self, on_meeting: Callable[[CalendarMeeting], None], stop_event) -> None:
        while not stop_event.is_set():
            try:
                for meeting in self.poll_once():
                    on_meeting(meeting)
            except (TransportError, HttpLib2Error, OSError) as exc:
                LOGGER.warning(
                    "Calendar polling failed (network error); retrying in %ds: %s",
                    self.settings.poll_interval_seconds,
                    exc,
                )
            except Exception:
                LOGGER.exception("Calendar polling failed; retrying on next interval")
            stop_event.wait(self.settings.poll_interval_seconds)

    def parse_event(self, event: dict[str, Any]) -> CalendarMeeting | None:
        meet_url = extract_meet_url(event)
        if not meet_url:
            return None
        start = parse_google_time(event.get("start", {}))
        end = parse_google_time(event.get("end", {}))
        if not start:
            return None
        if not end:
            end = start + timedelta(hours=1)
        organizer = (event.get("organizer") or {}).get("email", "")
        attendees = [item.get("email", "") for item in event.get("attendees", []) if item.get("email")]
        return CalendarMeeting(
            calendar_event_id=event["id"],
            meeting_title=event.get("summary") or "Untitled meeting",
            meet_url=meet_url,
            start_time=start,
            end_time=end,
            organizer=organizer,
            attendees=attendees,
        )

    def _fetch_events(self, now: datetime, horizon: datetime) -> list[dict[str, Any]]:
        response = (
            self.service.events()
            .list(
                calendarId=self.calendar_id,
                timeMin=now.isoformat(),
                timeMax=horizon.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        return response.get("items", [])

    def _load_seen(self) -> set[str]:
        if not self.seen_path.exists():
            return set()
        try:
            return set(json.loads(self.seen_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Could not read seen meetings file; starting with empty set")
            return set()

    def _save_seen(self) -> None:
        self.seen_path.parent.mkdir(parents=True, exist_ok=True)
        self.seen_path.write_text(json.dumps(sorted(self._seen), indent=2), encoding="utf-8")


def extract_meet_url(event: dict[str, Any]) -> str | None:
    conference_data = event.get("conferenceData") or {}
    for entry in conference_data.get("entryPoints", []) or []:
        if entry.get("entryPointType") == "video" and "meet.google.com" in entry.get("uri", ""):
            return entry["uri"]
    hangout_link = event.get("hangoutLink")
    if hangout_link and "meet.google.com" in hangout_link:
        return hangout_link
    return None


def parse_google_time(value: dict[str, str]) -> datetime | None:
    raw = value.get("dateTime") or value.get("date")
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def sleep_until_next_poll(seconds: int) -> None:
    time.sleep(seconds)
