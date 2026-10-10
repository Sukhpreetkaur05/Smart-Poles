# Safe Street AI — Laptop Webcam Prototype

AI-assisted public-safety prototype using a laptop webcam, YOLO pose/tracking, temporal movement heuristics, evidence capture, night enhancement, and optional alerts.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python test_email.py
python app.py
```

## Email alert mode

This build uses a **fresh disposable sender** for testing. Set your real receiving email in `.env` as `SOS_TO_EMAIL`. No SMTP/Gmail password is required.

See `README_EMAIL.md` for details.

## Controls

- `Q` / `Esc`: quit
- `E`: manual SOS test
- `N`: toggle forced night enhancement mode

## Safety / accuracy note

The violence, choking-like, fall, and SOS detections are prototype heuristics. They are not a certified crime classifier and can produce false positives or false negatives. Real deployment needs validated action-recognition models, held-out testing, human verification, security controls, and appropriate legal/operational review.

## Dashboards

The Suraksha SOS (citizen) and Control Room dashboards are built in and start with `python app.py`:
`http://localhost:8000/citizen` and `http://localhost:8000/control`. See `README_DASHBOARD.md`.
