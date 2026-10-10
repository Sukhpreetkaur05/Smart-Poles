"""
Suraksha dashboards for Safe Street AI.

Serves two web dashboards from one small stdlib HTTP server (no extra pip installs):

    http://localhost:8000/          landing page
    http://localhost:8000/citizen   Suraksha SOS Dashboard  (citizen / mobile side)
    http://localhost:8000/control   Control Room Dashboard  (operator side)

It is started automatically by app.py, and can also run on its own:

    python dashboard_server.py      (history from evidence/ + citizen SOS only, no AI cameras)
"""
from __future__ import annotations

import csv
import io
import json
import mimetypes
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE, "dashboard")
EVIDENCE_DIR = os.path.join(BASE, "evidence")
EVENTS_DIR = os.path.join(BASE, "events")
DATA_DIR = os.path.join(BASE, "dashboard_data")
DB_FILE = os.path.join(DATA_DIR, "incidents.json")
PORT = int(os.getenv("DASHBOARD_PORT", "8000") or 8000)
LDR_DARK_VALUE = int(os.getenv("DASHBOARD_LDR_DARK_VALUE", "1") or 1)
MEDIA_ROOTS = ("evidence", "events")

mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("audio/webm", ".weba")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(EVIDENCE_DIR, exist_ok=True)

_lock = threading.RLock()
_incidents: dict[str, dict] = {}
_frames: dict[str, tuple[float, bytes]] = {}  # camera -> (time, jpeg)
_state_fn = None
_sos_fn = None
_alias: dict[str, str] = {}  # camera-incident id -> citizen SOS id it belongs to

SECTORS = ["Sector 4", "Market", "Park", "Main Gate", "Bus Stand", "Station Road"]


# ------------------------------------------------------------------ helpers
def pole_for(camera: str) -> str:
    nums = re.findall(r"\d+", camera or "")
    n = int(nums[0]) if nums else 1
    return f"P-{max(n, 1):02d}"


def sector_for(pole: str) -> str:
    try:
        return SECTORS[(int(pole.split("-")[1]) - 1) % len(SECTORS)]
    except Exception:
        return SECTORS[0]


def _now() -> float:
    return time.time()


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _save():
    with _lock:
        tmp = DB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(_incidents.values()), f, indent=1)
        os.replace(tmp, DB_FILE)


def _next_number() -> int:
    return max([i.get("no", 0) for i in _incidents.values()] + [2000]) + 1


def _new_incident(**kw) -> dict:
    ts = kw.pop("ts", _now())
    camera = kw.get("camera", "")
    pole = kw.pop("pole", None) or pole_for(camera)
    inc = {
        "id": kw.pop("id", uuid.uuid4().hex[:8]),
        "no": _next_number(),
        "type": "Incident",
        "source": "AI camera",
        "camera": camera,
        "pole": pole,
        "sector": sector_for(pole),
        "ts": ts,
        "time_str": _fmt(ts),
        "status": "new",  # new | responding | resolved | cancelled
        "ack_ts": None,
        "resolved_ts": None,
        "resolved_by": "",
        "note": "",
        "photo": "",
        "video": "",
        "audio": "",
        "lat": None,
        "lng": None,
        "text": "",
        "lang": "",
    }
    inc.update(kw)
    return inc


# ------------------------------------------------------------ public API (app.py)
def configure(state_fn=None, sos_fn=None):
    """state_fn() -> {'cameras': [...], 'esp': {...}} ; sos_fn() triggers evidence recording."""
    global _state_fn, _sos_fn
    _state_fn, _sos_fn = state_fn, sos_fn


def register_incident(data: dict) -> dict:
    """Called by app.py the moment the AI / ESP button raises an incident."""
    etype = data.get("type", "Incident")
    source = "ESP32 SOS button" if "manual" in etype.lower() else "AI camera"
    with _lock:
        if "manual" in etype.lower():  # a citizen pressed SOS moments ago -> same incident
            for c in sorted(_incidents.values(), key=lambda i: i["ts"], reverse=True):
                if (c["source"] == "Citizen app" and _now() - c["ts"] < 30
                        and not c["photo"] and c["id"] not in _alias.values()):
                    _alias[data.get("id", "")] = c["id"]
                    c["camera"] = data.get("camera", c["camera"])
                    c["pole"] = pole_for(c["camera"])
                    c["sector"] = sector_for(c["pole"])
                    _save()
                    return c
        inc = _new_incident(
            id=data.get("id") or uuid.uuid4().hex[:8],
            type=etype,
            source=source,
            camera=data.get("camera", ""),
        )
        _incidents[inc["id"]] = inc
        _save()
    return inc


