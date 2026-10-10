from __future__ import annotations

import json
import math
import os
import smtplib
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
from dotenv import load_dotenv

load_dotenv()

from ultralytics import YOLO  # noqa: E402

from camera_source import CameraSource  # noqa: E402
import dashboard_server as dashboard  # noqa: E402

# ============================================================
# CONFIG (.env)
# ============================================================


def env_str(name, default=""):
    return os.getenv(name, default).strip()


def env_int(name, default):
    try:
        return int(env_str(name, str(default)))
    except ValueError:
        return default


def env_float(name, default):
    try:
        return float(env_str(name, str(default)))
    except ValueError:
        return default


EMAIL_ENABLED = env_int("EMAIL_ENABLED", 0) == 1
SMTP_HOST = env_str("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = env_int("SMTP_PORT", 465)
SMTP_USE_TLS = env_int("SMTP_USE_TLS", 0) == 1
SMTP_USE_SSL = env_int("SMTP_USE_SSL", 1) == 1
SMTP_USERNAME = env_str("SMTP_USERNAME")
SMTP_PASSWORD = env_str("SMTP_PASSWORD").replace(" ", "")
SMTP_FROM = env_str("SMTP_FROM", SMTP_USERNAME)
SOS_TO_EMAIL = env_str("SOS_TO_EMAIL")
EMAIL_TIMEOUT = env_int("EMAIL_TIMEOUT", 60)
MAX_TOTAL_ATTACHMENT_MB = env_float("MAX_TOTAL_ATTACHMENT_MB", 18)

MODEL_NAME = env_str("MODEL_NAME", "yolo26n-pose.pt")
CONF = env_float("CONF", 0.35)
IMG_SIZE = env_int("IMG_SIZE", 416)
PROCESS_EVERY_N = max(1, env_int("PROCESS_EVERY_N", 1))

PRE_EVENT_SECONDS = env_float("PRE_EVENT_SECONDS", 3)
POST_EVENT_SECONDS = env_float("POST_EVENT_SECONDS", 15)
EVENT_CONFIRM_SECONDS = env_float("EVENT_CONFIRM_SECONDS", 0.5)
EVENT_COOLDOWN_SECONDS = env_float("EVENT_COOLDOWN_SECONDS", 15)

ALERT_ALL_CAMERAS = env_int("ALERT_ALL_CAMERAS", 1) == 1
PRIORITY_DURATION_SECONDS = env_float("PRIORITY_DURATION_SECONDS", 30)

# cross-camera same-person search
HANDOFF_THRESHOLD = env_float("HANDOFF_THRESHOLD", 0.70)
HANDOFF_CONFIRM_FRAMES = max(1, env_int("HANDOFF_CONFIRM_FRAMES", 2))
HANDOFF_COOLDOWN_SECONDS = env_float("HANDOFF_COOLDOWN_SECONDS", 60)
TARGET_TTL_SECONDS = env_float("TARGET_TTL_SECONDS", 600)

EVIDENCE_DIR = "evidence"
os.makedirs(EVIDENCE_DIR, exist_ok=True)


def parse_cameras(raw):
    """CAMERA_SOURCES=CAM01=url|CAM02=url  (or a single NAME=url / url)."""
    cams = {}
    if not raw:
        cams["WEBCAM"] = 0
        return cams
    for i, part in enumerate(raw.split("|")):
        part = part.strip()
        if not part:
            continue
        if "=" in part and not part.lower().startswith(("http", "rtsp")):
            name, src = part.split("=", 1)
        else:
            name, src = f"CAM{i + 1:02d}", part
        name, src = name.strip(), src.strip()
        cams[name] = int(src) if src.isdigit() else src
    return cams


def parse_graph(raw):
    """CAMERA_GRAPH=CAM01:CAM02,CAM03;CAM02:CAM03"""
    graph = {}
    for block in raw.split(";"):
        if ":" not in block:
            continue
        a, others = block.split(":", 1)
        for b in [x.strip() for x in others.split(",") if x.strip()]:
            graph.setdefault(a.strip(), set()).add(b)
            graph.setdefault(b, set()).add(a.strip())
    return graph


CAMERAS = parse_cameras(env_str("CAMERA_SOURCES"))
GRAPH = parse_graph(env_str("CAMERA_GRAPH"))

# ============================================================
# EMAIL (built in)
# ============================================================


def email_configured():
    return bool(
        EMAIL_ENABLED and SMTP_USERNAME and SMTP_PASSWORD and SOS_TO_EMAIL
    )


def _send(msg):
    if SMTP_USE_SSL:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=EMAIL_TIMEOUT)
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=EMAIL_TIMEOUT)
        if SMTP_USE_TLS:
            server.starttls()
    try:
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def send_text_alert(inc):
    if not email_configured():
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = f"SOS ALERT: {inc['type']} on {inc['camera']}"
        msg["From"] = SMTP_FROM
        msg["To"] = SOS_TO_EMAIL
        msg.set_content(
            f"Incident: {inc['type']}\n"
            f"Camera: {inc['camera']}\n"
            f"Time: {inc['time_str']}\n"
            f"Incident ID: {inc['id']}\n\n"
            "Evidence (photo + video) will follow in a second email."
        )
        _send(msg)
        print(f"[EMAIL] Text alert sent ({inc['id']})")
    except Exception as exc:
        print(f"[EMAIL] Text alert FAILED: {exc}")


