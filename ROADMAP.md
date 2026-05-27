# Product Roadmap / TODO

MeetRead's objective is to become a full-fledged open-source, self-hosted alternative to hosted meeting assistants such as Read.ai and Fireflies for Google Meet. It is designed for individuals and organizations that want meeting capture, transcripts, summaries, action items, and integrations while keeping meeting data inside infrastructure they control.

The current implementation joins Google Meet calls from Calendar, records routed audio, captures Meet-provided captions, generates local and LLM-backed meeting intelligence, and can deliver structured summary emails through SMTP. The roadmap below tracks the work required to evolve that capture bot into a production-ready meeting intelligence product.

## Product Principles

- Self-hosted first: users should be able to run MeetRead on their own machine, server, cloud account, or Kubernetes environment without depending on a hosted MeetRead SaaS.
- Privacy by default: meeting audio, transcripts, summaries, logs, and credentials should remain local unless the operator explicitly configures an external provider or integration.
- Local fallback: core capture and deterministic meeting intelligence should continue working when optional LLM, SMTP, storage, or integration providers are unavailable.
- Organization-ready: the product should grow toward workspaces, permissions, retention policies, audit logs, consent controls, and admin-level configuration.
- Extensible integrations: Slack, Notion, Confluence, Linear, Jira, HubSpot, object storage, webhooks, and APIs should be optional adapters around a stable internal meeting artifact model.
- Reliable capture before polish: meeting join reliability, audio health, caption health, diagnostics, and recovery should remain the foundation for dashboard and workflow features.

## Current Capabilities

- [x] Poll Google Calendar for upcoming Google Meet events.
- [x] Join Google Meet from Docker/Linux or macOS.
- [x] Manual meeting join by URL with optional background/detach mode (no calendar required).
- [x] Use a dedicated bot display name on the Meet pre-join screen.
- [x] Block bot camera/microphone permissions and disable pre-join mic/camera controls.
- [x] Detect Meet lobby and wait for host admission up to `LOBBY_WAIT_MINUTES`; return `not_admitted` on timeout.
- [x] Record routed system audio to WAV and optional MP3.
- [x] Capture Google Meet live captions from the browser DOM.
- [x] Write per-meeting metadata, logs, audio, live transcript, final transcript JSON, and final transcript text.
- [x] Detect explicit meeting end/removal states from Meet DOM text.
- [x] Use Docker Compose with Chrome, Xvfb, PulseAudio, ffmpeg, and mounted state/secrets.
- [x] Support macOS loopback recording through `sounddevice` and BlackHole.

## Capture Reliability

- [x] Add Calendar polling lookahead that covers the full poll interval.
- [x] Treat stale Google Meet caption DOM references as transient poll failures.
- [x] Dump debug screenshots/HTML for selected join, sign-in, URL-change, and caption-enable failures.
- [x] Detect Google Meet removal as meeting end, with `TREAT_REMOVAL_AS_MEETING_END` control.
- [ ] Add a formal meeting state machine with explicit states such as `scheduled`, `joining`, `waiting_for_admission`, `recording`, `removed`, `ended`, `failed`, and `partial`.
- [ ] Improve join diagnostics with structured screenshots, HTML snapshots, current URL, visible page text, and detected Meet state in one machine-readable diagnostic file.
- [ ] Add automatic recovery when caption DOM selectors change, including selector telemetry in `bot.log`.
- [ ] Detect silent audio during recording and mark the meeting as `partial` if the audio stream is blank for too long.
- [ ] Add configurable retry rules for temporary ChromeDriver, PulseAudio, Calendar API, or network failures.
- [ ] Support concurrent meetings safely with isolated browser profiles, PulseAudio sinks, output folders, and logs.

## Transcript Quality

- [x] Write final transcript as JSON and plain text.
- [x] Deduplicate repeated incremental Meet captions before meeting intelligence analysis.
- [ ] Persist cleaned/deduplicated final transcript as a first-class artifact separate from raw Meet captions.
- [ ] Add optional Whisper or another speech-to-text backend as a fallback when Google Meet captions are disabled or unreliable.
- [ ] Merge Meet captions and audio transcription into a single best transcript with timestamps and confidence metadata.
- [ ] Improve speaker detection with participant roster scraping, caption speaker parsing, and optional voice diarization.
- [ ] Add punctuation, casing, paragraphing, and sentence-boundary cleanup for final transcripts.
- [ ] Add transcript search and filtering by speaker, timestamp, or keyword.

## Meeting Intelligence