def attach_media(incident_id: str, jpg_path: str = "", mp4_path: str = ""):
    """Called by app.py when evidence files are written to disk."""
    with _lock:
        inc = _incidents.get(_alias.get(incident_id, incident_id))
        if not inc:
            return
        if jpg_path and os.path.exists(jpg_path):
            inc["photo"] = _rel(jpg_path)
        if mp4_path and os.path.exists(mp4_path):
            inc["video"] = _rel(mp4_path)
        _save()


def publish_frame(camera: str, jpeg: bytes):
    _frames[camera] = (_now(), jpeg)


def _rel(path: str) -> str:
    return os.path.relpath(os.path.abspath(path), BASE).replace("\\", "/")


# --------------------------------------------------------------------- storage
EVID_RE = re.compile(
    r"^(\d{8})_(\d{6})_(?:(MATCH)_)?([A-Za-z0-9]+?)_([0-9a-f]{6,8})\.(jpg|mp4)$"
)


def _load():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, encoding="utf-8") as f:
                for inc in json.load(f):
                    _incidents[inc["id"]] = inc
        except Exception as exc:
            print(f"[DASH] Could not read {DB_FILE}: {exc}")
    _import_archive()


def _import_archive():
    """Make existing evidence/ and events/ show up in History & Reports."""
    known = {i["photo"] for i in _incidents.values()} | {i["video"] for i in _incidents.values()}
    groups: dict[str, dict] = {}
    if os.path.isdir(EVIDENCE_DIR):
        for name in sorted(os.listdir(EVIDENCE_DIR)):
            m = EVID_RE.match(name)
            if not m:
                continue
            d, t, match, cam, uid, ext = m.groups()
            g = groups.setdefault(
                f"{d}{t}{cam}{uid}",
                {"d": d, "t": t, "match": bool(match), "cam": cam, "uid": uid},
            )
            g["jpg" if ext == "jpg" else "mp4"] = f"evidence/{name}"
    for g in groups.values():
        if (g.get("jpg") in known) or (g.get("mp4") in known):
            continue
        ts = datetime.strptime(g["d"] + g["t"], "%Y%m%d%H%M%S").timestamp()
        inc = _new_incident(
            id=g["uid"],
            type="Handoff match (same person)" if g["match"] else "AI detected incident",
            source="Evidence archive",
            camera=g["cam"],
            ts=ts,
            status="resolved",
            resolved_by="archive",
            resolved_ts=ts,
            note="Imported from evidence folder",
            photo=g.get("jpg", ""),
            video=g.get("mp4", ""),
        )
        _incidents[inc["id"]] = inc
    if os.path.isdir(EVENTS_DIR):
        for d in sorted(os.listdir(EVENTS_DIR)):
            jf = os.path.join(EVENTS_DIR, d, "event.json")
            if not os.path.exists(jf):
                continue
            photo, video = f"events/{d}/evidence.jpg", f"events/{d}/evidence.mp4"
            if photo in known or video in known:
                continue
            try:
                ev = json.load(open(jf, encoding="utf-8"))
                ts = datetime.fromisoformat(ev["timestamp"]).timestamp()
            except Exception:
                continue
            inc = _new_incident(
                id=d[-8:].replace("_", "") or uuid.uuid4().hex[:8],
                type=ev.get("event_type", "SOS").replace("_", " ").title(),
                source="Evidence archive",
                camera="CAM01",  # event.json uses a device label (e.g. LAPTOP_CAM_0)
                ts=ts,
                status="resolved",
                resolved_by="archive",
                resolved_ts=ts,
                note=ev.get("reason", "Imported from events folder"),
                photo=photo if os.path.exists(os.path.join(BASE, photo)) else "",
                video=video if os.path.exists(os.path.join(BASE, video)) else "",
            )
            _incidents[inc["id"]] = inc
    _save()


# ----------------------------------------------------------------------- state
def _public(inc: dict) -> dict:
    out = dict(inc)
    for k in ("photo", "video", "audio"):
        out[k + "_url"] = f"/media/{inc[k]}" if inc.get(k) else ""
    return out


