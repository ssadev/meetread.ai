# MeetRead

MeetRead is an open-source, self-hosted Google Meet capture and meeting intelligence platform. The long-term goal is to provide an alternative to hosted meeting assistants such as Read.ai and Fireflies for individuals and organizations that need meeting capture, transcripts, summaries, and action items without sending their meeting data outside their own infrastructure.

Today, MeetRead is a Python 3.11 meeting bot that joins Google Meet calls, records routed system audio, and scrapes Google Meet live captions in parallel. It can generate local and LLM-backed meeting intelligence and send optional summary emails. The current transcript source is Google Meet captions; it does not run Whisper, speech-to-text, or ML transcription by default.

## Product Objective

MeetRead is being built toward a full-fledged open-source meeting capture product with these priorities:

- Keep meeting artifacts under the user's control: audio, transcripts, summaries, logs, credentials, and integration data should stay on infrastructure the user or organization owns.
- Capture Google Meet reliably: join scheduled calls, record audio, collect captions/transcripts, detect meeting lifecycle states, and preserve durable per-meeting artifacts.
- Provide privacy-preserving meeting intelligence: local deterministic summaries by default, optional self-hosted or user-configured LLM providers, and clear fallbacks when AI providers fail.
- Support both individuals and organizations: local setup for personal use, plus future team/workspace concepts, permissions, audit logs, retention controls, and deployment options.
- Become a practical open-source replacement for hosted meeting assistants: dashboard, searchable transcripts, synced playback, editable summaries, action items, integrations, APIs, and production operations.

The product roadmap and TODO list live in [ROADMAP.md](ROADMAP.md).

## Current Scope

The current implementation focuses on capture and post-meeting artifacts:

- Calendar polling for upcoming Google Meet events.
- Manual meeting join by URL with optional background mode (no calendar required).
- Automated Meet join flow using a dedicated bot Google account.
- Lobby admission detection with configurable wait timeout.
- Routed system audio recording on Docker/Linux and macOS.
- Live caption scraping from the Google Meet browser DOM.
- Per-meeting output folders with metadata, logs, audio, transcripts, and intelligence artifacts.
- Rule-based local meeting intelligence with optional LLM providers (OpenAI-compatible, Sarvam AI).
- Optional SMTP summary email delivery with manual resend support.

This is not yet a complete Read.ai/Fireflies-class product. The roadmap tracks the remaining product, reliability, security, integration, and operations work needed to get there.

## Layout

```text
meetread.ai/
  main.py               # calendar-driven daemon
  join_meeting.py       # manual join by URL (no calendar needed)
  meet_bot.py           # core browser automation and meeting lifecycle
  calendar_watcher.py   # Google Calendar polling
  audio_recorder.py     # PulseAudio/sounddevice capture
  caption_scraper.py    # Google Meet DOM caption scraping
  meeting_intelligence.py  # rule-based and LLM intelligence providers
  email_delivery.py     # SMTP summary email delivery
  storage.py            # per-meeting artifact filesystem layout
  config.py             # settings dataclass, env loading
  auth.py               # Google OAuth flow
  list_audio_devices.py # utility to enumerate audio devices
  requirements.txt
  .env.example
  .env.docker.example
  Dockerfile
  docker-compose.yml
  tests/
```

## Docker Setup

The recommended production target is the Docker image. It contains Python, Google Chrome, Xvfb, PulseAudio, PortAudio, and ffmpeg. Chrome runs with no visible UI inside the container, PulseAudio routes Meet audio into a per-meeting monitor source, and the existing caption scraper records Meet captions from the browser DOM.

1. Generate `credentials.json` and `token.json` once before running the daemon. The container expects them to be mounted at runtime, not baked into the image.
2. Create the Docker env file:

```bash
cp .env.docker.example .env.docker
```

3. Edit `.env.docker` and set:

```env
BOT_GOOGLE_ACCOUNT_EMAIL=bot@gmail.com
BOT_GOOGLE_ACCOUNT_PASSWORD=yourpassword
BOT_DISPLAY_NAME=MeetRead Bot
```

4. Build and start:

```bash
docker compose up --build
```

The compose service is pinned to `linux/amd64` because Google publishes the stable Chrome Debian package for AMD64. On Apple Silicon Docker Desktop, Compose will build/run it through Linux AMD64 emulation.

The compose file mounts:

```text
./meetings  -> /app/meetings
./state     -> /app/state
./credentials.json -> /app/secrets/credentials.json
./token.json       -> /app/secrets/token.json
```

`/app/meetings` receives transcripts and audio artifacts. `/app/state` stores seen calendar events and the optional browser session pickle.

The default Docker mode uses an invisible Xvfb display rather than Chrome's native `--headless=new` mode because Google Meet media behavior is usually more reliable that way. To force native Chrome headless mode, set `CHROME_HEADLESS=true` in `.env.docker`.

## macOS Setup

On a Mac, PulseAudio is not used. Install a CoreAudio loopback device and record it with `sounddevice`.

1. Install Python 3.11 and ffmpeg:

```bash
brew install python@3.11 ffmpeg
```

2. Install BlackHole:

```bash
brew install blackhole-2ch
```

3. Open `Audio MIDI Setup`.
4. Create a `Multi-Output Device`.
5. Check both your speakers/headphones and `BlackHole 2ch`.
6. Set macOS system output to that Multi-Output Device before the meeting bot launches Chrome.
7. Confirm the device name:

```bash
python list_audio_devices.py
```

8. Set this in `.env`:

```env
AUDIO_BACKEND=sounddevice
AUDIO_INPUT_DEVICE=BlackHole 2ch
```

With this setup, Chrome/Meet audio goes to system output, the Multi-Output Device mirrors it into BlackHole, and `AudioRecorder` records from `BlackHole 2ch`. Captions are still scraped from Meet's DOM in parallel.

## Linux Setup

Install Chrome or Chromium, PulseAudio, ffmpeg, Xvfb, and Python 3.11.

```bash
sudo apt-get update
sudo apt-get install -y pulseaudio pulseaudio-utils ffmpeg xvfb python3.11 python3.11-venv
```

Create a virtual environment and install dependencies:

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Google Cloud Setup

1. Create a Google Cloud project.
2. Enable Google Calendar API.
3. Configure OAuth consent for the bot user.
4. Create OAuth Desktop credentials and download them as `credentials.json`.
5. Share the calendar owner calendar with the bot Google account.
6. Copy `.env.example` to `.env` and fill in `BOT_GOOGLE_ACCOUNT_EMAIL`, `BOT_GOOGLE_ACCOUNT_PASSWORD`, and optionally `BOT_DISPLAY_NAME`.

Run the OAuth flow once:

```bash
python -c "from auth import get_calendar_service; get_calendar_service()"
```

This creates `token.json`.

## Running

### Calendar daemon

```bash
python main.py
```

The daemon polls Calendar every `POLL_INTERVAL_SECONDS`. When a Google Meet event starts within `JOIN_BUFFER_MINUTES`, it launches a browser, joins the Meet, enables captions, then starts:

- `AudioRecorder` in one thread
- `CaptionScraper` in another thread

Both workers receive the same meeting start timestamp and the same `threading.Event` stop signal.

Before requesting entry, the bot blocks Chrome camera/microphone permissions, turns off Meet's pre-join mic/camera controls if they appear, and fills the Google Meet guest name prompt with `BOT_DISPLAY_NAME` when the prompt is shown.

If Google Meet shows that the bot was removed from the meeting, the bot treats that as meeting end and finalizes recording/transcripts by default. Set `TREAT_REMOVAL_AS_MEETING_END=false` to disable that behavior.

The bot waits up to `LOBBY_WAIT_MINUTES` for a host to admit it from the Meet lobby before treating the join as failed.

### Manual join (no calendar required)

```bash
python join_meeting.py "https://meet.google.com/abc-defg-hij"
python join_meeting.py "https://meet.google.com/abc-defg-hij" --title "Weekly Sync"
python join_meeting.py "https://meet.google.com/abc-defg-hij" --duration-hours 2
python join_meeting.py "https://meet.google.com/abc-defg-hij" --end-time "2026-05-27T15:00:00+05:30"
```

To detach from the terminal and run the bot in the background:

```bash
python join_meeting.py "https://meet.google.com/abc-defg-hij" --background
# Prints PID and log path, then exits. Bot keeps running.
```

Inside a running Docker container:

```bash
docker exec <container> python join_meeting.py "https://meet.google.com/abc-defg-hij" --background
```

## Output

Each meeting writes to:

```text
meetings/{YYYY-MM-DD}_{sanitized_meeting_title}/
  metadata.json
  audio_raw.wav
  audio_raw.mp3
  transcript_live.json
  transcript_final.json
  transcript_final.txt
  meeting_intelligence.json
  meeting_intelligence.md
  bot.log
```

Audio chunks are written to `audio_chunks/` while recording. After ffmpeg concatenates them successfully, the chunks folder is deleted when `DELETE_CHUNKS_AFTER_CONCAT=true`.

Meeting intelligence runs after transcript finalization when `MEETING_INTELLIGENCE_ENABLED=true`. The current provider is `rule_based`, which creates deterministic local summaries, key points, decisions, risks, questions, blockers, topics, and action items without sending transcript data to an external service. The provider is selected with:

```env
MEETING_INTELLIGENCE_PROVIDER=rule_based
```

For AI-generated intelligence, enable the LLM provider:

```env
MEETING_INTELLIGENCE_PROVIDER=llm
MEETING_LLM_PROVIDER=openai_compatible
MEETING_LLM_BASE_URL=http://localhost:11434/v1
MEETING_LLM_MODEL=llama3.1
MEETING_LLM_API_KEY=
MEETING_LLM_TEMPERATURE=0.2
MEETING_LLM_TIMEOUT_SECONDS=120
MEETING_LLM_MAX_INPUT_CHARS=60000
MEETING_LLM_RESPONSE_FORMAT=json_schema
MEETING_LLM_FALLBACK_PROVIDER=rule_based
```