- [x] Add a provider-neutral meeting intelligence interface.
- [x] Add a local rule-based provider as privacy-safe fallback.
- [x] Generate meeting intelligence artifacts: `meeting_intelligence.json` and `meeting_intelligence.md`.
- [x] Generate summary, key points, decisions, risks, action items, questions, blockers, and topics.
- [x] Add AI/LLM-generated summaries behind user-controlled provider settings.
- [x] Support OpenAI-compatible hosted APIs and self-hosted/local providers such as Ollama, LM Studio, and vLLM through one provider path.
- [x] Support Sarvam AI as a named LLM provider (`MEETING_LLM_PROVIDER=sarvam`).
- [x] Support `json_schema`, `json_object`, `text`, and omitted response-format modes for OpenAI-compatible gateways.
- [x] Parse LLM JSON from either `message.content` or `message.reasoning_content`.
- [x] Pass `reasoning_effort` to providers that support it (`MEETING_LLM_REASONING_EFFORT`).
- [x] Chunk long transcripts automatically and merge partial results for LLM providers with input length limits.
- [x] Fall back to rule-based intelligence when LLM generation fails.
- [x] Add a manual intelligence regeneration command for existing meeting folders.
- [ ] Add native provider adapters where OpenAI-compatible APIs are insufficient.
- [ ] Improve topic normalization so LLM topic items with partial fields still produce clean timestamps and summaries.
- [ ] Extract richer action items with owner, due date, source timestamp, and confidence using LLM-backed providers.
- [ ] Detect objections and unresolved topics with better semantic accuracy.
- [ ] Add meeting highlights with links back to audio/transcript timestamps.
- [ ] Add optional summary style presets for sales calls, standups, interviews, support calls, and product meetings without requiring a static template for every meeting.

## Email And Integrations

- [x] Send meeting summaries through SMTP.
- [x] Send structured HTML emails with plain-text fallback.
- [x] Record email delivery status, recipients, sent timestamp, and failure errors in `metadata.json`.
- [x] Keep meeting capture status independent from SMTP delivery status.
- [x] Add a manual summary email resend command for existing meeting folders.
- [ ] Add optional attachments for transcript, intelligence markdown, and/or audio artifacts.
- [ ] Add recipient modes for organizer-only and organizer-plus-attendees, with explicit opt-in.
- [ ] Push summaries and action items to Slack, Notion, Confluence, Linear, Jira, or HubSpot.
- [ ] Create calendar event follow-up notes automatically after the meeting ends.
- [ ] Add webhook events for `meeting_started`, `meeting_completed`, `meeting_failed`, `summary_ready`, and `summary_email_sent`.
- [ ] Store artifacts in S3, GCS, or another object store instead of only the local filesystem.
- [ ] Add a REST API to query meetings, transcripts, summaries, recordings, and delivery status.
- [ ] Add user/team-level settings for retention, sharing, summary format, and integration targets.

## Product Experience

- [ ] Build a web dashboard to browse meetings, recordings, transcripts, summaries, action items, and email delivery status.
- [ ] Add playback with transcript syncing and click-to-seek timestamps.
- [ ] Add editable transcripts and human correction workflow.
- [ ] Add editable summaries and action-item correction workflow.
- [ ] Add organization, workspace, and team concepts.
- [ ] Add shareable meeting pages with permissions.
- [ ] Add notification preferences for meeting completion and summary delivery.

## Security, Privacy, And Compliance

- [x] Keep LLM meeting intelligence opt-in through provider configuration.
- [x] Keep summary email delivery opt-in through `SUMMARY_EMAIL_ENABLED`.
- [x] Avoid automatic emailing to calendar attendees by default.
- [ ] Add explicit retention settings for audio, transcripts, logs, screenshots, and email delivery metadata.
- [ ] Add encryption-at-rest support for sensitive artifacts.
- [ ] Redact secrets, personal data, and sensitive keywords from logs.
- [ ] Redact SMTP passwords and LLM API keys from debug output.
- [ ] Add role-based access control for meeting artifacts.
- [ ] Add audit logs for who accessed, edited, exported, emailed, or deleted meeting data.
- [ ] Add consent and disclosure controls so meeting participants know the bot is recording.

## Operations And Scale

- [x] Add unit tests around browser/audio boundaries, transcript processing, LLM parsing, and SMTP sending.
- [ ] Add structured JSON logs and metrics for join success rate, caption health, audio health, intelligence generation time, and email delivery status.
- [ ] Add health checks for Chrome, PulseAudio, Calendar API, storage, LLM provider, SMTP provider, and worker queues.
- [ ] Move long-running post-processing into a job queue.
- [ ] Add database storage for meetings, artifacts, speaker metadata, summaries, and integration status.
- [ ] Add alerts for repeated join failures, blank recordings, missing transcripts, failed intelligence generation, SMTP failures, or expired credentials.
- [ ] Add deployment docs for Docker Compose, Kubernetes, and systemd production setups.

## Evaluation And Quality

- [ ] Create fixture-based tests using saved Meet DOM samples for caption parsing.
- [ ] Add audio validation tests that distinguish silence from real speech.
- [ ] Add golden-file tests for transcript cleanup and summary generation.
- [ ] Add golden-file tests for HTML email rendering.
- [ ] Track transcript word error rate when using an STT backend.
- [ ] Track summary quality with human review fields and regression examples.
- [ ] Add end-to-end test meetings in a controlled Google Workspace test account.