def build_state() -> dict:
    now = _now()
    with _lock:
        items = sorted(_incidents.values(), key=lambda i: i["ts"], reverse=True)
        today = datetime.now().date()
        status = {"new": 0, "responding": 0, "resolved": 0, "cancelled": 0}
        for i in items:
            status[i["status"]] = status.get(i["status"], 0) + 1
        resolved_today = sum(
            1 for i in items
            if i["status"] == "resolved" and datetime.fromtimestamp(i["resolved_ts"] or i["ts"]).date() == today
        )
        resp = [(i["ack_ts"] - i["ts"]) / 60 for i in items if i.get("ack_ts")]
        avg_resp = round(sum(resp) / len(resp), 1) if resp else 0.0
        total = max(1, len(items))
        active = status["new"] + status["responding"]

        days = [(today - timedelta(days=6 - k)) for k in range(7)]
        weekly_alerts = [0] * 7
        weekly_resolved = [0] * 7
        weekly_resp = [[] for _ in range(7)]
        for i in items:
            d = datetime.fromtimestamp(i["ts"]).date()
            if d in days:
                k = days.index(d)
                weekly_alerts[k] += 1
                if i["status"] == "resolved":
                    weekly_resolved[k] += 1
                if i.get("ack_ts"):
                    weekly_resp[k].append((i["ack_ts"] - i["ts"]) / 60)
        hourly = [0] * 6
        for i in items:
            hourly[datetime.fromtimestamp(i["ts"]).hour // 4] += 1
        by_type = {"SOS": 0, "Motion / AI": 0, "Other": 0}
        for i in items:
            t = (i["type"] + " " + i["source"]).lower()
            if "sos" in t or "citizen" in t:
                by_type["SOS"] += 1
            elif "match" in t or "motion" in t or "ai" in t or "fall" in t or "violence" in t:
                by_type["Motion / AI"] += 1
            else:
                by_type["Other"] += 1
        langs: dict[str, int] = {}
        for i in items:
            if i.get("lang"):
                langs[i["lang"]] = langs.get(i["lang"], 0) + 1

    ext = _state_fn() if _state_fn else {"cameras": [], "esp": {}}
    esp = ext.get("esp", {}) or {}
    cams = ext.get("cameras", []) or []
    poles = []
    seen = set()
    for c in cams:
        p = pole_for(c["name"])
        seen.add(p)
        poles.append({
            "pole": p, "camera": c["name"], "sector": sector_for(p),
            "online": bool(c.get("online")), "persons": c.get("persons", 0),
            "night": bool(esp.get("online") and esp.get("ldr") == LDR_DARK_VALUE),
            "motion": bool(esp.get("online") and esp.get("pir")) or c.get("persons", 0) > 0,
            "armed": bool(esp.get("armed", True)),
            "stream": f"/stream/{c['name']}",
        })
    if not poles:  # standalone mode: show poles that appear in history, offline
        for cam in sorted({i["camera"] for i in items if i["camera"]}):
            p = pole_for(cam)
            if p in seen:
                continue
            seen.add(p)
            poles.append({"pole": p, "camera": cam, "sector": sector_for(p), "online": False,
                          "persons": 0, "night": False, "motion": False, "armed": True, "stream": ""})
    return {
        "server_time": now,
        "incidents": [_public(i) for i in items[:300]],
        "stats": {
            "new": status["new"], "responding": status["responding"],
            "resolved_today": resolved_today, "active": active,
            "poles_online": sum(1 for p in poles if p["online"]), "poles_total": len(poles),
            "avg_response_min": avg_resp,
            "resolved_pct": round(100 * (status["resolved"] + status["cancelled"]) / total),
            "pct": {k: round(100 * v / total) for k, v in status.items()},
            "weekly_alerts": weekly_alerts, "weekly_resolved": weekly_resolved,
            "weekly_days": [d.strftime("%a") for d in days],
            "weekly_resp": [round(sum(x) / len(x), 1) if x else 0 for x in weekly_resp],
            "hourly": hourly, "by_type": by_type, "langs": langs,
            "translated": sum(1 for i in items if i.get("lang")),
        },
        "poles": poles,
        "esp": esp,
    }


def build_csv() -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["No", "ID", "Date", "Pole", "Sector", "Type", "Source", "Status", "Resolved by", "Note", "Photo", "Video"])
    with _lock:
        for i in sorted(_incidents.values(), key=lambda x: x["ts"], reverse=True):
            w.writerow([i["no"], i["id"], i["time_str"], i["pole"], i["sector"], i["type"], i["source"],
                        i["status"], i["resolved_by"], i["note"], i["photo"], i["video"]])
    return buf.getvalue()


