# Agent Setup

This file gives coding agents the project context needed before making changes in MeetRead.

## Project Objective

MeetRead is an open-source, self-hosted Google Meet capture and meeting intelligence platform. The goal is to become a practical alternative to hosted tools such as Read.ai and Fireflies for individuals and organizations that want to capture Google Meet calls without sending meeting data outside infrastructure they control.

Build decisions should support that direction:

- Self-hosted first.
- Privacy-preserving by default.
- Local capture and local deterministic fallback should keep working without external AI, SMTP, storage, or integration providers.
- External services are opt-in adapters, not required dependencies.
- Organization features such as workspaces, permissions, audit logs, retention, consent, and admin controls are part of the long-term product direction.

## First Files To Read

Read these before implementing non-trivial changes:

1. `README.md` for the current setup, runtime modes, and artifact layout.
2. `ROADMAP.md` for the product direction and prioritized TODO areas.
3. `config.py` for all environment-driven behavior.
4. The module closest to the requested change.
5. The matching test file under `tests/` when one exists.

## Current Architecture

Core runtime flow:

1. `main.py` starts `MeetingOrchestrator`.
2. `calendar_watcher.py` polls Google Calendar for upcoming Meet events.
3. `meet_bot.py` launches Chrome/Selenium, signs in or reuses a profile, joins Meet, enables captions, and coordinates capture.
4. `audio_recorder.py` records routed meeting audio.
5. `caption_scraper.py` scrapes Google Meet live captions from the browser DOM.
6. `storage.py` writes per-meeting metadata, logs, audio, captions, final transcripts, and artifacts.
7. `meeting_intelligence.py` generates summary, key points, decisions, risks, questions, blockers, topics, and action items.
8. `email_delivery.py` optionally sends structured SMTP summary emails.

Important runtime outputs live under:

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

## Development Setup

Use Python 3.11.

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

For local configuration:

```bash
cp .env.example .env
```

Use `.env.docker.example` for Docker-based runs:

```bash
cp .env.docker.example .env.docker
docker compose up --build
```

Running the real bot requires Google credentials, a bot Google account, Chrome, and audio routing. Unit tests do not require a real meeting.

## Verification Commands

For most code changes:

```bash
pytest
```

For focused changes:

```bash
pytest tests/test_meeting_intelligence.py
pytest tests/test_email_delivery.py
pytest tests/test_meet_bot.py
pytest tests/test_audio_recorder.py
pytest tests/test_caption_scraper.py
pytest tests/test_calendar_watcher.py
pytest tests/test_storage.py
```

Manual runtime checks are environment-dependent. Do not assume a real Google Meet test can run without explicit credentials, Chrome setup, and audio routing.

## Product And Privacy Rules

- Do not make transcript, audio, summary, log, or credential data leave the machine unless the operator explicitly enables an external provider.
- Keep LLM-backed intelligence optional. `rule_based` local intelligence must remain a valid fallback.
- Keep SMTP delivery optional. Capture status should not depend on email delivery success.
- Avoid automatic emailing to calendar attendees unless an explicit opt-in recipient mode is implemented.
- Do not commit real `credentials.json`, `token.json`, `.env`, meeting artifacts, browser profiles, or generated audio/transcript files.
- Treat meeting data as sensitive. Avoid adding debug logs that expose secrets, raw credentials, full tokens, SMTP passwords, LLM API keys, or unnecessary personal data.
- Prefer explicit configuration through `config.py` and `.env.example` over hidden defaults.

## Implementation Guidance

- Preserve the current module boundaries unless the change requires a larger refactor.
- Add or update tests for behavior changes, especially around browser state handling, caption parsing, transcript normalization, intelligence parsing, email delivery, and storage metadata.
- Keep capture reliability higher priority than dashboard or integration polish.
- When adding integrations, isolate them behind optional adapters and keep the local artifact model stable.
- When adding AI features, support local/self-hosted providers where possible and define deterministic fallback behavior.
- When changing output artifacts, keep backward compatibility or document the migration clearly.

## Common Task Map

- Calendar scheduling or event filtering: `calendar_watcher.py`, `tests/test_calendar_watcher.py`
- Meet join flow, browser state, captions toggle, meeting end detection: `meet_bot.py`, `tests/test_meet_bot.py`
- Audio capture, chunking, ffmpeg concat, MP3 generation: `audio_recorder.py`, `tests/test_audio_recorder.py`
- Caption DOM parsing and transcript capture: `caption_scraper.py`, `tests/test_caption_scraper.py`
- Artifact paths, metadata, final transcript writing: `storage.py`, `tests/test_storage.py`
- Summaries, action items, LLM parsing, rule-based fallback: `meeting_intelligence.py`, `tests/test_meeting_intelligence.py`
- SMTP summary rendering and delivery: `email_delivery.py`, `tests/test_email_delivery.py`
- Environment variables and defaults: `config.py`, `.env.example`, `.env.docker.example`

## Documentation Expectations

Update documentation when changing:

- Setup or runtime commands.
- Environment variables.
- Output artifact names or structure.
- Provider behavior for LLMs, SMTP, storage, or integrations.
- Product direction or roadmap scope.

Keep `README.md` focused on user-facing setup and operation. Keep `ROADMAP.md` focused on product direction and TODOs. Keep this file focused on helping code agents work safely in the repo.