def send_evidence_email(inc, jpg_path, mp4_path):
    if not email_configured():
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = f"EVIDENCE: {inc['type']} on {inc['camera']}"
        msg["From"] = SMTP_FROM
        msg["To"] = SOS_TO_EMAIL

        limit = MAX_TOTAL_ATTACHMENT_MB * 1024 * 1024
        total = 0
        notes = []
        files = []
        for path, mime in ((jpg_path, "image/jpeg"), (mp4_path, "video/mp4")):
            if path and os.path.exists(path):
                size = os.path.getsize(path)
                if total + size <= limit:
                    files.append((path, mime))
                    total += size
                else:
                    notes.append(f"{os.path.basename(path)} too large, saved locally.")

        msg.set_content(
            f"Incident: {inc['type']}\nCamera: {inc['camera']}\n"
            f"Time: {inc['time_str']}\nIncident ID: {inc['id']}\n"
            + ("\n".join(notes) if notes else "")
        )
        for path, mime in files:
            maintype, subtype = mime.split("/")
            with open(path, "rb") as f:
                msg.add_attachment(
                    f.read(),
                    maintype=maintype,
                    subtype=subtype,
                    filename=os.path.basename(path),
                )
        _send(msg)
        print(f"[EMAIL] Evidence email sent ({inc['id']})")
    except Exception as exc:
        print(f"[EMAIL] Evidence email FAILED: {exc}")


def send_match_email(target, cam_name, time_str, sim, jpg_path):
    # JPEG-only warning when the incident person shows up on another camera
    if not email_configured():
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = f"WARNING: same person seen at {cam_name}"
        msg["From"] = SMTP_FROM
        msg["To"] = SOS_TO_EMAIL
        msg.set_content(
            "WARNING: The person from an earlier incident has been spotted "
            "on another camera.\n\n"
            f"Seen now at: {cam_name}\n"
            f"Time: {time_str}\n"
            f"Match confidence: {sim * 100:.0f}% (clothing/appearance based)\n\n"
            f"Original incident: {target.incident_type} on {target.camera}\n"
            f"Original time: {target.time_str}\n"
            f"Incident ID: {target.incident_id}\n\n"
            "This is an anonymous appearance match, not identity recognition. "
            "Please verify the attached photo manually."
        )
        with open(jpg_path, "rb") as f:
            msg.add_attachment(
                f.read(),
                maintype="image",
                subtype="jpeg",
                filename=os.path.basename(jpg_path),
            )
        _send(msg)
        print(f"[EMAIL] Match photo sent ({cam_name})")
    except Exception as exc:
        print(f"[EMAIL] Match email FAILED: {exc}")


