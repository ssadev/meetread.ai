from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_title(title: str, max_length: int = 80) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", title).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return (cleaned or "untitled_meeting")[:max_length].strip("._-") or "untitled_meeting"


def meeting_folder_name(title: str, start_time: datetime | None = None) -> str:
    when = start_time or datetime.now()
    return f"{when:%Y-%m-%d}_{sanitize_title(title)}"


class MeetingStorage:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def create_meeting_dir(self, title: str, start_time: datetime | None = None) -> Path:
        root = self.base_dir / meeting_folder_name(title, start_time)
        candidate = root
        index = 2
        while candidate.exists():
            candidate = Path(f"{root}_{index}")
            index += 1
        candidate.mkdir(parents=True)
        (candidate / "audio_chunks").mkdir()
        return candidate

    def initial_metadata(self, meeting: Any, meeting_id: str | None = None) -> dict[str, Any]:
        return {
            "meeting_id": meeting_id or str(uuid4()),
            "title": getattr(meeting, "meeting_title", getattr(meeting, "title", "")),
            "meet_url": getattr(meeting, "meet_url", ""),
            "calendar_event_id": getattr(meeting, "calendar_event_id", getattr(meeting, "event_id", "")),
            "start_time": _iso(getattr(meeting, "start_time", None)),
            "end_time": _iso(getattr(meeting, "end_time", None)),
            "actual_join_time": None,
            "actual_leave_time": None,
            "duration_seconds": 0,
            "attendees": list(getattr(meeting, "attendees", []) or []),
            "organizer": getattr(meeting, "organizer", ""),
            "audio_file": None,
            "audio_duration_seconds": 0,
            "audio_silent_chunks": 0,
            "transcript_file": None,
            "total_lines": 0,
            "speakers_detected": [],
            "status": "in_progress",
        }

    def write_metadata(self, meeting_dir: str | Path, metadata: dict[str, Any]) -> Path:
        path = Path(meeting_dir) / "metadata.json"
        _atomic_write_json(path, metadata)
        return path

    def update_metadata(self, meeting_dir: str | Path, **updates: Any) -> dict[str, Any]:
        path = Path(meeting_dir) / "metadata.json"
        metadata = self.read_metadata(meeting_dir)
        metadata.update(updates)
        _atomic_write_json(path, metadata)
        return metadata

    def read_metadata(self, meeting_dir: str | Path) -> dict[str, Any]:
        path = Path(meeting_dir) / "metadata.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def write_transcript_json(self, meeting_dir: str | Path, lines: list[dict[str, Any]], final: bool) -> Path:
        filename = "transcript_final.json" if final else "transcript_live.json"
        path = Path(meeting_dir) / filename
        _atomic_write_json(path, lines)
        return path

    def write_transcript_text(self, meeting_dir: str | Path, lines: Iterable[dict[str, Any]]) -> Path:
        path = Path(meeting_dir) / "transcript_final.txt"
        content = "\n".join(
            f"[{line.get('timestamp', '00:00:00')}] {line.get('speaker', 'Unknown')}: {line.get('text', '')}"
            for line in lines
            if line.get("text")
        )
        path.write_text(content + ("\n" if content else ""), encoding="utf-8")
        return path

    def finalize_transcripts(self, meeting_dir: str | Path, lines: list[dict[str, Any]]) -> dict[str, Any]:
        final_json = self.write_transcript_json(meeting_dir, lines, final=True)
        self.write_transcript_text(meeting_dir, lines)
        speakers = sorted({line.get("speaker", "Unknown") for line in lines if line.get("speaker")})
        return {"transcript_file": final_json.name, "total_lines": len(lines), "speakers_detected": speakers}

    def write_meeting_intelligence(
        self,
        meeting_dir: str | Path,
        result: dict[str, Any],
        markdown: str,
    ) -> dict[str, Any]:
        json_path = Path(meeting_dir) / "meeting_intelligence.json"
        md_path = Path(meeting_dir) / "meeting_intelligence.md"
        _atomic_write_json(json_path, result)
        md_path.write_text(markdown, encoding="utf-8")
        return {
            "meeting_intelligence_file": json_path.name,
            "meeting_intelligence_markdown_file": md_path.name,
            "meeting_intelligence_provider": result.get("provider"),
        }

    def setup_meeting_logger(self, meeting_dir: str | Path, name: str) -> logging.Logger:
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        file_handler = logging.FileHandler(Path(meeting_dir) / "bot.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
        for child_name in ("audio_recorder", "caption_scraper"):
            self._attach_child_file_logger(child_name, file_handler, formatter)
        return logger

    def _attach_child_file_logger(
        self,
        name: str,
        file_handler: logging.Handler,
        formatter: logging.Formatter,
    ) -> None:
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        for handler in list(logger.handlers):
            if getattr(handler, "_meet_bot_child_handler", False):
                logger.removeHandler(handler)
                handler.close()
        child_handler = logging.FileHandler(file_handler.baseFilename, encoding="utf-8")
        child_handler.setFormatter(formatter)
        child_handler._meet_bot_child_handler = True  # type: ignore[attr-defined]
        logger.addHandler(child_handler)

    def remove_chunks_dir(self, meeting_dir: str | Path) -> None:
        chunks_dir = Path(meeting_dir) / "audio_chunks"
        if chunks_dir.exists():
            shutil.rmtree(chunks_dir)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)
