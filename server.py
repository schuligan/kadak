"""Kadak's local web app. Run `python3 server.py` and open http://127.0.0.1:8765.

Everything stays on this computer: the server only listens on 127.0.0.1,
refuses requests addressed to any other host name, and makes no network
calls of its own (yt-dlp downloads only the link you paste).
"""
import json
import os
import re
import threading
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import kadak

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("KADAK_DATA", ROOT / "kadak-data"))
PORT = int(os.environ.get("KADAK_PORT", "8765"))
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ORIGINS = {f"http://{h}" for h in HOSTS}
LAYOUTS = {"face", "blur", "crop"}
SECTION = re.compile(r"^\d{1,2}:\d{2}:\d{2}-\d{1,2}:\d{2}:\d{2}$")
JOB_ID = re.compile(r"^[0-9a-f]{12}$")
CLIP_FILE = re.compile(r"^kadak_\d{2}\.mp4$")
FONT_FILE = re.compile(r"^[A-Za-z-]+\.ttf$")

JOBS = {}
LOCK = threading.Lock()  # ponytail: one lock for all job state; fine for one person on one machine


def run_job(jid, req):
    job = JOBS[jid]

    def progress(ev):
        with LOCK:
            if "found" in ev:
                job["found"].append(ev["found"])
            elif "clip" in ev:
                job["clips"].append(ev["clip"])
            else:
                job.update({k: ev[k] for k in ("stage", "pct", "text", "title") if k in ev})

    try:
        clips = kadak.make_clips(req["url"], DATA / jid, count=req["count"], layout=req["layout"],
                                 section=req["section"], progress=progress)
        with LOCK:
            job.update(state="done", pct=100, clips=clips,
                       text=f"{len(clips)} clips ready" if clips else "No strong moments found in this video")
    except kadak.KadakError as e:
        with LOCK:
            job.update(state="error", error=str(e))
    except Exception as e:  # show something useful instead of a silent dead job
        traceback.print_exc()
        with LOCK:
            job.update(state="error", error=f"Something went wrong: {e}")


def parse_request(body):
    """Validate the Make clips form. Returns (request, error message)."""
    try:
        req = json.loads(body or b"{}")
    except ValueError:
        return None, "That request was not readable."
    url = str(req.get("url", "")).strip()
    if not re.match(r"^https?://\S+$", url):
        return None, "Paste a full YouTube or Twitch link that starts with https://"
    if req.get("consent") is not True:
        return None, "Tick the box to confirm you own the video or the creator allows clipping."
    try:
        count = int(req.get("count", 5))
    except (TypeError, ValueError, OverflowError):
        count = 5
    layout = req.get("layout") if req.get("layout") in LAYOUTS else "face"
    section = str(req.get("section") or "").replace(" ", "")
    if section and not SECTION.match(section):
        return None, "Write the part of the stream like 1:05:00-1:17:00"
    return {"url": url, "count": max(1, min(count, 10)), "layout": layout, "section": section or None}, None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet; errors still print

    def send_json(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, ctype, download_name=None):
        size = path.stat().st_size
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):  # Safari needs byte ranges to play video
            if m.group(1):
                start, end = int(m.group(1)), int(m.group(2) or end)
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def host_ok(self):
        # Refuse other host names so a web page can't reach Kadak through DNS rebinding.
        if self.headers.get("Host") in HOSTS:
            return True
        self.send_json(403, {"error": "Open Kadak at http://127.0.0.1:%d" % PORT})
        return False

    def do_GET(self):
        if not self.host_ok():
            return
        parts = self.path.split("?")[0].strip("/").split("/")
        if parts == [""]:
            return self.send_file(ROOT / "web" / "index.html", "text/html; charset=utf-8")
        if len(parts) == 2 and parts[0] == "fonts" and FONT_FILE.match(parts[1]) and (ROOT / "fonts" / parts[1]).is_file():
            return self.send_file(ROOT / "fonts" / parts[1], "font/ttf")
        if len(parts) == 3 and parts[:2] == ["api", "jobs"] and parts[2] in JOBS:
            with LOCK:
                snapshot = json.loads(json.dumps(JOBS[parts[2]]))
            return self.send_json(200, snapshot)
        if len(parts) == 3 and parts[0] == "clips" and JOB_ID.match(parts[1]) and CLIP_FILE.match(parts[2]):
            path = DATA / parts[1] / parts[2]
            if path.is_file():
                name = parts[2] if "dl=1" in self.path else None
                return self.send_file(path, "video/mp4", name)
        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if not self.host_ok():
            return
        if self.path != "/api/jobs":
            return self.send_json(404, {"error": "Not found"})
        # Only Kadak's own page may start a job (blocks cross-site form posts).
        origin = self.headers.get("Origin")
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json" or (origin and origin not in ORIGINS):
            return self.send_json(403, {"error": "Start clips from the Kadak page."})
        body = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 10_000))
        req, err = parse_request(body)
        if err:
            return self.send_json(400, {"error": err})
        with LOCK:
            if any(j["state"] == "running" for j in JOBS.values()):
                return self.send_json(409, {"error": "Kadak is still working on another video. Wait for it to finish."})
            jid = uuid.uuid4().hex[:12]
            JOBS[jid] = {"id": jid, "state": "running", "stage": "download", "pct": 0, "text": "Starting",
                         "title": "", "found": [], "clips": [], "error": ""}
        threading.Thread(target=run_job, args=(jid, req), daemon=True).start()
        self.send_json(200, {"id": jid})


def main():
    url = f"http://127.0.0.1:{PORT}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        raise SystemExit(f"Kadak couldn't start: port {PORT} is busy, so it may already be running. "
                         f"Open {url} in your browser, or close the other copy and try again.")
    for note in kadak.setup_notes():
        print(note, flush=True)
    print(f"Kadak is running at {url}  (press Ctrl+C to stop)", flush=True)
    if not os.environ.get("KADAK_NO_BROWSER"):
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
