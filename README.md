# Google Meet Bot

Python 3.11 meeting bot that joins Google Meet calls, records routed system audio, and scrapes Google Meet live captions in parallel. It does not run Whisper, speech-to-text, or ML transcription. The transcript comes only from Meet captions.

## Layout

```text
google-meet-bot/
  main.py
  calendar_watcher.py
  meet_bot.py
  audio_recorder.py
  caption_scraper.py
  storage.py
  config.py
  auth.py
  requirements.txt
  .env.example
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

```bash
python main.py
```

The daemon polls Calendar every `POLL_INTERVAL_SECONDS`. When a Google Meet event starts within `JOIN_BUFFER_MINUTES`, it launches a browser, joins the Meet, enables captions, then starts:

- `AudioRecorder` in one thread
- `CaptionScraper` in another thread

Both workers receive the same meeting start timestamp and the same `threading.Event` stop signal.

Before requesting entry, the bot blocks Chrome camera/microphone permissions, turns off Meet's pre-join mic/camera controls if they appear, and fills the Google Meet guest name prompt with `BOT_DISPLAY_NAME` when the prompt is shown.

If Google Meet shows that the bot was removed from the meeting, the bot treats that as meeting end and finalizes recording/transcripts by default. Set `TREAT_REMOVAL_AS_MEETING_END=false` to disable that behavior.

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
MEETING_LLM_RESPONSE_FORMAT=json_schema
MEETING_LLM_FALLBACK_PROVIDER=rule_based
```

The `openai_compatible` LLM provider posts to `/chat/completions`, so it can work with OpenAI-compatible hosted APIs, Ollama's OpenAI-compatible endpoint, LM Studio, vLLM, or a self-hosted gateway. `MEETING_LLM_API_KEY` can be empty for local providers. `MEETING_LLM_RESPONSE_FORMAT` defaults to `json_schema`; use `text` for gateways that do not support structured output, `json_object` for older OpenAI-compatible servers, or `none` to omit the field. If the LLM request fails or returns invalid JSON after one repair retry, `MEETING_LLM_FALLBACK_PROVIDER=rule_based` lets meeting finalization still produce local intelligence artifacts.

To regenerate intelligence for an existing meeting folder after improving the analyzer:

```bash
python3 -m meeting_intelligence meetings/2026-05-26_245pm
```

The rule-based provider normalizes repeated incremental Meet captions before analysis and records `raw_total_lines`, `normalized_segment_count`, and `normalization_applied` in `meeting_intelligence.json`.

## Audio Routing

On Linux, the bot creates the PulseAudio sink before launching Chrome:

```bash
pactl load-module module-null-sink sink_name=MeetBot_xxx
pactl set-default-sink MeetBot_xxx
pactl set-default-source MeetBot_xxx.monitor
```

Chrome is started with `--alsa-output-device=pulse`, so meeting audio goes to the virtual sink and `sounddevice` records from `MeetBot_xxx.monitor`.

On macOS, set `AUDIO_BACKEND=sounddevice` and `AUDIO_INPUT_DEVICE=BlackHole 2ch`. The app does not call `pactl` and does not start a virtual display on macOS.

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

## Product Roadmap / TODO

The project currently captures Google Meet audio and Meet-provided captions. To move closer to a Read.ai or Fireflies-style product replacement, the next improvements should focus on reliability, transcript quality, intelligence, collaboration workflows, and product operations.

### Capture Reliability

- [ ] Add a meeting state machine with explicit states such as `scheduled`, `joining`, `waiting_for_admission`, `recording`, `removed`, `ended`, `failed`, and `partial`.
- [ ] Improve join diagnostics with structured screenshots, HTML snapshots, current URL, visible page text, and detected Meet state.
- [ ] Add automatic recovery when caption DOM selectors change, including selector telemetry in `bot.log`.
- [ ] Detect silent audio during recording and mark the meeting as `partial` if the audio stream is blank for too long.
- [ ] Add configurable retry rules for temporary ChromeDriver, PulseAudio, or network failures.
- [ ] Support concurrent meetings safely with isolated browser profiles, PulseAudio sinks, output folders, and logs.

