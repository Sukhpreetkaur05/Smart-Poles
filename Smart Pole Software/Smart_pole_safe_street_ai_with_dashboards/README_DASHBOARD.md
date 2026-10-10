# Suraksha Dashboards (integrated)

Both dashboards start automatically with `python app.py`:

| URL | Dashboard |
|---|---|
| http://localhost:8000/ | Landing page |
| http://localhost:8000/citizen | Suraksha SOS Dashboard (citizen side) |
| http://localhost:8000/control | Control Room Dashboard (operator side) |

Port: set `DASHBOARD_PORT` in `.env` (default 8000). On a phone use `http://<laptop-ip>:8000/citizen`;
browsers allow GPS/camera/mic only on localhost or HTTPS, so phone use of those needs an HTTPS tunnel.

## Run
```
python -m pip install -r requirements.txt
python app.py                  # AI + cameras + both dashboards
python dashboard_server.py     # dashboards only (history + citizen SOS, no AI)
```

## How it is wired
```
app.py (AI, ESP32, key E) --register_incident / attach_media / publish_frame--> dashboard_server.py <-- browsers
citizen SOS button -------------------------------------> starts camera evidence recording
```
* AI incidents and ESP32 button presses show as **New** in the Control Room; the buzzer sounds (click the page once
  to allow audio), **Acknowledge** stops it, **Resolve** requires a note.
* Evidence photo/video attach to the incident when the 15 s recording is finalised.
* Citizen **SOS** sends GPS + message, triggers camera recording, and records 15 s video from the browser.
  **Cancel Alert** marks it cancelled.
* Live camera feeds = MJPEG previews of the annotated AI frames. Pole status comes from the ESP32 (armed/pir/ldr).
* Existing `evidence/` and `events/` files are imported once into History & Reports.
* Data: `dashboard_data/incidents.json`. CSV: `/api/export.csv`. PDF: "Download PDF report" then Save as PDF.

## Assumptions
* `CAM01` = pole `P-01`, `CAM02` = `P-02`...; sector names are placeholders (`SECTORS` in dashboard_server.py).
* Night = ESP32 `ldr` equals 1 (change with `DASHBOARD_LDR_DARK_VALUE`).
* Translator is an offline phrase table (`PHRASES` in dashboard/sos.html), not a translation service.
* Emergency contacts live in the browser only; real alerts still go through the existing email system.
* No login on the dashboards: keep them on a trusted network.
* `.env` holds an email password: remove it before sharing the folder.