# ============================================================
# POSE ANALYSIS
# ============================================================

NOSE, L_SH, R_SH, L_WR, R_WR, L_HIP, R_HIP = 0, 5, 6, 9, 10, 11, 12
KP_OK = 0.3

SKELETON = [
    (5, 6), (5, 7), (7, 9), (6, 8), (8, 10), (5, 11), (6, 12),
    (11, 12), (11, 13), (13, 15), (12, 14), (14, 16),
]


class Person:
    def __init__(self, pid, box, kp, kc):
        self.id = pid
        self.box = box
        self.kp = kp
        self.kc = kc
        x1, y1, x2, y2 = box
        self.w = max(1.0, x2 - x1)
        self.h = max(1.0, y2 - y1)
        self.cx = (x1 + x2) / 2
        self.cy = (y1 + y2) / 2

    def pt(self, i):
        if self.kc[i] >= KP_OK:
            return self.kp[i]
        return None

    def neck(self):
        ls, rs = self.pt(L_SH), self.pt(R_SH)
        if ls is not None and rs is not None:
            return (ls + rs) / 2
        n = self.pt(NOSE)
        return n


class Analyzer:
    """Per-camera detector for SOS / fall / struggle / neck-grab."""

    def __init__(self):
        self.history = defaultdict(lambda: deque(maxlen=60))
        self.wrist_hist = defaultdict(lambda: deque(maxlen=12))
        self.first_seen = {}
        self.last_seen = {}

    def _update(self, p, now):
        self.history[p.id].append((now, p.cx, p.cy, p.w, p.h))
        wr = []
        for i in (L_WR, R_WR):
            pt = p.pt(i)
            wr.append(None if pt is None else (pt[0] / p.h, pt[1] / p.h))
        self.wrist_hist[p.id].append((now, wr))

    def _wrist_speed(self, pid):
        hist = self.wrist_hist[pid]
        if len(hist) < 4:
            return 0.0
        speeds = []
        for k in (0, 1):
            pts = [(t, w[k]) for t, w in hist if w[k] is not None]
            if len(pts) >= 3:
                d = 0.0
                for (t0, a), (t1, b) in zip(pts, pts[1:]):
                    d += math.hypot(b[0] - a[0], b[1] - a[1])
                dt = pts[-1][0] - pts[0][0]
                if dt > 0:
                    speeds.append(d / dt)
        return max(speeds) if speeds else 0.0

    def _sos(self, p):
        nose, lw, rw = p.pt(NOSE), p.pt(L_WR), p.pt(R_WR)
        if nose is None or lw is None or rw is None:
            return False
        margin = 0.05 * p.h
        return lw[1] < nose[1] - margin and rw[1] < nose[1] - margin

    def _fall(self, p, now):
        hist = self.history[p.id]
        if len(hist) < 5:
            return False
        horizontal_now = p.w > 1.2 * p.h
        if not horizontal_now:
            return False
        for t, cx, cy, w, h in hist:
            if now - t > 2.0:
                continue
            was_upright = h > 1.2 * w
            dropped = (p.cy - cy) > 0.25 * h
            if was_upright and dropped:
                return True
        return False

    def analyze(self, persons, now):
        """Return dict {event_type: set(person ids)} for conditions met now."""
        flags = {}
        for p in persons:
            self._update(p, now)
            if self._sos(p):
                flags.setdefault("SOS gesture", set()).add(p.id)
            if self._fall(p, now):
                flags.setdefault("Fall", set()).add(p.id)

        for i, a in enumerate(persons):
            for b in persons[i + 1:]:
                dist = math.hypot(a.cx - b.cx, a.cy - b.cy)
                close = dist < 1.2 * max(a.h, b.h)
                if not close:
                    continue
                speed = max(self._wrist_speed(a.id), self._wrist_speed(b.id))
                if speed > 2.5:
                    flags.setdefault("Possible struggle", set()).update({a.id, b.id})
                for x, y in ((a, b), (b, a)):
                    neck = y.neck()
                    if neck is None:
                        continue
                    for wi in (L_WR, R_WR):
                        wpt = x.pt(wi)
                        if wpt is None:
                            continue
                        if np.linalg.norm(wpt - neck) < 0.15 * y.h:
                            flags.setdefault("Possible neck-grab", set()).update(
                                {x.id, y.id}
                            )
        return flags