# ---------------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # --- tiny response helpers
    def _send(self, code, body: bytes, ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b""

    def _jbody(self) -> dict:
        try:
            return json.loads(self._body() or b"{}")
        except Exception:
            return {}

    def _file(self, path, ctype=None):
        if not os.path.isfile(path):
            return self._send(404, b"Not found")
        ctype = ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        start, end, code = 0, size - 1, 200
        if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            elif m.group(2):
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # --- routing
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        pages = {"/": "index.html", "/citizen": "sos.html", "/control": "control.html"}
        if path in pages:
            return self._file(os.path.join(WEB_DIR, pages[path]), "text/html; charset=utf-8")
        if path == "/api/state":
            return self._json(build_state())
        if path == "/api/export.csv":
            return self._send(200, build_csv().encode("utf-8-sig"), "text/csv; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="suraksha_incidents.csv"'})
        if path.startswith("/media/"):
            rel = path[len("/media/"):]
            full = os.path.realpath(os.path.join(BASE, rel))
            # must resolve INSIDE evidence/ or events/ (blocks ../.env style tricks)
            for root in MEDIA_ROOTS:
                if full.startswith(os.path.realpath(os.path.join(BASE, root)) + os.sep):
                    return self._file(full)
            return self._send(403, b"Forbidden")
        if path.startswith("/snapshot/"):
            fr = _frames.get(path.split("/", 2)[2])
            return self._send(200, fr[1], "image/jpeg") if fr else self._send(404, b"No frame")
        if path.startswith("/stream/"):
            return self._mjpeg(path.split("/", 2)[2])
        return self._send(404, b"Not found")

    def _mjpeg(self, cam):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        last = 0.0
        try:
            while True:
                fr = _frames.get(cam)
                if fr and fr[0] != last:
                    last = fr[0]
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(fr[1])).encode() + b"\r\n\r\n" + fr[1] + b"\r\n")
                time.sleep(0.12)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        if path == "/api/sos":
            return self._citizen_sos(self._jbody())
        if path.startswith("/api/upload/"):
            return self._upload(path.rsplit("/", 1)[1])
        m = re.match(r"^/api/(ack|resolve|cancel)/([\w-]+)$", path)
        if m:
            return self._transition(m.group(1), m.group(2), self._jbody())
        return self._send(404, b"Not found")

    # --- actions
    def _citizen_sos(self, d):
        text = str(d.get("text", ""))[:500]
        with _lock:
            inc = _new_incident(
                type="Citizen SOS", source="Citizen app", camera=str(d.get("camera", "") or "APP"),
                pole=d.get("pole") or "P-01", text=text, lang=str(d.get("lang", ""))[:20],
                lat=d.get("lat"), lng=d.get("lng"),
            )
            _incidents[inc["id"]] = inc
            _save()
        if _sos_fn:  # tell the camera AI to start evidence recording
            try:
                _sos_fn()
            except Exception as exc:
                print(f"[DASH] sos callback failed: {exc}")
        print(f"[DASH] Citizen SOS received ({inc['id']})")
        self._json({"ok": True, "id": inc["id"], "no": inc["no"]})

    def _upload(self, iid):
        with _lock:
            inc = _incidents.get(iid)
        if not inc:
            return self._json({"ok": False, "error": "unknown incident"}, 404)
        body = self._body()
        if not body or len(body) > 60 * 1024 * 1024:
            return self._json({"ok": False, "error": "bad size"}, 400)
        ctype = (self.headers.get("Content-Type") or "").lower()
        kind = "video" if ctype.startswith("video") else "audio"
        ext = ".webm" if "webm" in ctype else ".mp4" if "mp4" in ctype else ".ogg" if "ogg" in ctype else ".bin"
        name = f"{datetime.now():%Y%m%d_%H%M%S}_CITIZEN_{iid}{ext}"
        with open(os.path.join(EVIDENCE_DIR, name), "wb") as f:
            f.write(body)
        with _lock:
            inc[kind] = f"evidence/{name}"
            _save()
        self._json({"ok": True, kind: inc[kind]})

    def _transition(self, action, iid, d):
        with _lock:
            inc = _incidents.get(iid)
            if not inc:
                return self._json({"ok": False, "error": "unknown incident"}, 404)
            now = _now()
            if action == "ack":
                if inc["status"] == "new":
                    inc["status"], inc["ack_ts"] = "responding", now
            elif action == "resolve":
                note = str(d.get("note", "")).strip()
                if not note:
                    return self._json({"ok": False, "error": "Resolve note is required"}, 400)
                inc["status"], inc["resolved_ts"] = "resolved", now
                inc["note"], inc["resolved_by"] = note[:500], str(d.get("by", "CR-1042"))[:40]
                inc["ack_ts"] = inc["ack_ts"] or now
            elif action == "cancel":
                inc["status"], inc["resolved_ts"] = "cancelled", now
                inc["note"] = inc["note"] or "Cancelled by citizen - problem solved"
            _save()
        self._json({"ok": True, "status": inc["status"]})


_started = False


def start(block=False):
    global _started
    if _started:
        return
    _started = True
    _load()
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except Exception as exc:
        print(f"[DASH] Could not start dashboard on port {PORT}: {exc}")
        return
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[DASH] Citizen SOS dashboard : http://localhost:{PORT}/citizen")
    print(f"[DASH] Control room dashboard: http://localhost:{PORT}/control")
    if block:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    start(block=True)
