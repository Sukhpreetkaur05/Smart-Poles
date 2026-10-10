# SOS Email — Disposable Sender Demo

This version uses a disposable **sender**, not a disposable recipient.

```text
Laptop webcam
   -> YOLO pose/tracking
   -> SOS / fall / interaction heuristic
   -> evidence saved locally
   -> fresh NenzMail temporary sender
   -> your normal email inbox
   -> phone mail notification
```

NenzMail currently documents a free API that can create temporary inboxes and send an email from the generated inbox to another address. It also documents automatic expiry and an API endpoint for immediate deletion. See https://nenzmail.com/ and its API guide before using the service.

## 1. Configure the recipient

Copy `.env.example` to `.env`:

```powershell
Copy-Item .env.example .env
```

Then set:

```env
EMAIL_ENABLED=1
TEMP_SENDER_ENABLED=1
TEMP_SENDER_DELETE=1
TEMP_SENDER_DELETE_DELAY=5
SOS_TO_EMAIL=your_email@gmail.com
NENZMAIL_API=https://api.nenzmail.com/api
```

You do **not** need a Gmail SMTP password in this version.

## 2. Test email without the webcam

```powershell
python test_email.py
```

The script will:
1. create a fresh temporary sender;
2. send the test SOS email to `SOS_TO_EMAIL`;
3. request deletion of the temporary sender a few seconds later.

The sender is not your real email address. Your `SOS_TO_EMAIL` remains the receiver.

## 3. Run the webcam AI

```powershell
python app.py
```

Press `E` in the webcam window to trigger a manual test SOS. For an automatically detected SOS, the current prototype uses its pose/movement heuristics.

The incident evidence is still stored locally in `events/`. This email demo does not upload the evidence image to the public temp-mail service.

## Important

Disposable mail services are for testing, not production emergency dispatch. Do not send sensitive real incident data through a public temporary-email provider. For deployment, replace the temp sender with an authenticated private mail/SMS/push service and add human verification, encryption, access control, retention policies, and operational safeguards.