# ============================================================
# ESP32 NODE (sensor + alarm) over Wi-Fi
# ============================================================

ESP_PORT = env_int("ESP_PORT", 5002)
ESP_ALERT_SECONDS = env_float("ESP_ALERT_SECONDS", 20)

esp = {
    "armed": True, "last_seen": 0.0, "pir": 0, "ldr": 0,
    "alert": "", "alert_ts": 0.0, "ip": "",
    "sos_seq": None,
}
esp_lock = threading.Lock()
button_sos_event = threading.Event()


def esp_set_alert(text):
    with esp_lock:
        esp["alert"] = text
        esp["alert_ts"] = time.time()


def esp_armed():
    # if ESP is online and its switch is OFF -> pause detection
    with esp_lock:
        online = time.time() - esp["last_seen"] < 6
        return not (online and not esp["armed"])


class EspHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            data = {}
        first = False
        with esp_lock:
            now = time.time()
            first = now - esp["last_seen"] > 6
            if data:
                esp["last_seen"] = now
                esp["armed"] = bool(data.get("armed", 1))
                esp["pir"] = int(data.get("pir", 0))
                esp["ldr"] = int(data.get("ldr", 0))
                esp["ip"] = self.client_address[0]

                # The ESP32 increments sos_seq whenever its SOS button is pressed.
                # Ignore the first packet (and ESP32 reboot/reset) to avoid a
                # false SOS; trigger recording only when a later value increases.
                try:
                    incoming_seq = int(data.get("sos_seq", 0))
                except (TypeError, ValueError):
                    incoming_seq = 0
                previous_seq = esp["sos_seq"]
                if previous_seq is None:
                    esp["sos_seq"] = incoming_seq
                elif incoming_seq > previous_seq:
                    esp["sos_seq"] = incoming_seq
                    button_sos_event.set()
                elif incoming_seq < previous_seq:
                    # ESP32 rebooted and its counter reset.
                    esp["sos_seq"] = incoming_seq
            if now - esp["alert_ts"] < ESP_ALERT_SECONDS:
                reply = "ALERT|" + esp["alert"]
            else:
                reply = "OK"
        if first and data:
            print(f"[ESP] Node connected from {self.client_address[0]}")
        body = reply.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.do_POST()


def start_esp_server():
    try:
        server = ThreadingHTTPServer(("0.0.0.0", ESP_PORT), EspHandler)
    except Exception as exc:
        print(f"[ESP] Server could not start on port {ESP_PORT}: {exc}")
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[ESP] Listening on port {ESP_PORT} (POST /esp)")


# ============================================================
# CROSS-CAMERA HANDOFF (anonymous clothing-colour matching)
# ============================================================


