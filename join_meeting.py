#!/usr/bin/env python3
"""Manually trigger the bot to join a Google Meet by URL.

Usage:
    python join_meeting.py <meet_url> [options]

Examples:
    python join_meeting.py "https://meet.google.com/abc-defg-hij"
    python join_meeting.py "https://meet.google.com/abc-defg-hij" --title "Weekly Sync"
    python join_meeting.py "https://meet.google.com/abc-defg-hij" --duration-hours 2
    python join_meeting.py "https://meet.google.com/abc-defg-hij" --end-time "2026-05-27T15:00:00+05:30"

Docker exec:
    docker exec <container> python join_meeting.py "https://meet.google.com/abc-defg-hij"
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from config import SETTINGS
from meet_bot import MeetBot


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger(__name__)

MEET_URL_RE = re.compile(r"https://meet\.google\.com/[a-z0-9]+-[a-z0-9]+-[a-z0-9]+", re.IGNORECASE)


@dataclass
class ManualMeeting:
    meet_url: str
    meeting_title: str
    start_time: datetime
    end_time: datetime
    calendar_event_id: str = ""
    attendees: list[str] = field(default_factory=list)
    organizer: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Join a Google Meet by URL (same bot flow as calendar-triggered meetings)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("meet_url", help="Google Meet URL (https://meet.google.com/xxx-xxxx-xxx)")
    parser.add_argument("--title", default="Manual Meeting", help="Meeting title for storage/logs (default: 'Manual Meeting')")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--duration-hours",
        type=float,
        default=None,
        help="How many hours to stay (default: MAX_MEETING_DURATION_HOURS from env)",
    )
    group.add_argument(
        "--end-time",
        default=None,
        help="Explicit end time in ISO 8601 format (e.g. 2026-05-27T15:00:00+05:30)",
    )
    return parser.parse_args()


def validate_meet_url(url: str) -> str:
    url = url.strip()
    if not MEET_URL_RE.match(url):
        raise ValueError(
            f"Invalid Google Meet URL: {url!r}\n"
            "Expected format: https://meet.google.com/xxx-xxxx-xxx"
        )
    return url


def resolve_end_time(args: argparse.Namespace, start: datetime) -> datetime:
    if args.end_time:
        raw = args.end_time
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        end = datetime.fromisoformat(raw)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return end.astimezone(timezone.utc)
    hours = args.duration_hours if args.duration_hours is not None else SETTINGS.max_meeting_duration_hours
    return start + timedelta(hours=hours)


def main() -> int:
    args = parse_args()

    try:
        meet_url = validate_meet_url(args.meet_url)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    now = datetime.now(timezone.utc)
    end_time = resolve_end_time(args, now)

    meeting = ManualMeeting(
        meet_url=meet_url,
        meeting_title=args.title,
        start_time=now,
        end_time=end_time,
    )

    LOGGER.info(
        "Manual join triggered: url=%s title=%r duration_until=%s",
        meet_url,
        args.title,
        end_time.isoformat(),
    )

    try:
        result = MeetBot(settings=SETTINGS).run(meeting)
    except Exception:
        LOGGER.exception("MeetBot raised an unhandled exception")
        return 2

    status = result.get("status", "unknown")
    LOGGER.info(
        "Manual join finished: status=%s duration_seconds=%s transcript_lines=%s",
        status,
        result.get("duration_seconds"),
        result.get("total_lines"),
    )
    return 0 if status in {"completed", "partial"} else 1


if __name__ == "__main__":
    sys.exit(main())