The `openai_compatible` LLM provider posts to `/chat/completions` and works with Ollama, LM Studio, vLLM, OpenAI, or any OpenAI-compatible gateway. `MEETING_LLM_API_KEY` can be empty for local providers. `MEETING_LLM_RESPONSE_FORMAT` defaults to `json_schema`; use `text` for gateways that do not support structured output, `json_object` for older servers, or `none` to omit the field. `MEETING_LLM_MAX_INPUT_CHARS` controls how much transcript text is sent per request; long transcripts are chunked automatically and results merged. If the LLM request fails or returns invalid JSON, `MEETING_LLM_FALLBACK_PROVIDER=rule_based` lets meeting finalization still produce local intelligence artifacts.

**Sarvam AI** is also supported as an LLM provider:

```env
MEETING_INTELLIGENCE_PROVIDER=llm
MEETING_LLM_PROVIDER=sarvam
MEETING_LLM_BASE_URL=https://api.sarvam.ai/v1
MEETING_LLM_MODEL=sarvam-105b
MEETING_LLM_API_KEY=your-sarvam-api-key
MEETING_LLM_RESPONSE_FORMAT=json_object
MEETING_LLM_REASONING_EFFORT=high
MEETING_LLM_FALLBACK_PROVIDER=rule_based
```

`MEETING_LLM_REASONING_EFFORT` is optional and passed to providers that support it (e.g. `low`, `medium`, `high` on Sarvam).

To regenerate intelligence for an existing meeting folder after improving the analyzer:

```bash
python3 -m meeting_intelligence meetings/2026-05-26_245pm
```

The rule-based provider normalizes repeated incremental Meet captions before analysis and records `raw_total_lines`, `normalized_segment_count`, and `normalization_applied` in `meeting_intelligence.json`.

Summary email delivery runs after meeting finalization when `SUMMARY_EMAIL_ENABLED=true`. It sends a concise inline summary to `SMTP_SUMMARY_RECIPIENTS`; it does not email calendar attendees automatically and does not attach large files by default.

```env
SUMMARY_EMAIL_ENABLED=true
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=bot@example.com
SMTP_PASSWORD=app-password
SMTP_FROM=MeetRead <bot@example.com>
SMTP_SUMMARY_RECIPIENTS=alice@example.com,bob@example.com
SMTP_USE_TLS=true
SMTP_USE_SSL=false
SMTP_SUBJECT_PREFIX=[MeetRead]
```

Email delivery status is recorded in `metadata.json` as `summary_email_status`, `summary_email_sent_at`, `summary_email_recipients`, and `summary_email_error` when applicable. SMTP failures do not change the meeting capture status. To resend a summary for an existing meeting:

```bash
python3 -m email_delivery meetings/2026-05-26_615pm
```

## Audio Routing

`AUDIO_BACKEND` defaults to `auto`, which selects `pulseaudio` on Linux and `sounddevice` on macOS.

On Linux (or Docker), the bot creates a per-meeting PulseAudio null sink before launching Chrome:

```bash
pactl load-module module-null-sink sink_name=MeetBot_xxx
pactl set-default-sink MeetBot_xxx
pactl set-default-source MeetBot_xxx.monitor
```

Chrome routes Meet audio into that virtual sink, and `sounddevice` records from `MeetBot_xxx.monitor`.

On macOS, the bot does not call `pactl` and does not start a virtual display. Set:

```env
AUDIO_BACKEND=sounddevice
AUDIO_INPUT_DEVICE=BlackHole 2ch
```

To enumerate available input device names:

```bash
python list_audio_devices.py
```

## Google Account Notes

Use a dedicated bot Google account. If Google asks for 2FA during Selenium login, the bot logs the issue and aborts the meeting with `failed` status.

On macOS, the most reliable setup is a persistent Chrome profile:

```env
CHROME_USER_DATA_DIR=./chrome-bot-profile
CHROME_PROFILE_DIRECTORY=Default
SKIP_GOOGLE_LOGIN=true
```

Then run Chrome once with that profile and sign in manually:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --user-data-dir="$PWD/chrome-bot-profile" --profile-directory=Default
```

After the bot account is signed in, close that Chrome window and run `python main.py`. The bot will reuse the signed-in session and skip automated password entry. If automated sign-in gets stuck, the meeting folder will contain `google_signin_no_password.html` and `google_signin_no_password.png` for diagnosis.

## Roadmap

The product roadmap and TODO list live in [ROADMAP.md](ROADMAP.md). Future work should preserve the core project direction: open-source, self-hosted, privacy-conscious meeting capture and intelligence for Google Meet.

## Code Agent Context

Coding agents should read [AGENTS.md](AGENTS.md) before making changes. It summarizes the project objective, architecture, privacy rules, local setup, verification commands, and module map for future development.

## systemd Example

```ini
[Unit]
Description=Google Meet Bot
After=network-online.target pulseaudio.service

[Service]
WorkingDirectory=/opt/google-meet-bot
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/google-meet-bot/.venv/bin/python /opt/google-meet-bot/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Tests

```bash
pytest
```

The tests mock browser and audio boundaries. A real meeting requires Chrome, Google credentials, and a bot account that can join the target Meet. Full audio capture on macOS requires a loopback input such as BlackHole.