def person_feature(frame, p):
    # colour signature of upper body + lower body. No face recognition.
    fh, fw = frame.shape[:2]
    x1, y1, x2, y2 = [int(v) for v in p.box]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(fw, x2), min(fh, y2)
    if (y2 - y1) < 50 or (x2 - x1) < 20:
        return None
    crop = cv2.resize(frame[y1:y2, x1:x2], (48, 96))
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    feat = {}
    for name, (r0, r1) in (("up", (14, 50)), ("lo", (52, 88))):
        reg = hsv[r0:r1, 10:38]
        hs = cv2.calcHist([reg], [0, 1], None, [12, 6], [0, 180, 0, 256])
        v = cv2.calcHist([reg], [2], None, [4], [0, 256])
        feat[name + "_hs"] = cv2.normalize(
            hs, None, 1.0, 0.0, cv2.NORM_L1
        ).reshape(-1, 1).astype(np.float32)
        feat[name + "_v"] = cv2.normalize(
            v, None, 1.0, 0.0, cv2.NORM_L1
        ).reshape(-1, 1).astype(np.float32)
    return feat


def blend(old, new, alpha=0.2):
    if old is None:
        return new
    return {k: (1 - alpha) * old[k] + alpha * new[k] for k in new}


def feature_similarity(a, b):
    def s(k):
        return 1.0 - cv2.compareHist(a[k], b[k], cv2.HISTCMP_BHATTACHARYYA)

    return (
        0.4 * s("up_hs") + 0.4 * s("lo_hs") + 0.1 * s("up_v") + 0.1 * s("lo_v")
    )


class Target:
    def __init__(self, inc_data, person_id, feat):
        self.id = uuid.uuid4().hex[:6]
        self.incident_id = inc_data["id"]
        self.incident_type = inc_data["type"]
        self.camera = inc_data["camera"]
        self.time_str = inc_data["time_str"]
        self.person_id = person_id
        self.feat = feat
        self.expires = time.time() + TARGET_TTL_SECONDS
        self.last_alert = {}


def _send_match(target, cam_name, annotated, sim):
    now = datetime.now()
    path = os.path.join(
        EVIDENCE_DIR,
        f"{now.strftime('%Y%m%d_%H%M%S')}_MATCH_{cam_name}_{target.incident_id}.jpg",
    )
    cv2.imwrite(path, annotated)
    send_match_email(target, cam_name, now.strftime("%Y-%m-%d %H:%M:%S"), sim, path)


def check_handoff(cam, frame, targets, now):
    targets[:] = [t for t in targets if now < t.expires]
    active = [t for t in targets if t.camera != cam.name]
    cam.labels = {}
    if not active or not cam.persons:
        cam.match_counts = {}
        return
    new_counts = {}
    for p in cam.persons:
        feat = person_feature(frame, p)
        if feat is None:
            continue
        best_sim, best_t = 0.0, None
        for t in active:
            sim = feature_similarity(feat, t.feat)
            if sim > best_sim:
                best_sim, best_t = sim, t
        matched = best_t is not None and best_sim >= HANDOFF_THRESHOLD
        cam.labels[p.id] = (best_sim, matched)
        if not matched:
            continue
        key = (best_t.id, p.id)
        count = cam.match_counts.get(key, 0) + 1
        new_counts[key] = count
        if (
            count >= HANDOFF_CONFIRM_FRAMES
            and now - best_t.last_alert.get(cam.name, 0) >= HANDOFF_COOLDOWN_SECONDS
        ):
            best_t.last_alert[cam.name] = now
            annotated = frame.copy()
            draw_persons(annotated, [p], {p.id: (best_sim, True)})
            put_banner(
                annotated,
                f"SAME PERSON as {best_t.camera} incident ({best_sim * 100:.0f}%)",
                (0, 0, 200),
            )
            print(
                f"[HANDOFF] Target from {best_t.camera} seen on {cam.name} "
                f"({best_sim * 100:.0f}%)"
            )
            esp_set_alert(f"SAME PERSON|{cam.name}")
            threading.Thread(
                target=_send_match,
                args=(best_t, cam.name, annotated, best_sim),
                daemon=True,
            ).start()
    cam.match_counts = new_counts


# ============================================================
# INCIDENT RECORDING
# ============================================================


