from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol


DEFAULT_PROVIDER = "rule_based"
DEFAULT_LLM_PROVIDER = "openai_compatible"
OPENAI_COMPATIBLE_PROVIDERS = {DEFAULT_LLM_PROVIDER, "sarvam"}


class MeetingIntelligenceProvider(Protocol):
    name: str

    def analyze(self, transcript_lines: list[dict[str, Any]], metadata: dict[str, Any]) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class RuleBasedMeetingIntelligenceProvider:
    name: str = DEFAULT_PROVIDER

    def analyze(self, transcript_lines: list[dict[str, Any]], metadata: dict[str, Any]) -> dict[str, Any]:
        raw_lines = [_normalize_line(line) for line in transcript_lines if _line_text(line)]
        lines = normalize_transcript_lines(transcript_lines)
        speakers = sorted({line["speaker"] for line in lines if line["speaker"] and line["speaker"] != "Unknown"})
        key_points = _extract_key_points(lines)
        decisions = _extract_tagged_items(lines, DECISION_PATTERNS, confidence=0.72)
        risks = _extract_tagged_items(lines, RISK_PATTERNS, confidence=0.68)
        questions = _extract_questions(lines)
        blockers = _extract_tagged_items(lines, BLOCKER_PATTERNS, confidence=0.7)
        action_items = _extract_action_items(lines)
        topics = _segment_topics(lines)

        return {
            "schema_version": 1,
            "provider": self.name,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "meeting_id": metadata.get("meeting_id"),
            "title": metadata.get("title", ""),
            "summary": _build_summary(metadata, lines, speakers, key_points, decisions, action_items, topics),
            "key_points": key_points,
            "decisions": decisions,
            "risks": risks,
            "action_items": action_items,
            "questions": questions,
            "blockers": blockers,
            "topics": topics,
            "confidence": _overall_confidence(lines, action_items, decisions, key_points),
            "source": {
                "transcript_file": metadata.get("transcript_file", "transcript_final.json"),
                "total_lines": len(lines),
                "raw_total_lines": len(raw_lines),
                "normalized_segment_count": len(lines),
                "normalization_applied": len(lines) != len(raw_lines),
                "speakers_detected": speakers,
            },
        }


@dataclass(frozen=True)
class LLMSettings:
    provider: str = DEFAULT_LLM_PROVIDER
    base_url: str = "http://localhost:11434/v1"
    api_key: str = ""
    model: str = "llama3.1"
    temperature: float = 0.2
    timeout_seconds: int = 120
    max_input_chars: int = 60000
    response_format: str = "json_schema"
    reasoning_effort: str | None = None


ChatClient = Callable[[list[dict[str, str]], LLMSettings], str]


@dataclass(frozen=True)
class LLMMeetingIntelligenceProvider:
    settings: LLMSettings
    chat_client: ChatClient | None = None
    name: str = "llm"

    def analyze(self, transcript_lines: list[dict[str, Any]], metadata: dict[str, Any]) -> dict[str, Any]:
        lines = normalize_transcript_lines(transcript_lines)
        raw_lines = [_normalize_line(line) for line in transcript_lines if _line_text(line)]
        chunks = _chunk_lines_for_llm(lines, self.settings.max_input_chars)
        partials = [
            self._analyze_chunk(chunk, metadata, part_index=index + 1, total_parts=len(chunks))
            for index, chunk in enumerate(chunks)
        ]
        total_parts = len(partials)
        if total_parts == 1:
            result = partials[0]
        else:
            result = self._reduce_partials(partials, metadata)
        return _finalize_llm_result(
            result,
            metadata=metadata,
            settings=self.settings,
            raw_total_lines=len(raw_lines),
            normalized_segment_count=len(lines),
            normalization_applied=len(lines) != len(raw_lines),
            chunk_count=total_parts,
        )

    def _analyze_chunk(
        self,
        lines: list[dict[str, Any]],
        metadata: dict[str, Any],
        part_index: int,
        total_parts: int,
    ) -> dict[str, Any]:
        payload = {
            "meeting": _meeting_prompt_metadata(metadata),
            "part_index": part_index,
            "total_parts": total_parts or 1,
            "transcript": [_line_for_prompt(line) for line in lines],
        }
        return self._complete_json(
            [
                {"role": "system", "content": LLM_SYSTEM_PROMPT},
                {"role": "user", "content": f"Analyze this meeting transcript JSON:\n{json.dumps(payload, ensure_ascii=False)}"},
            ]
        )

    def _reduce_partials(self, partials: list[dict[str, Any]], metadata: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "meeting": _meeting_prompt_metadata(metadata),
            "partial_analyses": partials,
        }
        return self._complete_json(
            [
                {"role": "system", "content": LLM_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "Merge these partial meeting analyses into one final JSON object:\n"
                    + json.dumps(payload, ensure_ascii=False),
                },
            ]
        )

    def _complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        content = self._chat(messages)
        try:
            return _parse_json_object(content)
        except ValueError:
            repair_messages = messages + [
                {
                    "role": "user",
                    "content": "Your previous response was not valid JSON. Return only one valid JSON object matching the requested schema.",
                }
            ]
            return _parse_json_object(self._chat(repair_messages))

    def _chat(self, messages: list[dict[str, str]]) -> str:
        client = self.chat_client or _openai_compatible_chat
        return client(messages, self.settings)


