"""
Phone camera WITHOUT installing any app.

Phone opens  https://<laptop-ip>:5000  in Chrome -> sends camera frames.
Laptop serves a local MJPEG stream at  http://127.0.0.1:5001/video
which camera_source.py / app.py can read.

Install once:   python -m pip install flask cryptography
Run:            python phone_stream.py
"""

import logging
import socket
import threading
import time

import cv2
import numpy as np
from flask import Flask, Response, request

UI_PORT = 5000       # HTTPS, phone connects here
STREAM_PORT = 5001   # plain HTTP, only for this laptop

latest = {"jpg": None, "ts": 0.0}
lock = threading.Lock()

# placeholder shown until the phone starts sending
_ph = np.zeros((480, 640, 3), dtype=np.uint8)
cv2.putText(_ph, "Waiting for phone...", (120, 250),
            cv2.FONT_HERSHEY_SIMPLEX, 1.1, (255, 255, 255), 2)
PLACEHOLDER = cv2.imencode(".jpg", _ph)[1].tobytes()

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Phone Camera</title>
<style>
body{margin:0;background:#111;color:#fff;font-family:sans-serif;text-align:center}
video{width:100%;max-height:70vh;background:#000}
#s{padding:12px;font-size:18px}
button{font-size:18px;padding:10px 18px;margin:6px}
</style></head>
<body>
<video id="v" playsinline muted autoplay></video>
<div id="s">Camera shuru ho raha hai...</div>
<button onclick="flip()">Front/Back camera</button>
<canvas id="c" style="display:none"></canvas>
<script>
let facing = "environment", stream = null, sent = 0;
const v = document.getElementById("v"), c = document.getElementById("c"),
      s = document.getElementById("s");

async function start() {
  if (stream) stream.getTracks().forEach(t => t.stop());
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: {facingMode: facing, width: {ideal: 640}, height: {ideal: 480}},
      audio: false
    });
    v.srcObject = stream;
    await v.play();
    try { await navigator.wakeLock.request("screen"); } catch (e) {}
    s.textContent = "Streaming ON - is tab ko khula rakho, screen ON rakho";
  } catch (e) {
    s.textContent = "Camera error: " + e.message + " (permission Allow karo)";
  }
}
function flip() { facing = facing === "environment" ? "user" : "environment"; start(); }

async function loop() {
  if (v.videoWidth > 0) {
    c.width = v.videoWidth; c.height = v.videoHeight;
    c.getContext("2d").drawImage(v, 0, 0);
    const blob = await new Promise(r => c.toBlob(r, "image/jpeg", 0.6));
    try {
      await fetch("/frame", {method: "POST", body: blob});
      sent++;
      s.textContent = "Streaming ON - frames sent: " + sent;
    } catch (e) { s.textContent = "Laptop se connection toot gaya..."; }
  }
  setTimeout(loop, 50);
}
start(); loop();
</script></body></html>"""

# ---------------- phone-facing HTTPS app ----------------
ui_app = Flask("ui")


@ui_app.route("/")
def index():
    return PAGE


@ui_app.route("/frame", methods=["POST"])
def frame():
    data = request.get_data()
    if data:
        with lock:
            latest["jpg"] = data
            latest["ts"] = time.time()
    return "ok"


# ---------------- local MJPEG stream ----------------
stream_app = Flask("stream")


def mjpeg():
    while True:
        with lock:
            jpg = latest["jpg"]
            fresh = (time.time() - latest["ts"]) < 3.0
        if jpg is None or not fresh:
            jpg = PLACEHOLDER
        yield (
            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(jpg)).encode()
            + b"\r\n\r\n" + jpg + b"\r\n"
        )
        time.sleep(0.04)


@stream_app.route("/video")
def video():
    return Response(mjpeg(), mimetype="multipart/x-mixed-replace; boundary=frame")


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    threading.Thread(
        target=lambda: stream_app.run(
            host="127.0.0.1", port=STREAM_PORT, threaded=True, use_reloader=False
        ),
        daemon=True,
    ).start()

    ip = lan_ip()
    print("=" * 60)
    print(f" Phone ke Chrome mein kholo:  https://{ip}:{UI_PORT}")
    print(" (Certificate warning aayegi -> Advanced -> Proceed)")
    print(f" app.py ke liye stream:       http://127.0.0.1:{STREAM_PORT}/video")
    print("=" * 60)

    ui_app.run(
        host="0.0.0.0", port=UI_PORT, ssl_context="adhoc",
        threaded=True, use_reloader=False,
    )