class Camera:
    def __init__(self, name, source):
        self.name = name
        self.source = CameraSource(name, source)
        self.analyzer = Analyzer()
        self.model = None
        self.buffer = deque()  # (ts, frame)
        self.last_ts = 0.0
        self.frame_count = 0
        self.pending = {}  # type -> [first_seen, last_seen, ids]
        self.last_incident = 0.0
        self.priority_until = 0.0
        self.priority_reason = ""
        self.persons = []
        self.track_feats = {}
        self.match_counts = {}
        self.labels = {}
        self.status_text = ""
        self.last_pub = 0.0


class Incident:
    def __init__(self, cam, etype, trigger_frame, pre_frames):
        now = datetime.now()
        self.data = {
            "id": uuid.uuid4().hex[:8],
            "type": etype,
            "camera": cam.name,
            "time_str": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.stamp = now.strftime("%Y%m%d_%H%M%S")
        self.cam = cam
        self.trigger_frame = trigger_frame
        self.frames = list(pre_frames)
        self.end_time = time.time() + POST_EVENT_SECONDS
        self.start_time = time.time()
        self.done = False

    def finalize(self):
        base = os.path.join(
            EVIDENCE_DIR, f"{self.stamp}_{self.cam.name}_{self.data['id']}"
        )
        jpg_path = base + ".jpg"
        mp4_path = base + ".mp4"
        try:
            cv2.imwrite(jpg_path, self.trigger_frame)
            if self.frames:
                h, w = self.frames[0][1].shape[:2]
                span = max(0.1, self.frames[-1][0] - self.frames[0][0])
                fps = min(30.0, max(3.0, len(self.frames) / span))
                writer = cv2.VideoWriter(
                    mp4_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h)
                )
                for _, fr in self.frames:
                    if fr.shape[:2] != (h, w):
                        fr = cv2.resize(fr, (w, h))
                    writer.write(fr)
                writer.release()
            print(f"[EVIDENCE] Saved {jpg_path} / {mp4_path}")
        except Exception as exc:
            print(f"[EVIDENCE] Save failed: {exc}")
        dashboard.attach_media(self.data["id"], jpg_path, mp4_path)
        send_evidence_email(self.data, jpg_path, mp4_path)


# ============================================================
# DRAWING
# ============================================================