def normalize_transcript_lines(transcript_lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw_lines = [_normalize_line(line) for line in transcript_lines if _line_text(line)]
    merged: list[dict[str, Any]] = []
    for line in raw_lines:
        if merged and _is_caption_extension(merged[-1], line):
            previous = merged[-1]
            previous["text"] = line["text"]
            previous["end_timestamp"] = line["timestamp"]
            previous["source_indices"].extend(line["source_indices"])
            previous["source_line_count"] += 1
            continue
        merged.append(line)

    segments: list[dict[str, Any]] = []
    for line in merged:
        chunks = _split_text_into_chunks(line["text"])
        for chunk_index, chunk in enumerate(chunks):
            segment = dict(line)
            segment["text"] = chunk
            segment["segment_index"] = len(segments) + 1
            segment["chunk_index"] = chunk_index + 1
            segment["chunk_count"] = len(chunks)
            segments.append(segment)
    return segments


def create_meeting_intelligence_provider(provider_name: str | None = None, settings: Any = None) -> MeetingIntelligenceProvider:
    provider = (provider_name or DEFAULT_PROVIDER).strip().lower().replace("-", "_")
    if provider in {"", DEFAULT_PROVIDER, "rules", "local_rules"}:
        return RuleBasedMeetingIntelligenceProvider()
    if provider == "llm":
        return LLMMeetingIntelligenceProvider(_llm_settings_from(settings))
    raise ValueError(f"Unsupported meeting intelligence provider: {provider_name}")


def render_intelligence_markdown(result: dict[str, Any]) -> str:
    sections = [
        f"# {result.get('title') or 'Meeting Intelligence'}",
        "",
        "## Summary",
        result.get("summary") or "No summary generated.",
        "",
        _render_items("Key Points", result.get("key_points", []), "text"),
        _render_items("Decisions", result.get("decisions", []), "text"),
        _render_action_items(result.get("action_items", [])),
        _render_items("Risks", result.get("risks", []), "text"),
        _render_items("Questions", result.get("questions", []), "text"),
        _render_items("Blockers", result.get("blockers", []), "text"),
        _render_topics(result.get("topics", [])),
    ]
    return "\n".join(section.rstrip() for section in sections if section is not None).strip() + "\n"


DECISION_PATTERNS = [
    re.compile(r"\b(we decided|decided|decision is|agreed|we agreed|approved|we will go with)\b", re.I),
]
RISK_PATTERNS = [
    re.compile(r"\b(risk|concern|concerned|problem|issue|might fail|could fail|blocked by|dependency)\b", re.I),
]
BLOCKER_PATTERNS = [
    re.compile(r"\b(blocked|blocker|stuck|waiting on|cannot proceed|can't proceed|dependency)\b", re.I),
]
ACTION_PATTERNS = [
    re.compile(r"\b(?P<owner>[A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){0,2})\s+(?:will|should|needs to|need to)\s+(?P<task>.{6,})"),
    re.compile(r"\b(?:action item|todo|follow up|follow-up):?\s+(?P<task>.{6,})", re.I),
    re.compile(r"\b(?:i|we)\s+(?:will|should|need to|needs to)\s+(?P<task>.{6,})", re.I),
]
DUE_DATE_PATTERN = re.compile(
    r"\b(today|tomorrow|this week|next week|by (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|eod|end of day|next week|this week)|(?:on|by) \w+ \d{1,2})\b",
    re.I,
)
LLM_SYSTEM_PROMPT = """You generate concise meeting intelligence from transcript segments.
Return only valid JSON with these keys: summary, key_points, decisions, risks, action_items, questions, blockers, topics, confidence.
Use only the transcript evidence. Do not invent owners, due dates, decisions, or facts.
Every list item must include source_timestamp, source_speaker, text, and confidence when applicable.
Action items must include task, owner, due_date, source_timestamp, source_speaker, source_text, and confidence.
Use null for unknown owner or due_date. Keep all text concise and readable."""
MEETING_INTELLIGENCE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "summary": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "decisions": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "risks": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "action_items": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "questions": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "blockers": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "topics": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "confidence": {"type": "number"},
    },
    "required": [
        "summary",
        "key_points",
        "decisions",
        "risks",
        "action_items",
        "questions",
        "blockers",
        "topics",
        "confidence",
    ],
}


