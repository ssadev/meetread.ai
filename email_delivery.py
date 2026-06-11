from __future__ import annotations

import html
import json
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

from config import SETTINGS
from storage import MeetingStorage


SmtpFactory = Callable[..., Any]


def send_summary_email_for_meeting(
    meeting_dir: str | Path,
    metadata: dict[str, Any],
    settings: Any = SETTINGS,
    smtp_factory: SmtpFactory = smtplib.SMTP,
    smtp_ssl_factory: SmtpFactory = smtplib.SMTP_SSL,
) -> dict[str, Any]:
    if not getattr(settings, "summary_email_enabled", False):
        return {"summary_email_status": "disabled"}

    recipients = _recipients(settings)
    if not recipients:
        return {"summary_email_status": "skipped_no_recipients"}

    missing = _missing_smtp_settings(settings)
    if missing:
        return {
            "summary_email_status": "failed",
            "summary_email_error": f"Missing SMTP setting(s): {', '.join(missing)}",
        }

    meeting_path = Path(meeting_dir)
    try:
        intelligence = _read_json(meeting_path / "meeting_intelligence.json")
        message = build_summary_email(meeting_path, metadata, intelligence, recipients, settings)
        _send_message(message, settings, smtp_factory=smtp_factory, smtp_ssl_factory=smtp_ssl_factory)
    except Exception as exc:
        return {
            "summary_email_status": "failed",
            "summary_email_recipients": recipients,
            "summary_email_error": str(exc),
        }

    return {
        "summary_email_status": "sent",
        "summary_email_sent_at": datetime.now(timezone.utc).isoformat(),
        "summary_email_recipients": recipients,
    }


def build_summary_email(
    meeting_dir: str | Path,
    metadata: dict[str, Any],
    intelligence: dict[str, Any],
    recipients: list[str],
    settings: Any = SETTINGS,
) -> EmailMessage:
    title = metadata.get("title") or "Meeting"
    sender = getattr(settings, "smtp_from", "") or getattr(settings, "smtp_username", "")
    subject_prefix = getattr(settings, "smtp_subject_prefix", "[MeetRead]")
    message = EmailMessage()
    message["Subject"] = f"{subject_prefix} {title} summary"
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(_plain_text_body(meeting_dir, metadata, intelligence))
    message.add_alternative(_html_body(meeting_dir, metadata, intelligence), subtype="html")
    return message


def resend_summary_email(
    meeting_dir: str | Path,
    settings: Any = SETTINGS,
    smtp_factory: SmtpFactory = smtplib.SMTP,
    smtp_ssl_factory: SmtpFactory = smtplib.SMTP_SSL,
) -> dict[str, Any]:
    meeting_path = Path(meeting_dir)
    storage = MeetingStorage(meeting_path.parent)
    metadata = storage.read_metadata(meeting_path)
    if not metadata:
        raise FileNotFoundError(f"Missing metadata.json in {meeting_path}")
    updates = send_summary_email_for_meeting(
        meeting_path,
        metadata,
        settings=settings,
        smtp_factory=smtp_factory,
        smtp_ssl_factory=smtp_ssl_factory,
    )
    metadata.update(updates)
    storage.write_metadata(meeting_path, metadata)
    return updates


def _send_message(
    message: EmailMessage,
    settings: Any,
    smtp_factory: SmtpFactory,
    smtp_ssl_factory: SmtpFactory,
) -> None:
    host = getattr(settings, "smtp_host", "")
    port = int(getattr(settings, "smtp_port", 587))
    timeout = int(getattr(settings, "smtp_timeout_seconds", 30))
    username = getattr(settings, "smtp_username", "")
    password = getattr(settings, "smtp_password", "")
    use_ssl = bool(getattr(settings, "smtp_use_ssl", False))
    use_tls = bool(getattr(settings, "smtp_use_tls", True))
    factory = smtp_ssl_factory if use_ssl else smtp_factory
    with factory(host, port, timeout=timeout) as smtp:
        if use_tls and not use_ssl:
            smtp.starttls()
        if username or password:
            smtp.login(username, password)
        smtp.send_message(message)