def draw_persons(frame, persons, labels=None):
    labels = labels or {}
    for p in persons:
        x1, y1, x2, y2 = [int(v) for v in p.box]
        sim, matched = labels.get(p.id, (None, False))
        color = (0, 0, 255) if matched else (0, 200, 0)
        text = f"ID {p.id}"
        if sim is not None:
            text += f" sim {sim * 100:.0f}%"
        if matched:
            text = "TARGET " + text
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 3 if matched else 2)
        cv2.putText(
            frame, text, (x1, max(15, y1 - 6)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1,
        )
        for a, b in SKELETON:
            if p.kc[a] >= KP_OK and p.kc[b] >= KP_OK:
                cv2.line(
                    frame,
                    tuple(int(v) for v in p.kp[a]),
                    tuple(int(v) for v in p.kp[b]),
                    (255, 180, 0), 2,
                )
        for i in range(len(p.kp)):
            if p.kc[i] >= KP_OK:
                cv2.circle(
                    frame, tuple(int(v) for v in p.kp[i]), 3, (0, 0, 255), -1
                )


def put_banner(frame, text, color, row=0):
    cv2.rectangle(frame, (0, row * 28), (frame.shape[1], row * 28 + 26), color, -1)
    cv2.putText(
        frame, text, (8, row * 28 + 19),
        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2,
    )


# ============================================================
# MAIN
# ============================================================


def extract_persons(result):
    persons = []
    if result.boxes is None or result.keypoints is None:
        return persons
    boxes = result.boxes.xyxy.cpu().numpy()
    ids = (
        result.boxes.id.cpu().numpy().astype(int)
        if result.boxes.id is not None
        else np.arange(len(boxes))
    )
    kps = result.keypoints.xy.cpu().numpy()
    if result.keypoints.conf is not None:
        kcs = result.keypoints.conf.cpu().numpy()
    else:
        kcs = np.ones(kps.shape[:2])
    for i in range(len(boxes)):
        persons.append(Person(int(ids[i]), boxes[i], kps[i], kcs[i]))
    return persons


def main():
    print(f"[BOOT] Loading model: {MODEL_NAME}")

    cams = {}
    for name, src in CAMERAS.items():
        print(f"[CAMERA] Starting {name} -> {src}")
        cams[name] = Camera(name, src)
        # separate model per camera: keeps tracker IDs from mixing
        cams[name].model = YOLO(MODEL_NAME)
        cams[name].source.start()
    print("[BOOT] Model loaded")

    print("\n[CONFIG] Cameras:")
    for name, src in CAMERAS.items():
        print(f"  {name} -> {src}")
    print(f"[CONFIG] Handoff graph: {GRAPH}")
    print(f"[CONFIG] Alert all cameras: {ALERT_ALL_CAMERAS}")
    print(f"[CONFIG] Email: {'ON' if email_configured() else 'OFF'}")
    start_esp_server()

    def _dashboard_state():
        with esp_lock:
            esp_info = {
                "online": time.time() - esp["last_seen"] < 6,
                "armed": esp["armed"], "pir": esp["pir"], "ldr": esp["ldr"], "ip": esp["ip"],
            }
        return {
            "esp": esp_info,
            "cameras": [
                {"name": c.name, "online": c.source.status() in ("LIVE", "DELAYED"),
                 "status": c.source.status(), "persons": len(c.persons)}
                for c in cams.values()
            ],
        }

    dashboard.configure(state_fn=_dashboard_state, sos_fn=button_sos_event.set)
    dashboard.start()
    print("[KEYS] E = manual SOS test, Q = quit\n")

    incidents = []
    targets = []  # handoff targets (Target objects)
    manual_sos = False

    try:
        while True:
            now = time.time()

            if button_sos_event.is_set():
                manual_sos = True
                button_sos_event.clear()
                print("[ESP32 BUTTON] SOS pressed — starting evidence recording")

            for cam in cams.values():
                frame = cam.source.read()
                ts = cam.source.timestamp
                status = cam.source.status()

                if frame is None:
                    blank = np.zeros((360, 640, 3), dtype=np.uint8)
                    put_banner(blank, f"{cam.name}: waiting for camera...", (60, 60, 60))
                    cv2.imshow(cam.name, blank)
                    continue

                new_frame = ts != cam.last_ts
                cam.last_ts = ts
                display = frame.copy()

                if new_frame:
                    cam.buffer.append((now, frame))
                    while cam.buffer and now - cam.buffer[0][0] > PRE_EVENT_SECONDS:
                        cam.buffer.popleft()
                    cam.frame_count += 1

                    for inc in incidents:
                        if inc.cam is cam and not inc.done:
                            inc.frames.append((now, frame))

                tracking_active = any(t.camera != cam.name for t in targets)
                priority = now < cam.priority_until or tracking_active
                run_ai = new_frame and (
                    priority or cam.frame_count % PROCESS_EVERY_N == 0
                )

                if run_ai:
                    results = cam.model.track(
                        frame,
                        persist=True,
                        tracker="bytetrack.yaml",
                        conf=CONF,
                        imgsz=IMG_SIZE,
                        verbose=False,
                    )
                    cam.persons = extract_persons(results[0])
                    for p in cam.persons:
                        if p.h > 1.1 * p.w:  # update colour profile only when upright
                            f = person_feature(frame, p)
                            if f is not None:
                                cam.track_feats[p.id] = blend(
                                    cam.track_feats.get(p.id), f
                                )
                    flags = cam.analyzer.analyze(cam.persons, now)
                    if manual_sos:
                        flags["SOS gesture (manual)"] = {p.id for p in cam.persons}

                    for etype in flags:
                        entry = cam.pending.setdefault(etype, [now, now, set()])
                        entry[1] = now
                        entry[2] = set(flags[etype])
                    for etype in list(cam.pending):
                        first, last, ids = cam.pending[etype]
                        if etype not in flags and now - last > 0.3:
                            del cam.pending[etype]
                            continue
                        is_manual_sos = etype == "SOS gesture (manual)"
                        if (
                            etype in flags
                            and now - first >= (0 if is_manual_sos else EVENT_CONFIRM_SECONDS)
                            and (
                                is_manual_sos
                                or now - cam.last_incident >= EVENT_COOLDOWN_SECONDS
                            )
                            and (is_manual_sos or esp_armed())
                        ):
                            cam.last_incident = now
                            del cam.pending[etype]
                            annotated = frame.copy()
                            draw_persons(annotated, cam.persons)
                            put_banner(annotated, f"INCIDENT: {etype}", (0, 0, 200))
                            inc = Incident(cam, etype, annotated, list(cam.buffer))
                            incidents.append(inc)
                            dashboard.register_incident(inc.data)
                            for pid in ids:
                                feat = cam.track_feats.get(pid)
                                if feat is None:
                                    pp = next(
                                        (q for q in cam.persons if q.id == pid), None
                                    )
                                    feat = (
                                        person_feature(frame, pp)
                                        if pp is not None
                                        else None
                                    )
                                if feat is not None:
                                    targets.append(Target(inc.data, pid, feat))
                            if ids:
                                print(
                                    f"[HANDOFF] Searching {len(ids)} target(s) "
                                    f"from {cam.name} on other cameras"
                                )
                            print(f"[INCIDENT] {etype} on {cam.name} ({inc.data['id']})")
                            esp_set_alert(f"{etype}|{cam.name}")
                            threading.Thread(
                                target=send_text_alert, args=(inc.data,), daemon=True
                            ).start()

                            if ALERT_ALL_CAMERAS:
                                target_cams = GRAPH.get(cam.name) or (set(cams) - {cam.name})
                                for tname in target_cams:
                                    if tname in cams:
                                        cams[tname].priority_until = (
                                            now + PRIORITY_DURATION_SECONDS
                                        )
                                        cams[tname].priority_reason = (
                                            f"search from {cam.name}"
                                        )
                    check_handoff(cam, frame, targets, now)
                    manual_sos = False

                draw_persons(display, cam.persons, cam.labels)
                put_banner(
                    display,
                    f"{cam.name} [{status}] persons={len(cam.persons)}",
                    (40, 40, 40),
                )
                if priority:
                    put_banner(
                        display,
                        f"PRIORITY SEARCH ({cam.priority_reason})",
                        (0, 120, 220), row=1,
                    )
                if not esp_armed():
                    put_banner(display, "DISARMED (ESP switch OFF)", (90, 90, 90), row=3)
                if any(i.cam is cam and not i.done for i in incidents):
                    put_banner(display, "RECORDING EVIDENCE", (0, 0, 200), row=2)

                cv2.imshow(cam.name, display)

                if now - cam.last_pub > 0.12:  # ~8 fps preview for the Control Room
                    cam.last_pub = now
                    try:
                        small = cv2.resize(display, (480, int(display.shape[0] * 480 / display.shape[1])))
                        ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 60])
                        if ok:
                            dashboard.publish_frame(cam.name, buf.tobytes())
                    except Exception:
                        pass

            for inc in incidents:
                if not inc.done and time.time() >= inc.end_time:
                    inc.done = True
                    threading.Thread(target=inc.finalize, daemon=True).start()
            incidents = [i for i in incidents if not i.done]

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), ord("Q")):
                break
            if key in (ord("e"), ord("E")):
                manual_sos = True
                print("[KEY] Manual SOS test")

    except KeyboardInterrupt:
        pass
    finally:
        print("[CLEANUP] Closing cameras...")
        for cam in cams.values():
            cam.source.stop()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()