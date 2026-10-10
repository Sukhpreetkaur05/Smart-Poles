"""Direct SMTP email test for Safe Street AI."""

from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ENV_PATH = Path(__file__).with_name(".env")
load_dotenv(ENV_PATH)

from email_alert import email_configured, send_email

print("--- Safe Street AI Direct SMTP Test ---")
print(".env:", ENV_PATH)

if not email_configured():
    raise SystemExit(
        "Email is not configured. Check SMTP_USERNAME, SMTP_PASSWORD, "
        "SMTP_FROM and SOS_TO_EMAIL in .env."
    )

payload = {
    "event_type": "TEST_SOS_EMAIL",
    "confidence": 1.0,
    "reason": "User ran test_email.py",
    "camera_id": "LAPTOP_CAM_TEST",
    "timestamp": datetime.now().isoformat(timespec="seconds"),
    "person_id": None,
}

ok, message = send_email(payload)
print(message)

if not ok:
    raise SystemExit(1)

print("\n✅ Test email submitted successfully.")
print("Check the receiver inbox, Spam/Junk, and All Mail if needed.")
