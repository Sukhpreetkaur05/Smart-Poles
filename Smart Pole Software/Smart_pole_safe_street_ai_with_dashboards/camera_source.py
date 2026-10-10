import re
import threading
import time
import urllib.request
from typing import List, Optional, Union

import cv2
import numpy as np


class CameraSource:
    """
    Camera-independent frame source.

    Supported:
    - Laptop webcam: 0, 1, 2...
    - HTTP/MJPEG phone camera (DroidCam, IP Webcam, ESP32-CAM stream)
    - RTSP camera
    - Video file

    Connection strategy for network cameras:
      1. OpenCV (FFMPEG, then ANY) on several URL variants
      2. Pure-Python MJPEG reader (no OpenCV decoding of the stream)
    A source counts as connected only after the first real frame arrives.
    """

    HTTP_FALLBACK_PATHS = [
        "/video",
        "/mjpegfeed",
        "/mjpegfeed?640x480",
        "/videofeed",
    ]

    def __init__(self, camera_id: str, source: Union[str, int]):
        self.camera_id = camera_id
        self.source = self._normalize(source)

        self.cap: Optional[cv2.VideoCapture] = None
        self.stream = None  # urllib response for MJPEG mode
        self.mode = ""  # "opencv" | "mjpeg"
        self.active_url = None

        self.frame = None
        self.timestamp = 0.0

        self.running = False
        self.lock = threading.Lock()
        self.thread = None

        self.last_error = ""
        self.connected = False
        self.last_connect_attempt = 0.0

    # ========================================================
    # HELPERS
    # ========================================================

    @staticmethod
    def _normalize(source):
        if isinstance(source, int):
            return source
        s = str(source).strip()
        if s.isdigit():
            return int(s)
        # fix accidental double slash in path: http://ip:port//video
        s = re.sub(r"(?<!:)//+", "/", s.replace("://", ":@@"))
        s = s.replace(":@@", "://")
        return s

    def _log(self, msg):
        print(f"[CAMERA:{self.camera_id}] {msg}")

    def _candidates(self) -> List[str]:
        """Return URL variants to try, last working one first."""
        urls: List[str] = []
        if self.active_url:
            urls.append(self.active_url)
        urls.append(self.source)

        m = re.match(r"^(https?://[^/]+)(/.*)?$", self.source)
        if m:
            base = m.group(1)
            for p in self.HTTP_FALLBACK_PATHS:
                urls.append(base + p)

        seen, out = set(), []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _set_frame(self, frame):
        with self.lock:
            self.frame = frame
            self.timestamp = time.time()

    # ========================================================
    # OPENCV CONNECT
    # ========================================================

    def _open_webcam(self) -> bool:
        for backend in (cv2.CAP_DSHOW, cv2.CAP_ANY):
            cap = cv2.VideoCapture(self.source, backend)
            if cap.isOpened():
                ok, frame = cap.read()
                if ok and frame is not None:
                    self.cap = cap
                    self.mode = "opencv"
                    self._set_frame(frame)
                    return True
            cap.release()
        self.last_error = "Webcam could not be opened"
        return False

    def _try_opencv(self, url: str) -> bool:
        for backend, name in ((cv2.CAP_FFMPEG, "FFMPEG"), (cv2.CAP_ANY, "ANY")):
            cap = None
            try:
                cap = cv2.VideoCapture(url, backend)
                for prop in (
                    "CAP_PROP_OPEN_TIMEOUT_MSEC",
                    "CAP_PROP_READ_TIMEOUT_MSEC",
                ):
                    try:
                        cap.set(getattr(cv2, prop), 5000)
                    except Exception:
                        pass
                try:
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                except Exception:
                    pass

                if not cap.isOpened():
                    cap.release()
                    continue

                ok, frame = cap.read()
                if ok and frame is not None:
                    self.cap = cap
                    self.mode = "opencv"
                    self.active_url = url
                    self._set_frame(frame)
                    self._log(f"CONNECTED (OpenCV/{name}) -> {url}")
                    return True
                cap.release()
            except Exception as exc:
                self.last_error = str(exc)
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
        return False

    # ========================================================
    # PURE MJPEG CONNECT
    # ========================================================

    @staticmethod
    def _extract_jpeg(buf: bytearray):
        """Pop one complete JPEG from buffer, return decoded frame or None."""
        start = buf.find(b"\xff\xd8")
        if start == -1:
            if len(buf) > 2_000_000:
                buf.clear()
            return None
        end = buf.find(b"\xff\xd9", start + 2)
        if end == -1:
            if start > 0:
                del buf[:start]
            if len(buf) > 4_000_000:
                buf.clear()
            return None
        jpg = bytes(buf[start:end + 2])
        del buf[:end + 2]
        arr = np.frombuffer(jpg, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)

    def _try_mjpeg(self, url: str) -> bool:
        if not url.lower().startswith("http"):
            return False
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"}
            )
            resp = urllib.request.urlopen(req, timeout=5)
        except Exception as exc:
            self.last_error = f"MJPEG open failed: {exc}"
            return False

        buf = bytearray()
        deadline = time.time() + 6
        try:
            while time.time() < deadline:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                frame = self._extract_jpeg(buf)
                if frame is not None:
                    self.stream = resp
                    self._pending = buf
                    self.mode = "mjpeg"
                    self.active_url = url
                    self._set_frame(frame)
                    self._log(f"CONNECTED (MJPEG reader) -> {url}")
                    return True
        except Exception as exc:
            self.last_error = f"MJPEG read failed: {exc}"

        try:
            resp.close()
        except Exception:
            pass
        return False

    # ========================================================
    # CONNECT / CLOSE
    # ========================================================

    def _connect(self) -> bool:
        self.last_connect_attempt = time.time()
        self._log(f"Connecting -> {self.source}")

        if isinstance(self.source, int):
            ok = self._open_webcam()
        else:
            ok = False
            urls = self._candidates()
            for url in urls:
                if not self.running:
                    return False
                if self._try_opencv(url):
                    ok = True
                    break
            if not ok:
                for url in urls:
                    if not self.running:
                        return False
                    if self._try_mjpeg(url):
                        ok = True
                        break

        self.connected = ok
        if not ok:
            if not self.last_error:
                self.last_error = "Could not open source"
            self._log(f"FAILED: {self.last_error}")
        return ok

    def _close(self):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        if self.stream is not None:
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        self.connected = False

    # ========================================================
    # FRAME PUMPS
    # ========================================================

    def _pump_opencv(self):
        fails = 0
        while self.running:
            try:
                ok, frame = self.cap.read()
            except Exception as exc:
                self.last_error = str(exc)
                return
            if ok and frame is not None:
                fails = 0
                self._set_frame(frame)
                continue
            fails += 1
            if fails >= 3:
                self.last_error = "Frame read failed"
                return
            time.sleep(0.1)

    def _pump_mjpeg(self):
        buf = getattr(self, "_pending", bytearray())
        while self.running:
            try:
                chunk = self.stream.read(4096)
            except Exception as exc:
                self.last_error = f"MJPEG stream error: {exc}"
                return
            if not chunk:
                self.last_error = "MJPEG stream ended"
                return
            buf.extend(chunk)
            # keep only the newest complete frame to avoid lag
            latest = None
            while True:
                frame = self._extract_jpeg(buf)
                if frame is None:
                    break
                latest = frame
            if latest is not None:
                self._set_frame(latest)

    # ========================================================
    # BACKGROUND READER
    # ========================================================

    def _reader(self):
        retry_delay = 1.0
        while self.running:
            self._close()
            self.last_error = ""

            if not self._connect():
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, 5.0)
                continue

            retry_delay = 1.0

            if self.mode == "opencv":
                self._pump_opencv()
            else:
                self._pump_mjpeg()

            if self.running:
                self._log(
                    f"Stream lost ({self.last_error}) - reconnecting..."
                )
            self._close()
            time.sleep(0.5)

        self._close()

    # ========================================================
    # PUBLIC API
    # ========================================================

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(
            target=self._reader,
            name=f"camera-{self.camera_id}",
            daemon=True,
        )
        self.thread.start()

    def read(self):
        with self.lock:
            if self.frame is None:
                return None
            return self.frame.copy()

    def is_connected(self):
        return self.connected

    def age_seconds(self):
        with self.lock:
            if self.timestamp <= 0:
                return float("inf")
            return time.time() - self.timestamp

    def status(self):
        age = self.age_seconds()
        if self.connected:
            if age < 2:
                return "LIVE"
            elif age < 5:
                return "DELAYED"
            return "STALE"
        return "OFFLINE"

    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3.0)
        self._close()
        self._log("STOPPED")