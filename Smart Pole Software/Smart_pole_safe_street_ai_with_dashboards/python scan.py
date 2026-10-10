import urllib.request, urllib.error

base = "http://10.207.118.76:4747"
paths = ["/", "/video", "/video?640x480", "/mjpegfeed", "/mjpegfeed?640x480",
         "/videofeed", "/mjpeg", "/stream", "/video.mjpg", "/shot.jpg", "/live"]

for p in paths:
    try:
        r = urllib.request.urlopen(base + p, timeout=4)
        print("OK  ", r.status, r.headers.get("Content-Type"), p)
        r.close()
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, p)
    except Exception as e:
        print("ERR ", p, e)