def _llm_settings_from(settings: Any) -> LLMSettings:
    if isinstance(settings, LLMSettings):
        return settings
    return LLMSettings(
        provider=getattr(settings, "meeting_llm_provider", DEFAULT_LLM_PROVIDER),
        base_url=getattr(settings, "meeting_llm_base_url", "http://localhost:11434/v1"),
        api_key=getattr(settings, "meeting_llm_api_key", ""),
        model=getattr(settings, "meeting_llm_model", "llama3.1"),
        temperature=float(getattr(settings, "meeting_llm_temperature", 0.2)),
        timeout_seconds=int(getattr(settings, "meeting_llm_timeout_seconds", 120)),
        max_input_chars=int(getattr(settings, "meeting_llm_max_input_chars", 60000)),
        response_format=getattr(settings, "meeting_llm_response_format", "json_schema"),
        reasoning_effort=getattr(settings, "meeting_llm_reasoning_effort", None) or None,
    )


def _openai_compatible_chat(messages: list[dict[str, str]], settings: LLMSettings) -> str:
    if settings.provider not in OPENAI_COMPATIBLE_PROVIDERS:
        raise ValueError(f"Unsupported LLM provider: {settings.provider}")
    base_url = settings.base_url.rstrip("/")
    request_body: dict[str, Any] = {
        "model": settings.model,
        "messages": messages,
        "temperature": settings.temperature,
    }
    if settings.reasoning_effort:
        request_body["reasoning_effort"] = settings.reasoning_effort
    response_format = _llm_response_format(settings.response_format)
    if response_format:
        request_body["response_format"] = response_format
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(request_body).encode("utf-8"),
        headers=_llm_headers(settings),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM request failed: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM request failed: {exc.reason}") from exc
    return _extract_chat_message_text(payload)


def _extract_chat_message_text(payload: dict[str, Any]) -> str:
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("LLM response did not include choices[0].message") from exc
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        text = _text_from_content_parts(content)
        if text.strip():
            return text
    reasoning_content = message.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content.strip():
        return reasoning_content
    raise RuntimeError("LLM response message did not include content or reasoning_content text")


def _text_from_content_parts(parts: list[Any]) -> str:
    texts = []
    for part in parts:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                texts.append(text)
    return "\n".join(texts)


def _llm_headers(settings: LLMSettings) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"
    return headers