### Transcript Quality

- [ ] Add optional Whisper or another speech-to-text backend as a fallback when Google Meet captions are disabled or unreliable.
- [ ] Merge Meet captions and audio transcription into a single best transcript with timestamps and confidence metadata.
- [ ] Improve speaker detection with participant roster scraping, caption speaker parsing, and optional voice diarization.
- [ ] Add transcript cleanup to remove repeated incremental captions without losing live transcript updates.
- [ ] Add punctuation, casing, paragraphing, and sentence boundary cleanup for final transcripts.
- [ ] Add transcript search and filtering by speaker, timestamp, or keyword.

### Meeting Intelligence

- [x] Add a provider-neutral meeting intelligence interface with a local rule-based provider.
- [x] Generate local meeting intelligence artifacts with summary, key points, decisions, risks, action items, questions, blockers, and topics.
- [x] Add AI/LLM-generated summaries behind user-controlled provider settings.
- [x] Support OpenAI-compatible hosted APIs and self-hosted/local providers such as Ollama through one provider path.
- [ ] Add native provider adapters where OpenAI-compatible APIs are insufficient.
- [ ] Extract richer action items with owner, due date, source timestamp, and confidence using LLM-backed providers.
- [ ] Detect objections and unresolved topics with better semantic accuracy.
- [ ] Improve topic segmentation so long meetings are grouped into readable sections.
- [ ] Add meeting highlights with links back to audio/transcript timestamps.
- [ ] Add optional summary style presets for sales calls, standups, interviews, support calls, and product meetings without requiring a static template for every meeting.

### Integrations

- [ ] Push summaries and action items to Slack, email, Notion, Confluence, Linear, Jira, or HubSpot.
- [ ] Create calendar event follow-up notes automatically after the meeting ends.
- [ ] Add webhook events for `meeting_started`, `meeting_completed`, `meeting_failed`, and `summary_ready`.
- [ ] Store artifacts in S3, GCS, or another object store instead of only the local filesystem.
- [ ] Add a REST API to query meetings, transcripts, summaries, and recordings.
- [ ] Add user/team-level settings for retention, sharing, summary format, and integration targets.

### Product Experience

- [ ] Build a web dashboard to browse meetings, recordings, transcripts, summaries, and action items.
- [ ] Add playback with transcript syncing and click-to-seek timestamps.
- [ ] Add editable transcripts and human correction workflow.
- [ ] Add shareable meeting pages with permissions.
- [ ] Add organization, workspace, and team concepts.
- [ ] Add notification preferences for meeting completion and summary delivery.

### Security, Privacy, And Compliance

- [ ] Add explicit retention settings for audio, transcripts, logs, and screenshots.
- [ ] Add encryption-at-rest support for sensitive artifacts.
- [ ] Redact secrets, personal data, and sensitive keywords from logs.
- [ ] Add role-based access control for meeting artifacts.
- [ ] Add audit logs for who accessed, edited, exported, or deleted meeting data.
- [ ] Add consent and disclosure controls so meeting participants know the bot is recording.

### Operations And Scale

- [ ] Add structured JSON logs and metrics for join success rate, caption health, audio health, and processing time.
- [ ] Add health checks for Chrome, PulseAudio, Calendar API, storage, and worker queues.
- [ ] Move long-running post-processing into a job queue.
- [ ] Add database storage for meetings, artifacts, speaker metadata, summaries, and integration status.
- [ ] Add alerts for repeated join failures, blank recordings, missing transcripts, or expired credentials.
- [ ] Add deployment docs for Docker Compose, Kubernetes, and systemd production setups.

### Evaluation And Quality

- [ ] Create fixture-based tests using saved Meet DOM samples for caption parsing.
- [ ] Add audio validation tests that distinguish silence from real speech.
- [ ] Add golden-file tests for transcript cleanup and summary generation.
- [ ] Track transcript word error rate when using an STT backend.
- [ ] Track summary quality with human review fields and regression examples.
- [ ] Add end-to-end test meetings in a controlled Google Workspace test account.

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