def _plain_text_body(meeting_dir: str | Path, metadata: dict[str, Any], intelligence: dict[str, Any]) -> str:
    lines = [
        metadata.get("title") or "Meeting",
        "",
        _meeting_details(metadata),
        "",
        "Summary",
        _summary(intelligence),
        "",
        _plain_items("Key Points", intelligence.get("key_points", [])),
        _plain_items("Decisions", intelligence.get("decisions", [])),
        _plain_action_items(intelligence.get("action_items", [])),
        _plain_items("Risks", intelligence.get("risks", [])),
        _plain_items("Blockers", intelligence.get("blockers", [])),
        _plain_items("Questions", intelligence.get("questions", [])),
        _plain_artifacts(meeting_dir, metadata),
    ]
    return "\n".join(section.rstrip() for section in lines if section is not None).strip() + "\n"


def _html_body(meeting_dir: str | Path, metadata: dict[str, Any], intelligence: dict[str, Any]) -> str:
    title = html.escape(metadata.get("title") or "Meeting")
    preheader = html.escape(_summary(intelligence)[:140])
    body = "".join(
        [
            _hero(metadata, intelligence),
            _summary_card(intelligence),
            _section_card("Key Points", intelligence.get("key_points", []), "#2563eb"),
            _section_card("Decisions", intelligence.get("decisions", []), "#16a34a"),
            _action_card(intelligence.get("action_items", [])),
            _section_card("Risks", intelligence.get("risks", []), "#dc2626"),
            _section_card("Blockers", intelligence.get("blockers", []), "#9333ea"),
            _section_card("Questions", intelligence.get("questions", []), "#0f766e"),
            _artifact_card(meeting_dir, metadata),
        ]
    )
    return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title}</title>
  </head>
  <body style="margin:0;padding:0;background:#f4f7fb;font-family:Arial,Helvetica,sans-serif;color:#172033;">
    <div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">{preheader}</div>
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f7fb;margin:0;padding:24px 0;">
      <tr>
        <td align="center" style="padding:0 12px;">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:720px;background:#ffffff;border:1px solid #d9e2ef;border-radius:12px;overflow:hidden;">
            <tr>
              <td style="padding:0;">
                {body}
              </td>
            </tr>
          </table>
          <p style="margin:14px 0 0;color:#64748b;font-size:12px;line-height:18px;">Generated by MeetRead AI</p>
        </td>
      </tr>
    </table>
  </body>
</html>"""


def _meeting_details(metadata: dict[str, Any]) -> str:
    return (
        f"Status: {metadata.get('status', 'unknown')} | "
        f"Duration: {metadata.get('duration_seconds', 0)}s | "
        f"Joined: {metadata.get('actual_join_time') or 'unknown'} | "
        f"Left: {metadata.get('actual_leave_time') or 'unknown'}"
    )


def _hero(metadata: dict[str, Any], intelligence: dict[str, Any]) -> str:
    title = html.escape(metadata.get("title") or "Meeting")
    provider = html.escape(str(intelligence.get("provider") or "unknown"))
    status = html.escape(str(metadata.get("status") or "unknown"))
    duration = _format_duration(metadata.get("duration_seconds", 0))
    joined = _short_time(metadata.get("actual_join_time"))
    left = _short_time(metadata.get("actual_leave_time"))
    return f"""
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#0f172a;">
  <tr>
    <td style="padding:28px 32px;">
      <div style="color:#93c5fd;font-size:12px;font-weight:bold;letter-spacing:.08em;text-transform:uppercase;">MeetRead Summary</div>
      <h1 style="margin:8px 0 16px;color:#ffffff;font-size:28px;line-height:34px;font-weight:700;">{title}</h1>
      <table role="presentation" cellspacing="0" cellpadding="0">
        <tr>
          {_stat_cell("Status", status)}
          {_stat_cell("Duration", duration)}
          {_stat_cell("Joined", joined)}
          {_stat_cell("Left", left)}
          {_stat_cell("Provider", provider)}
        </tr>
      </table>
    </td>
  </tr>