def _llm_response_format(value: str) -> dict[str, Any] | None:
    response_format = (value or "json_schema").strip().lower()
    if response_format in {"none", "off", "disabled"}:
        return None
    if response_format == "text":
        return {"type": "text"}
    if response_format == "json_object":
        return {"type": "json_object"}
    if response_format == "json_schema":
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "meeting_intelligence",
                "schema": MEETING_INTELLIGENCE_JSON_SCHEMA,
            },
        }
    raise ValueError(f"Unsupported MEETING_LLM_RESPONSE_FORMAT: {value}")


def _parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            raise ValueError("LLM response did not contain a JSON object")
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("LLM response JSON was not an object")
    return value


def _chunk_lines_for_llm(lines: list[dict[str, Any]], max_input_chars: int) -> list[list[dict[str, Any]]]:
    if not lines:
        return [[]]
    budget = max(1000, max_input_chars)
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_size = 0
    for line in lines:
        encoded_size = len(json.dumps(_line_for_prompt(line), ensure_ascii=False))
        if current and current_size + encoded_size > budget:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(line)
        current_size += encoded_size
    if current:
        chunks.append(current)
    return chunks


def _line_for_prompt(line: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": line.get("timestamp", "00:00:00"),
        "end_timestamp": line.get("end_timestamp", line.get("timestamp", "00:00:00")),
        "speaker": line.get("speaker", "Unknown"),
        "text": line.get("text", ""),
    }


def _meeting_prompt_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "meeting_id": metadata.get("meeting_id"),
        "title": metadata.get("title", ""),
        "start_time": metadata.get("start_time"),
        "end_time": metadata.get("end_time"),
    }


def _finalize_llm_result(
    result: dict[str, Any],
    metadata: dict[str, Any],
    settings: LLMSettings,
    raw_total_lines: int,
    normalized_segment_count: int,
    normalization_applied: bool,
    chunk_count: int,
) -> dict[str, Any]:
    normalized = {
        "schema_version": 1,
        "provider": "llm",
        "llm_provider": settings.provider,
        "llm_model": settings.model,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "meeting_id": metadata.get("meeting_id"),
        "title": metadata.get("title", ""),
        "summary": _clean_text(result.get("summary", "")),
        "key_points": _normalize_llm_items(result.get("key_points", [])),
        "decisions": _normalize_llm_items(result.get("decisions", [])),
        "risks": _normalize_llm_items(result.get("risks", [])),
        "action_items": _normalize_llm_action_items(result.get("action_items", [])),
        "questions": _normalize_llm_items(result.get("questions", [])),
        "blockers": _normalize_llm_items(result.get("blockers", [])),
        "topics": _normalize_llm_topics(result.get("topics", [])),
        "confidence": _safe_confidence(result.get("confidence")),
        "source": {
            "transcript_file": metadata.get("transcript_file", "transcript_final.json"),
            "total_lines": normalized_segment_count,
            "raw_total_lines": raw_total_lines,
            "normalized_segment_count": normalized_segment_count,
            "normalization_applied": normalization_applied,
            "llm_chunk_count": chunk_count,
            "speakers_detected": list(metadata.get("speakers_detected", []) or []),
        },
    }
    if not normalized["summary"]:
        normalized["summary"] = "No reliable LLM summary was returned."
    return normalized


def _normalize_llm_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    normalized = []
    for item in items:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        text = _clean_text(item.get("text") or item.get("summary") or "")
        if not text:
            continue
        normalized.append(
            {
                "text": _clip_text(text, 320),
                "source_timestamp": item.get("source_timestamp") or "00:00:00",
                "source_speaker": item.get("source_speaker") or "Unknown",
                "confidence": _safe_confidence(item.get("confidence"), default=0.75),
            }
        )
    return normalized


def _normalize_llm_action_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            continue
        task = _clean_text(item.get("task") or item.get("text") or "")
        if not task:
            continue
        normalized.append(
            {
                "task": _clip_text(task, 220),
                "owner": item.get("owner"),
                "due_date": item.get("due_date"),
                "source_timestamp": item.get("source_timestamp") or "00:00:00",
                "source_speaker": item.get("source_speaker") or "Unknown",
                "source_text": _clip_text(item.get("source_text") or task, 320),
                "confidence": _safe_confidence(item.get("confidence"), default=0.75),
            }
        )
    return normalized


