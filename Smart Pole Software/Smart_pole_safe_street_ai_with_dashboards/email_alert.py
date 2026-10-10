from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable, Optional, Tuple


EMAIL_ENABLED = (
    os.getenv("EMAIL_ENABLED", "1").strip().lower()
    in {"1", "true", "yes", "on"}
)

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USERNAME).strip()
SOS_TO_EMAIL = os.getenv("SOS_TO_EMAIL", "").strip()
EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", "60"))
MAX_TOTAL_ATTACHMENT_MB = float(
    os.getenv("MAX_TOTAL_ATTACHMENT_MB", "18")
)


def email_configured() -> bool:
    return bool(
        EMAIL_ENABLED
        and SMTP_USERNAME
        and SMTP_PASSWORD
        and SMTP_FROM
        and SOS_TO_EMAIL
    )


def _attach(message: EmailMessage, path: Path) -> None:
    suffix = path.suffix.lower()

    if suffix in {".jpg", ".jpeg"}:
        maintype, subtype = "image", "jpeg"
    elif suffix == ".png":
        maintype, subtype = "image", "png"
    elif suffix == ".mp4":
        maintype, subtype = "video", "mp4"
    elif suffix in {".wav", ".wave"}:
        maintype, subtype = "audio", "wav"
    else:
        maintype, subtype = "application", "octet-stream"

    message.add_attachment(
        path.read_bytes(),
        maintype=maintype,
        subtype=subtype,
        filename=path.name,
    )


def _smtp_send(message: EmailMessage) -> None:
    context = ssl.create_default_context()

    # Gmail SMTP SSL on port 465.
    with smtplib.SMTP_SSL(
        SMTP_HOST,
        465,
        timeout=EMAIL_TIMEOUT,
        context=context,
    ) as server:
        server.ehlo()
        server.login(
            SMTP_USERNAME,
            SMTP_PASSWORD,
        )
        server.send_message(
            message,
            from_addr=SMTP_FROM,
            to_addrs=[message["To"]],
        )


def _base_payload(payload: dict) -> str:
    event = payload.get("event_type", "UNKNOWN")
    confidence = payload.get("confidence", "n/a")
    reason = payload.get("reason", "n/a")
    camera = payload.get("camera_id", "n/a")
    timestamp = payload.get("timestamp", "n/a")
    incident_id = payload.get("incident_id", "n/a")

    return (
        "🚨 SAFE STREET AI — IMMEDIATE ALERT\n\n"
        f"Incident ID: {incident_id}\n"
        f"Situation: {payload.get('situation', event)}\n"
        f"Event: {event}\n"
        f"Confidence: {confidence}\n"
        f"Reason: {reason}\n"
        f"Camera: {camera}\n"
        f"Time: {timestamp}\n\n"
        "Evidence recording has started.\n"
        "A detailed evidence email will follow.\n\n"
        "AI-assisted prototype alert. Human verification required."
    )


def send_text_alert(
    payload: dict,
    recipient: Optional[str] = None,
) -> Tuple[bool, str]:
    if not email_configured():
        return False, "Email is not configured."

    target = (recipient or SOS_TO_EMAIL).strip()
    if not target:
        return False, "SOS_TO_EMAIL is empty."

    message = EmailMessage()
    message["From"] = SMTP_FROM
    message["To"] = target
    message["Subject"] = (
        "[SAFE STREET AI] 🚨 IMMEDIATE SOS ALERT - "
        f"{payload.get('event_type', 'UNKNOWN')}"
    )

    body = _base_payload(payload)
    message.set_content(body)

    try:
        print("[EMAIL] Sending IMMEDIATE text alert...")
        _smtp_send(message)
        print("[EMAIL] Immediate alert submitted.")
        return True, "Immediate SOS text alert sent."
    except smtplib.SMTPAuthenticationError as exc:
        return False, f"Gmail authentication failed: {exc}"
    except Exception as exc:
        return False, f"Immediate alert failed: {exc}"


def send_evidence_email(
    payload: dict,
    attachment_paths: Optional[Iterable[str]] = None,
    recipient: Optional[str] = None,
) -> Tuple[bool, str]:
    if not email_configured():
        return False, "Email is not configured."

    target = (recipient or SOS_TO_EMAIL).strip()
    if not target:
        return False, "SOS_TO_EMAIL is empty."

    paths = []
    for raw in attachment_paths or []:
        if not raw:
            continue
        path = Path(raw)
        if path.is_file():
            paths.append(path)
        else:
            print("[EMAIL] Evidence file missing:", path)

    total_mb = sum(
        p.stat().st_size / 1024 / 1024
        for p in paths
    )

    if total_mb > MAX_TOTAL_ATTACHMENT_MB:
        return False, (
            f"Evidence is {total_mb:.2f} MB, above the configured "
            f"{MAX_TOTAL_ATTACHMENT_MB:.2f} MB attachment limit."
        )

    message = EmailMessage()
    message["From"] = SMTP_FROM
    message["To"] = target
    message["Subject"] = (
        "[SAFE STREET AI] INCIDENT EVIDENCE - "
        f"{payload.get('event_type', 'UNKNOWN')}"
    )

    body = (
        "🚨 SAFE STREET AI — INCIDENT EVIDENCE\n\n"
        f"Incident ID: {payload.get('incident_id', 'n/a')}\n"
        f"Situation: {payload.get('situation', payload.get('event_type', 'UNKNOWN'))}\n"
        f"Event: {payload.get('event_type', 'UNKNOWN')}\n"
        f"Confidence: {payload.get('confidence', 'n/a')}\n"
        f"Camera: {payload.get('camera_id', 'n/a')}\n"
        f"Time: {payload.get('timestamp', 'n/a')}\n\n"
        "Evidence files are attached to this email.\n"
        "Please verify the incident before taking action."
    )

    message.set_content(body)

    for path in paths:
        print(
            f"[EMAIL] Adding evidence: {path.name} "
            f"({path.stat().st_size / 1024 / 1024:.2f} MB)"
        )
        _attach(message, path)

    try:
        print("[EMAIL] Sending evidence email...")
        _smtp_send(message)
        print("[EMAIL] Evidence email submitted.")
        return True, (
            f"Evidence email sent with {len(paths)} attachment(s)."
        )
    except smtplib.SMTPAuthenticationError as exc:
        return False, f"Gmail authentication failed: {exc}"
    except Exception as exc:
        return False, f"Evidence email failed: {exc}"


def send_email(
    payload: dict,
    recipient: Optional[str] = None,
    attachment_path: Optional[str] = None,
    attachment_paths: Optional[Iterable[str]] = None,
):
    """Backward-compatible wrapper used by older code."""
    paths = []
    if attachment_paths:
        paths.extend(attachment_paths)
    elif attachment_path:
        paths.append(attachment_path)

    if paths:
        return send_evidence_email(payload, paths, recipient)
    return send_text_alert(payload, recipient)