</table>"""


def _stat_cell(label: str, value: str) -> str:
    return (
        '<td style="padding:0 8px 8px 0;">'
        '<table role="presentation" cellspacing="0" cellpadding="0" style="background:#1e293b;border-radius:8px;">'
        f'<tr><td style="padding:8px 10px;color:#cbd5e1;font-size:11px;line-height:14px;">{html.escape(label)}<br>'
        f'<span style="color:#ffffff;font-size:13px;font-weight:bold;">{html.escape(value)}</span></td></tr>'
        "</table></td>"
    )


def _summary_card(intelligence: dict[str, Any]) -> str:
    return _card(
        "Summary",
        f'<p style="margin:0;color:#1f2937;font-size:15px;line-height:23px;">{html.escape(_summary(intelligence))}</p>',
    )


def _section_card(title: str, items: Any, accent: str) -> str:
    normalized = _item_list(items)
    if not normalized:
        content = '<p style="margin:0;color:#64748b;font-size:14px;line-height:21px;">None detected.</p>'
    else:
        content = '<table role="presentation" width="100%" cellspacing="0" cellpadding="0">'
        for item in normalized:
            content += _bullet_row(_item_text(item), accent)
        content += "</table>"
    return _card(title, content)


def _action_card(items: Any) -> str:
    normalized = _item_list(items)
    if not normalized:
        content = '<p style="margin:0;color:#64748b;font-size:14px;line-height:21px;">None detected.</p>'
        return _card("Action Items", content)
    rows = ""
    for item in normalized:
        owner = html.escape(str(item.get("owner") or "Unassigned"))
        task = html.escape(str(item.get("task") or item.get("text") or ""))
        due = html.escape(str(item.get("due_date") or "No due date"))
        rows += (
            '<tr>'
            f'<td style="padding:10px;border-top:1px solid #e2e8f0;color:#1f2937;font-size:14px;line-height:20px;">{task}</td>'
            f'<td style="padding:10px;border-top:1px solid #e2e8f0;color:#475569;font-size:13px;line-height:20px;">{owner}</td>'
            f'<td style="padding:10px;border-top:1px solid #e2e8f0;color:#475569;font-size:13px;line-height:20px;">{due}</td>'
            "</tr>"
        )
    content = (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border:1px solid #e2e8f0;border-radius:8px;overflow:hidden;">'
        '<tr style="background:#f8fafc;">'
        '<th align="left" style="padding:10px;color:#334155;font-size:12px;text-transform:uppercase;letter-spacing:.04em;">Task</th>'
        '<th align="left" style="padding:10px;color:#334155;font-size:12px;text-transform:uppercase;letter-spacing:.04em;">Owner</th>'
        '<th align="left" style="padding:10px;color:#334155;font-size:12px;text-transform:uppercase;letter-spacing:.04em;">Due</th>'
        f"</tr>{rows}</table>"
    )
    return _card("Action Items", content)


def _artifact_card(meeting_dir: str | Path, metadata: dict[str, Any]) -> str:
    items = "".join(
        f'<li style="margin:0 0 6px;color:#475569;font-size:13px;line-height:19px;">{html.escape(name)}</li>'
        for name in _artifact_names(metadata)
    )
    folder = html.escape(str(Path(meeting_dir)))
    content = (
        f'<ul style="margin:0 0 10px 18px;padding:0;">{items}</ul>'
        f'<p style="margin:0;color:#64748b;font-size:12px;line-height:18px;">Folder: {folder}</p>'
    )
    return _card("Artifacts", content)


def _bullet_row(text: str, accent: str) -> str:
    return (
        '<tr>'
        f'<td valign="top" style="width:18px;padding:3px 10px 9px 0;"><span style="display:inline-block;width:8px;height:8px;border-radius:8px;background:{accent};"></span></td>'
        f'<td style="padding:0 0 9px;color:#1f2937;font-size:14px;line-height:21px;">{html.escape(text)}</td>'
        "</tr>"
    )


def _card(title: str, content: str) -> str:
    return f"""
