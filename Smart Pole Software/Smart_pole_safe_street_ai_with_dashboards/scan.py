import sys
import urllib.error
import urllib.request

# Usage: python scan.py 10.207.118.76
# It tries common ports and stream paths of DroidCam / IP Webcam / similar apps.

ip = sys.argv[1] if len(sys.argv) > 1 else "10.207.118.76"

ports = [4747, 8080, 8081, 4748, 8888]
paths = [
    "/", "/video", "/video?640x480", "/mjpegfeed", "/mjpegfeed?640x480",
    "/videofeed", "/mjpeg", "/stream", "/video.mjpg", "/mjpg/video.mjpg",
    "/shot.jpg", "/live", "/cam/1/stream", "/cam.mjpg",
]

found = []
for port in ports:
    for path in paths:
        url = f"http://{ip}:{port}{path}"
        try:
            r = urllib.request.urlopen(url, timeout=3)
            ctype = r.headers.get("Content-Type")
            print("OK   ", r.status, ctype, url)
            found.append((url, ctype))
            r.close()
        except urllib.error.HTTPError as e:
            if port == ports[0]:
                print("HTTP ", e.code, url)
        except Exception:
            pass  # port closed / no response

print("\n=== RESULT ===")
if not found:
    print("Koi bhi path nahi chala. Phone app ka server/stream ON nahi hai, ya IP/port galat hai.")
else:
    for url, ctype in found:
        print(url, "->", ctype)
    print("\n'multipart/x-mixed-replace' ya 'image/jpeg' wala URL .env mein CAMERA_SOURCES mein daalo.")