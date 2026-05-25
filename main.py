from __future__ import annotations

import logging
import signal
import threading
from concurrent.futures import ThreadPoolExecutor

from auth import get_calendar_service
from calendar_watcher import CalendarMeeting, CalendarWatcher
from config import SETTINGS
from meet_bot import MeetBot


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger(__name__)


class MeetingOrchestrator:
    def __init__(self):
        self.settings = SETTINGS
        self.stop_event = threading.Event()
        self.active_meetings: set[str] = set()
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=8)

    def start(self) -> None:
        service = get_calendar_service()
        watcher = CalendarWatcher(service, settings=self.settings)
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        LOGGER.info("Google Meet bot daemon started")
        watcher.run_forever(self.enqueue_meeting, self.stop_event)
        self.executor.shutdown(wait=True)

    def enqueue_meeting(self, meeting: CalendarMeeting) -> None:
        with self.lock:
            if meeting.calendar_event_id in self.active_meetings:
                LOGGER.info("Skipping duplicate meeting: %s", meeting.calendar_event_id)
                return
            self.active_meetings.add(meeting.calendar_event_id)
        self.executor.submit(self._run_meeting, meeting)

    def _run_meeting(self, meeting: CalendarMeeting) -> None:
        try:
            LOGGER.info("Starting bot for meeting %s", meeting.meeting_title)
            result = MeetBot(settings=self.settings).run(meeting)
            LOGGER.info("Meeting finished: %s status=%s", meeting.meeting_title, result.get("status"))
        except Exception:
            LOGGER.exception("Meeting worker failed: %s", meeting.meeting_title)
        finally:
            with self.lock:
                self.active_meetings.discard(meeting.calendar_event_id)

    def _handle_signal(self, signum, frame) -> None:  # noqa: ANN001
        LOGGER.info("Received signal %s; shutting down", signum)
        self.stop_event.set()


if __name__ == "__main__":
    MeetingOrchestrator().start()
