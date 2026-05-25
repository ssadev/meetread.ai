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
    )


SETTINGS = get_settings()
