from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from storage import MeetingStorage

try:
    from selenium.common.exceptions import StaleElementReferenceException
except Exception:  # pragma: no cover - selenium may be absent in lightweight test envs
    class StaleElementReferenceException(Exception):
        pass


LOGGER = logging.getLogger(__name__)

CAPTION_CONTAINER_SELECTORS = [
    'div[jsname="tgaKEf"]',
    "div.a4cQT",
    'div[class*="caption" i]',
    'div[role="region"][aria-label*="caption" i]',
]
SPEAKER_SELECTORS = ['div[jsname="W3Gkyd"]', 'div[class*="zs7s8d"]', 'span[class*="name" i]']
TEXT_SELECTORS = ['span[jsname="dfnsle"]', 'div[jsname="YSg9Nc"] span']


class CaptionScraper:
    def __init__(self, poll_interval_seconds: float = 1.0, flush_interval_seconds: float = 30.0):
        self.poll_interval_seconds = poll_interval_seconds
        self.flush_interval_seconds = flush_interval_seconds
        self._lines: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._errors: list[str] = []
        self._last_speaker = ""
        self._last_selector = ""
        self._buffer_speaker = ""
        self._buffer_text = ""
        self._meeting_start_time: datetime | None = None
        self._stale_dom_polls = 0

    def start(self, driver: Any, output_dir: str | Path, stop_event: threading.Event, meeting_start_time: datetime) -> None:
        self._meeting_start_time = meeting_start_time
        storage = MeetingStorage(Path(output_dir).parent)
        last_flush = time.monotonic()
        last_warning = 0.0
        LOGGER.info("Caption scraper starting: poll_interval_seconds=%s", self.poll_interval_seconds)
        while not stop_event.is_set():
            try:
                caption = self.scrape_current_caption(driver)
                if caption:
                    speaker, text = caption
                    self.ingest_caption(speaker, text, datetime.now(timezone.utc))
                elif time.monotonic() - last_warning > 120:
                    LOGGER.warning("No Google Meet caption DOM found yet; retrying")
                    last_warning = time.monotonic()
            except Exception as exc:
                self._errors.append(str(exc))
                LOGGER.exception("Caption scrape poll failed")
            if time.monotonic() - last_flush >= self.flush_interval_seconds:
                storage.write_transcript_json(output_dir, self.get_lines(), final=False)
                LOGGER.info("Caption transcript flushed: lines=%s", len(self.get_lines()))
                last_flush = time.monotonic()
            stop_event.wait(self.poll_interval_seconds)
        self.commit_buffer(datetime.now(timezone.utc))
        storage.write_transcript_json(output_dir, self.get_lines(), final=False)
        LOGGER.info("Caption scraper stopped: lines=%s errors=%s", len(self.get_lines()), len(self._errors))

    def scrape_current_caption(self, driver: Any) -> tuple[str, str] | None:
        for selector in CAPTION_CONTAINER_SELECTORS:
            try:
                containers = _find_elements(driver, selector)
                visible_containers = [item for item in containers if _is_displayed(item)]
                if not visible_containers:
                    continue
                container = visible_containers[-1]
                speaker, text = self._extract_caption_parts(container)
            except StaleElementReferenceException:
                self._stale_dom_polls += 1
                LOGGER.debug("Google Meet caption DOM went stale during poll; retrying next poll")
                continue
            if text:
                if self._last_selector != selector:
                    LOGGER.info("Google Meet caption selector active: %s", selector)
                self._last_selector = selector
                return speaker or "Unknown", text
        return None

    def ingest_caption(self, speaker: str, text: str, wall_clock: datetime | None = None) -> None:
        normalized_text = " ".join(text.split())
        normalized_speaker = (speaker or "Unknown").strip() or "Unknown"
        if not normalized_text:
            return
        if normalized_speaker == "Unknown":
            embedded_speaker, embedded_text = self._split_embedded_speaker(normalized_text)
            if embedded_speaker and embedded_text:
                normalized_speaker = embedded_speaker
                normalized_text = embedded_text
        if not self._buffer_text:
            self._buffer_speaker = normalized_speaker
            self._buffer_text = normalized_text
            self._last_speaker = normalized_speaker
            return
        lower_text = _caption_prefix_key(normalized_text)
        lower_buffer = _caption_prefix_key(self._buffer_text)
        if normalized_speaker != self._buffer_speaker or not lower_text.startswith(lower_buffer):
            if normalized_speaker == self._buffer_speaker and lower_buffer.startswith(lower_text):
                self._last_speaker = normalized_speaker
                return
            self.commit_buffer(wall_clock or datetime.now(timezone.utc))
            self._buffer_speaker = normalized_speaker
            self._buffer_text = normalized_text
        elif len(normalized_text) > len(self._buffer_text):
            self._buffer_text = normalized_text
        self._last_speaker = normalized_speaker

    def commit_buffer(self, wall_clock: datetime | None = None) -> None:
        if not self._buffer_text:
            return
        now = wall_clock or datetime.now(timezone.utc)
        with self._lock:
            self._lines.append(
                {
                    "index": len(self._lines) + 1,
                    "timestamp": self._elapsed_timestamp(now),
                    "wall_clock": now.isoformat(),
                    "speaker": self._buffer_speaker or "Unknown",
                    "text": self._buffer_text,
                }
            )
        self._buffer_speaker = ""
        self._buffer_text = ""

    def get_lines(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._lines)

    def get_status(self) -> dict[str, Any]:
        return {
            "lines_captured": len(self.get_lines()),
            "last_speaker": self._last_speaker,
            "last_selector": self._last_selector,
            "stale_dom_polls": self._stale_dom_polls,
            "errors": list(self._errors),
        }

    def _extract_caption_parts(self, container: Any) -> tuple[str, str]:
        full_text = _text(container).strip()
        speaker = self._extract_speaker(container)
        if speaker:
            return speaker, self._extract_text(container, speaker)
        speaker, text = self._split_embedded_speaker(full_text)
        if speaker and text:
            return speaker, text
        return "", full_text

    def _extract_speaker(self, container: Any) -> str:
        for selector in SPEAKER_SELECTORS:
            elements = _find_elements(container, selector)
            if elements:
                text = _text(elements[0]).strip()
                if text:
                    return text
        return ""

    def _extract_text(self, container: Any, speaker: str) -> str:
        for selector in TEXT_SELECTORS:
            elements = _find_elements(container, selector)
            text = " ".join(_text(element).strip() for element in elements if _text(element).strip())
            if text:
                return text
        full_text = _text(container).strip()
        if speaker and full_text.startswith(speaker):
            return full_text[len(speaker) :].strip()
        return full_text

    def _split_embedded_speaker(self, text: str) -> tuple[str, str]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) >= 2 and self._looks_like_speaker_name(lines[0]):
            return lines[0], " ".join(lines[1:])
        match = re.match(r"^([A-Z][\w'.-]+\s+[A-Z][\w'.-]+)\s+(.{8,})$", text)
        if match and self._looks_like_speaker_name(match.group(1)):
            return match.group(1), match.group(2).strip()
        return "", text

    def _looks_like_speaker_name(self, value: str) -> bool:
        words = value.split()
        if not 2 <= len(words) <= 4:
            return False
        return all(word[:1].isupper() for word in words)

    def _elapsed_timestamp(self, now: datetime) -> str:
        if not self._meeting_start_time:
            seconds = 0
        else:
            seconds = max(0, int((now - self._meeting_start_time).total_seconds()))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _find_elements(root: Any, selector: str) -> list[Any]:
    try:
        return list(root.find_elements("css selector", selector))
    except TypeError:
        return list(root.find_elements(selector))


def _is_displayed(element: Any) -> bool:
    try:
        return bool(element.is_displayed())
    except Exception:
        return True


def _text(element: Any) -> str:
    try:
        return element.text or ""
    except Exception:
        try:
            return element.get_attribute("textContent") or ""
        except Exception:
            return ""


def _caption_prefix_key(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", "", text.lower()).split())