<table role="presentation" width="100%" cellspacing="0" cellpadding="0">
  <tr>
    <td style="padding:22px 32px 0;">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border:1px solid #e2e8f0;border-radius:10px;">
        <tr>
          <td style="padding:18px 20px;">
            <h2 style="margin:0 0 12px;color:#0f172a;font-size:17px;line-height:23px;">{html.escape(title)}</h2>
            {content}
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>"""


def _format_duration(seconds: Any) -> str:
    try:
        total = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    minutes, secs = divmod(total, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _short_time(value: Any) -> str:
    if not value:
        return "unknown"
    text = str(value)
    if "T" in text:
        return text.split("T", 1)[1][:5]
    return text[:16]


def _summary(intelligence: dict[str, Any]) -> str:
    return intelligence.get("summary") or "No meeting summary was generated."


def _plain_items(title: str, items: Any) -> str:
    lines = [title]
    normalized = _item_list(items)
    if not normalized:
        lines.append("- None detected.")
    for item in normalized:
        lines.append(f"- {_item_text(item)}")
    return "\n".join(lines) + "\n"


def _plain_action_items(items: Any) -> str:
    lines = ["Action Items"]
    normalized = _item_list(items)
    if not normalized:
        lines.append("- None detected.")
    for item in normalized:
        owner = item.get("owner") or "Unassigned"
        due = f" due {item['due_date']}" if item.get("due_date") else ""
        lines.append(f"- {owner}: {item.get('task') or item.get('text') or ''}{due}")
    return "\n".join(lines) + "\n"


def _plain_artifacts(meeting_dir: str | Path, metadata: dict[str, Any]) -> str:
    lines = ["Artifacts"]
    for name in _artifact_names(metadata):
        lines.append(f"- {name}")
    lines.append(f"- folder: {Path(meeting_dir)}")
    return "\n".join(lines)


def _html_items(title: str, items: Any) -> str:
    normalized = _item_list(items)
    if not normalized:
        return f"<h2>{html.escape(title)}</h2><ul><li>None detected.</li></ul>"
    items_html = "".join(f"<li>{html.escape(_item_text(item))}</li>" for item in normalized)
    return f"<h2>{html.escape(title)}</h2><ul>{items_html}</ul>"


def _html_action_items(items: Any) -> str:
    normalized = _item_list(items)
    if not normalized:
        return "<h2>Action Items</h2><ul><li>None detected.</li></ul>"
    rows = []
    for item in normalized:
        owner = item.get("owner") or "Unassigned"
        due = f" due {item['due_date']}" if item.get("due_date") else ""
        task = item.get("task") or item.get("text") or ""
        rows.append(f"<li>{html.escape(owner)}: {html.escape(task)}{html.escape(due)}</li>")
    return "<h2>Action Items</h2><ul>" + "".join(rows) + "</ul>"


def _html_artifacts(meeting_dir: str | Path, metadata: dict[str, Any]) -> str:
    items = "".join(f"<li>{html.escape(name)}</li>" for name in _artifact_names(metadata))
    items += f"<li>folder: {html.escape(str(Path(meeting_dir)))}</li>"
    return f"<h2>Artifacts</h2><ul>{items}</ul>"


def _artifact_names(metadata: dict[str, Any]) -> list[str]:
    names = []
    for key in (
        "transcript_file",
        "meeting_intelligence_markdown_file",
        "meeting_intelligence_file",
        "audio_file",
        "audio_mp3_file",
    ):
        if metadata.get(key):
            names.append(str(metadata[key]))
    return names


def _item_list(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _item_text(item: dict[str, Any]) -> str:
    timestamp = item.get("source_timestamp")
    speaker = item.get("source_speaker")
    text = item.get("text") or item.get("summary") or item.get("title") or ""
    prefix = ""
    if timestamp:
        prefix += f"[{timestamp}] "
    if speaker:
        prefix += f"{speaker}: "
    return prefix + str(text)


def _recipients(settings: Any) -> list[str]:
    recipients = getattr(settings, "smtp_summary_recipients", [])
    if isinstance(recipients, str):
        recipients = [item.strip() for item in recipients.split(",") if item.strip()]
    return list(recipients or [])


def _missing_smtp_settings(settings: Any) -> list[str]:
    missing = []
    if not getattr(settings, "smtp_host", ""):
        missing.append("SMTP_HOST")
    if not (getattr(settings, "smtp_from", "") or getattr(settings, "smtp_username", "")):
        missing.append("SMTP_FROM")
    return missing


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) != 1:
        print("Usage: python3 -m email_delivery <meeting_dir>", file=sys.stderr)
        return 2
    updates = resend_summary_email(args[0])
    print(f"Summary email status: {updates.get('summary_email_status')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