def _normalize_llm_topics(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    normalized = []
    for item in items:
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            continue
        title = _clean_text(item.get("title") or item.get("text") or "")
        if not title:
            continue
        normalized.append(
            {
                "title": _clip_text(title, 100),
                "start_timestamp": item.get("start_timestamp") or item.get("source_timestamp") or "00:00:00",
                "end_timestamp": item.get("end_timestamp") or item.get("source_end_timestamp") or "00:00:00",
                "summary": _clip_text(item.get("summary") or "", 320),
                "keywords": item.get("keywords") if isinstance(item.get("keywords"), list) else [],
            }
        )
    return normalized


def _safe_confidence(value: Any, default: float = 0.7) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return round(max(0.0, min(1.0, number)), 2)


def _normalize_line(line: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": line.get("index"),
        "timestamp": line.get("timestamp", "00:00:00"),
        "end_timestamp": line.get("end_timestamp") or line.get("timestamp", "00:00:00"),
        "speaker": (line.get("speaker") or "Unknown").strip() or "Unknown",
        "text": _clean_text(line.get("text", "")),
        "source_indices": [line.get("index")] if line.get("index") is not None else [],
        "source_line_count": 1,
    }


def _line_text(line: dict[str, Any]) -> str:
    return _clean_text(line.get("text", ""))


def _clean_text(text: Any) -> str:
    return " ".join(str(text or "").split())


def _caption_prefix_key(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", "", text.lower()).split())


def _is_caption_extension(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    if previous["speaker"] != current["speaker"]:
        return False
    previous_text = previous["text"]
    current_text = current["text"]
    if len(current_text) <= len(previous_text):
        return False
    overlap = _common_prefix_length(_caption_prefix_key(previous_text), _caption_prefix_key(current_text))
    if len(_caption_prefix_key(previous_text)) < 80:
        return False
    return overlap / max(1, len(_caption_prefix_key(previous_text))) >= 0.72


def _common_prefix_length(left: str, right: str) -> int:
    count = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char:
            break
        count += 1
    return count


def _split_text_into_chunks(text: str, target_size: int = 650, hard_limit: int = 780) -> list[str]:
    text = _clean_text(text)
    if len(text) <= hard_limit:
        return [text] if text else []
    sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+", text) if item.strip()]
    if len(sentences) <= 1:
        return _split_long_text(text, hard_limit)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > hard_limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long_text(sentence, hard_limit))
            continue
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > target_size:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _split_long_text(text: str, hard_limit: int) -> list[str]:
    chunks = []
    remaining = text
    while len(remaining) > hard_limit:
        split_at = remaining.rfind(" ", 0, hard_limit)
        if split_at < hard_limit // 2:
            split_at = hard_limit
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _extract_key_points(lines: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    candidates = []
    for line in lines:
        text = line["text"]
        score = _importance_score(text)
        if score <= 0:
            continue
        candidates.append((score, len(text), line))
    if not candidates:
        candidates = [(1, len(line["text"]), line) for line in lines if len(line["text"]) >= 40]
    selected = sorted(candidates, key=lambda item: (-item[0], item[2].get("index") or 0))[:limit]
    return [_source_item(item[2], confidence=min(0.9, 0.55 + item[0] * 0.08)) for item in selected]


def _extract_tagged_items(
    lines: list[dict[str, Any]],
    patterns: list[re.Pattern[str]],
    confidence: float,
    limit: int = 10,
) -> list[dict[str, Any]]:
    items = []
    seen: set[str] = set()
    for line in lines:
        text = line["text"]
        if any(pattern.search(text) for pattern in patterns):
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            items.append(_source_item(line, confidence=confidence, max_length=280))
        if len(items) >= limit:
            break
    return items


def _extract_questions(lines: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    items = []
    for line in lines:
        text = line["text"]
        is_question = re.match(r"^(who|what|when|where|why|how|can|could|should|do|does|did)\b", text, re.I)
        has_direct_question = re.search(r"\b(who|what|when|where|why|how|can|could|should|do|does|did)\b[^.?!]*\?", text, re.I)
        if is_question or has_direct_question:
            items.append(_source_item(line, confidence=0.75, max_length=240))
        if len(items) >= limit:
            break
    return items


def _extract_action_items(lines: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    items = []
    seen: set[str] = set()
    for line in lines:
        patterns = ACTION_MARKER_PATTERNS if _is_long_monologue(line) else ACTION_PATTERNS
        for pattern in patterns:
            match = pattern.search(line["text"])
            if not match:
                continue
            task = _clean_action_task(match.groupdict().get("task") or line["text"])
            if not task:
                continue
            key = task.lower()
            if key in seen:
                continue
            seen.add(key)
            owner = match.groupdict().get("owner") or _owner_from_speaker(line["speaker"])
            if owner and _is_invalid_owner(owner):
                continue
            due_date = _extract_due_date(line["text"])
            items.append(
                {
                    "task": _clip_text(task, 180),
                    "owner": owner,
                    "due_date": due_date,
                    "source_timestamp": line["timestamp"],
                    "source_speaker": line["speaker"],
                    "source_text": _clip_text(line["text"], 280),
                    "confidence": 0.78 if owner else 0.64,
                }
            )
            break
        if len(items) >= limit:
            break
    return items


def _segment_topics(lines: list[dict[str, Any]], max_topics: int = 8) -> list[dict[str, Any]]:
    if not lines:
        return []
    chunk_size = max(4, min(12, len(lines) // 4 or 4))
    topics = []
    for index in range(0, len(lines), chunk_size):
        chunk = lines[index : index + chunk_size]
        if not chunk:
            continue
        chunk_text = " ".join(line["text"] for line in chunk)
        keywords = _keywords(chunk_text)
        title = _topic_title(chunk_text, keywords) or f"Discussion {len(topics) + 1}"
        topics.append(
            {
                "title": title,
                "start_timestamp": chunk[0]["timestamp"],
                "end_timestamp": chunk[-1].get("end_timestamp", chunk[-1]["timestamp"]),
                "summary": _clip_text(_clean_text(chunk[0]["text"]), 260),
                "keywords": keywords[:6],
            }
        )
        topics = _merge_repeated_topics(topics)
        if len(topics) >= max_topics:
            break
    return topics


def _merge_repeated_topics(topics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(topics) < 2:
        return topics
    previous = topics[-2]
    current = topics[-1]
    if previous["title"] != current["title"]:
        return topics
    previous["end_timestamp"] = current["end_timestamp"]
    previous["keywords"] = list(dict.fromkeys(previous.get("keywords", []) + current.get("keywords", [])))[:6]
    return topics[:-1]


def _build_summary(
    metadata: dict[str, Any],
    lines: list[dict[str, Any]],
    speakers: list[str],
    key_points: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    action_items: list[dict[str, Any]],
    topics: list[dict[str, Any]],
) -> str:
    title = metadata.get("title") or "The meeting"
    if not lines:
        return f"{title} has no captured transcript lines, so no reliable meeting summary could be generated."
    speaker_text = f" with {', '.join(speakers[:4])}" if speakers else ""
    parts = [f"{title} covered {len(lines)} normalized discussion segment(s){speaker_text}."]
    topic_summary = _topic_summary(topics)
    if topic_summary:
        parts.append(topic_summary)
    if key_points:
        parts.append(f"Notable point: {key_points[0]['text']}")
    if decisions:
        parts.append(f"Decision noted: {decisions[0]['text']}")
    if action_items:
        parts.append(f"{len(action_items)} action item(s) were detected.")
    return " ".join(parts)


def _topic_summary(topics: list[dict[str, Any]]) -> str:
    titles = [topic.get("title") for topic in topics if topic.get("title")]
    if not titles:
        return ""
    joined = ", ".join(dict.fromkeys(titles[:3]))
    return f"The discussion focused on {joined}."


def _source_item(line: dict[str, Any], confidence: float, max_length: int = 260) -> dict[str, Any]:
    return {
        "text": _clip_text(line["text"], max_length),
        "source_timestamp": line["timestamp"],
        "source_end_timestamp": line.get("end_timestamp", line["timestamp"]),
        "source_speaker": line["speaker"],
        "source_indices": list(line.get("source_indices", [])),
        "confidence": round(confidence, 2),
    }


def _importance_score(text: str) -> int:
    score = 0
    markers = [
        "decided",
        "agreed",
        "important",
        "need to",
        "next step",
        "follow up",
        "risk",
        "blocker",
        "deadline",
        "launch",
        "customer",
        "issue",
        "plan",
        "predict",
        "prediction",
        "market",
        "interest rate",
        "liquidity",
    ]
    lower = text.lower()
    score += sum(1 for marker in markers if marker in lower)
    if len(text) >= 80:
        score += 1
    return score


def _clean_action_task(task: str) -> str:
    task = _clean_text(task)
    task = re.sub(r"\b(by|on)\s+\w+\s+\d{0,2}\.?$", "", task, flags=re.I).strip()
    return task.rstrip(".")


def _owner_from_speaker(speaker: str) -> str | None:
    if speaker and speaker != "Unknown":
        return speaker
    return None


def _extract_due_date(text: str) -> str | None:
    match = DUE_DATE_PATTERN.search(text)
    return match.group(1) if match else None


def _is_long_monologue(line: dict[str, Any]) -> bool:
    return len(line["text"]) > 300 or line.get("source_line_count", 1) > 1


def _is_invalid_owner(owner: str) -> bool:
    return owner.strip().lower() in {
        "a",
        "an",
        "and",
        "but",
        "i",
        "it",
        "okay",
        "right",
        "that",
        "the",
        "there",
        "these",
        "this",
        "those",
        "we decided",
        "we",
        "you",
    }


def _clip_text(text: str, max_length: int) -> str:
    text = _clean_text(text)
    if len(text) <= max_length:
        return text
    clipped = text[: max_length + 1]
    split_at = max(clipped.rfind("."), clipped.rfind("?"), clipped.rfind("!"), clipped.rfind(";"))
    if split_at < max_length // 2:
        split_at = clipped.rfind(" ")
    if split_at < max_length // 2:
        split_at = max_length
    return clipped[:split_at].rstrip(" ,;:.") + "..."


def _overall_confidence(
    lines: list[dict[str, Any]],
    action_items: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    key_points: list[dict[str, Any]],
) -> float:
    if not lines:
        return 0.0
    confidence = 0.45
    if key_points:
        confidence += 0.15
    if action_items:
        confidence += 0.1
    if decisions:
        confidence += 0.1
    if len(lines) >= 10:
        confidence += 0.1
    return round(min(confidence, 0.85), 2)


def _keywords(text: str) -> list[str]:
    stop_words = {
        "about",
        "after",
        "again",
        "also",
        "because",
        "before",
        "could",
        "does",
        "done",
        "from",
        "going",
        "have",
        "here",
        "into",
        "like",
        "mean",
        "meeting",
        "okay",
        "right",
        "should",
        "that",
        "then",
        "their",
        "there",
        "these",
        "this",
        "thing",
        "think",
        "those",
        "very",
        "what",
        "when",
        "will",
        "with",
        "would",
    }
    words = re.findall(r"[A-Za-z][A-Za-z'-]{3,}", text.lower())
    counts: dict[str, int] = {}
    for word in words:
        if word in stop_words:
            continue
        counts[word] = counts.get(word, 0) + 1
    return [word for word, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def _topic_title(text: str, keywords: list[str]) -> str:
    lower = text.lower()
    labels = []
    topic_markers = [
        ("US Market", ["us market", "s&p", "nasdaq", "dow"]),
        ("Indian Market", ["indian market", "nifty", "sensex"]),
        ("Interest Rates", ["interest rate", "interest rates", "rate cut", "rates were cut"]),
        ("Liquidity", ["liquidity", "borrowing", "capital"]),
        ("Nifty", ["nifty"]),
        ("Market Outlook", ["prediction", "predict", "outlook", "sideways", "rally", "consolidate"]),
    ]
    for label, markers in topic_markers:
        if any(marker in lower for marker in markers):
            labels.append(label)
    if labels:
        return ", ".join(dict.fromkeys(labels[:3]))
    return ", ".join(keyword.title() for keyword in keywords[:3])


def _render_items(title: str, items: list[dict[str, Any]], field: str) -> str:
    lines = [f"## {title}"]
    if not items:
        lines.append("- None detected.")
        return "\n".join(lines)
    for item in items:
        timestamp = item.get("source_timestamp", "00:00:00")
        speaker = item.get("source_speaker", "Unknown")
        lines.append(f"- [{timestamp}] {speaker}: {item.get(field, '')}")
    return "\n".join(lines)


def _render_action_items(items: list[dict[str, Any]]) -> str:
    lines = ["## Action Items"]
    if not items:
        lines.append("- None detected.")
        return "\n".join(lines)
    for item in items:
        owner = item.get("owner") or "Unassigned"
        due = f" due {item['due_date']}" if item.get("due_date") else ""
        lines.append(f"- [{item.get('source_timestamp', '00:00:00')}] {owner}: {item.get('task', '')}{due}")
    return "\n".join(lines)


def _render_topics(items: list[dict[str, Any]]) -> str:
    lines = ["## Topics"]
    if not items:
        lines.append("- None detected.")
        return "\n".join(lines)
    for item in items:
        lines.append(
            f"- [{item.get('start_timestamp', '00:00:00')}-{item.get('end_timestamp', '00:00:00')}] "
            f"{item.get('title', 'Discussion')}: {item.get('summary', '')}"
        )
    return "\n".join(lines)


ACTION_MARKER_PATTERNS = [
    re.compile(r"\b(?:action item|todo|follow up|follow-up):?\s+(?P<task>.{6,})", re.I),
]


def regenerate_meeting_intelligence(meeting_dir: str | Path, provider_name: str | None = None) -> dict[str, Any]:
    from config import SETTINGS
    from storage import MeetingStorage

    meeting_path = Path(meeting_dir)
    metadata_path = meeting_path / "metadata.json"
    transcript_path = meeting_path / "transcript_final.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"Missing metadata.json in {meeting_path}")
    if not transcript_path.exists():
        raise FileNotFoundError(f"Missing transcript_final.json in {meeting_path}")

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    transcript_lines = json.loads(transcript_path.read_text(encoding="utf-8"))
    selected_provider = provider_name or metadata.get("meeting_intelligence_provider")
    try:
        provider = create_meeting_intelligence_provider(selected_provider, settings=SETTINGS)
        result = provider.analyze(transcript_lines, metadata)
        status = "completed"
    except Exception as exc:
        fallback_provider_name = getattr(SETTINGS, "meeting_llm_fallback_provider", "")
        if selected_provider == "llm" and fallback_provider_name and fallback_provider_name != "llm":
            provider = create_meeting_intelligence_provider(fallback_provider_name, settings=SETTINGS)
            result = provider.analyze(transcript_lines, metadata)
            result["fallback_reason"] = str(exc)
            status = "completed_with_fallback"
        else:
            raise
    markdown = render_intelligence_markdown(result)
    storage = MeetingStorage(meeting_path.parent)
    updates = storage.write_meeting_intelligence(meeting_path, result, markdown)
    metadata.update(updates, meeting_intelligence_status=status)
    storage.write_metadata(meeting_path, metadata)
    return result


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) != 1:
        print("Usage: python3 -m meeting_intelligence <meeting_dir>", file=sys.stderr)
        return 2
    result = regenerate_meeting_intelligence(args[0])
    print(
        f"Regenerated meeting intelligence: provider={result.get('provider')} "
        f"segments={result.get('source', {}).get('normalized_segment_count')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
