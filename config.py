from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _int_env(name: str) -> int | None:
    value = os.getenv(name)
    if not value:
        return None
    return int(value)


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    return float(value)


@dataclass(frozen=True)
class Settings:
    google_credentials_path: Path
    google_token_path: Path
    bot_google_account_email: str
    bot_google_account_password: str
    bot_display_name: str
    meetings_output_dir: Path
    poll_interval_seconds: int
    max_meeting_duration_hours: int
    join_buffer_minutes: int
    lobby_wait_minutes: int
    save_mp3: bool
    delete_chunks_after_concat: bool
    seen_meetings_path: Path
    bot_session_path: Path
    audio_backend: str
    audio_input_device: str | None
    audio_samplerate: int
    audio_channels: int
    audio_chunk_seconds: int
    chrome_version_main: int | None
    chrome_binary_path: str | None
    chrome_user_data_dir: str | None
    chrome_profile_directory: str | None
    chrome_headless: bool
    skip_google_login: bool
    treat_removal_as_meeting_end: bool
    meeting_intelligence_enabled: bool
    meeting_intelligence_provider: str
    meeting_llm_provider: str
    meeting_llm_base_url: str
    meeting_llm_api_key: str
    meeting_llm_model: str
    meeting_llm_temperature: float
    meeting_llm_timeout_seconds: int
    meeting_llm_max_input_chars: int
    meeting_llm_response_format: str
    meeting_llm_fallback_provider: str


def get_settings(env_path: str | Path = ".env") -> Settings:
    _load_dotenv(env_path)
    output_dir = Path(os.getenv("MEETINGS_OUTPUT_DIR", "./meetings"))
    return Settings(
        google_credentials_path=Path(os.getenv("GOOGLE_CREDENTIALS_PATH", "./credentials.json")),
        google_token_path=Path(os.getenv("GOOGLE_TOKEN_PATH", "./token.json")),
        bot_google_account_email=os.getenv("BOT_GOOGLE_ACCOUNT_EMAIL", ""),
        bot_google_account_password=os.getenv("BOT_GOOGLE_ACCOUNT_PASSWORD", ""),
        bot_display_name=os.getenv("BOT_DISPLAY_NAME", "MeetRead Bot"),
        meetings_output_dir=output_dir,
        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "300")),
        max_meeting_duration_hours=int(os.getenv("MAX_MEETING_DURATION_HOURS", "3")),
        join_buffer_minutes=int(os.getenv("JOIN_BUFFER_MINUTES", "2")),
        lobby_wait_minutes=int(os.getenv("LOBBY_WAIT_MINUTES", "10")),
        save_mp3=_bool_env("SAVE_MP3", True),
        delete_chunks_after_concat=_bool_env("DELETE_CHUNKS_AFTER_CONCAT", True),
        seen_meetings_path=Path(os.getenv("SEEN_MEETINGS_PATH", "./seen_meetings.json")),
        bot_session_path=Path(os.getenv("BOT_SESSION_PATH", "./bot_session.pkl")),
        audio_backend=os.getenv("AUDIO_BACKEND", "auto").lower(),
        audio_input_device=os.getenv("AUDIO_INPUT_DEVICE") or None,
        audio_samplerate=int(os.getenv("AUDIO_SAMPLERATE", "44100")),
        audio_channels=int(os.getenv("AUDIO_CHANNELS", "2")),
        audio_chunk_seconds=int(os.getenv("AUDIO_CHUNK_SECONDS", "60")),
        chrome_version_main=_int_env("CHROME_VERSION_MAIN"),
        chrome_binary_path=os.getenv("CHROME_BINARY_PATH") or None,
        chrome_user_data_dir=os.getenv("CHROME_USER_DATA_DIR") or None,
        chrome_profile_directory=os.getenv("CHROME_PROFILE_DIRECTORY") or None,
        chrome_headless=_bool_env("CHROME_HEADLESS", False),
        skip_google_login=_bool_env("SKIP_GOOGLE_LOGIN", False),
        treat_removal_as_meeting_end=_bool_env("TREAT_REMOVAL_AS_MEETING_END", True),
        meeting_intelligence_enabled=_bool_env("MEETING_INTELLIGENCE_ENABLED", True),
        meeting_intelligence_provider=os.getenv("MEETING_INTELLIGENCE_PROVIDER", "rule_based"),
        meeting_llm_provider=os.getenv("MEETING_LLM_PROVIDER", "openai_compatible"),
        meeting_llm_base_url=os.getenv("MEETING_LLM_BASE_URL", "http://localhost:11434/v1"),
        meeting_llm_api_key=os.getenv("MEETING_LLM_API_KEY", ""),
        meeting_llm_model=os.getenv("MEETING_LLM_MODEL", "llama3.1"),
        meeting_llm_temperature=_float_env("MEETING_LLM_TEMPERATURE", 0.2),
        meeting_llm_timeout_seconds=int(os.getenv("MEETING_LLM_TIMEOUT_SECONDS", "120")),
        meeting_llm_max_input_chars=int(os.getenv("MEETING_LLM_MAX_INPUT_CHARS", "60000")),
        meeting_llm_response_format=os.getenv("MEETING_LLM_RESPONSE_FORMAT", "json_schema"),
        meeting_llm_fallback_provider=os.getenv("MEETING_LLM_FALLBACK_PROVIDER", "rule_based"),
    )


SETTINGS = get_settings()
