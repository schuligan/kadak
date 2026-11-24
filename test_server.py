"""Checks for server.py's request rules, using a stand-in for the clip engine."""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.request
from urllib.error import HTTPError

fake = types.ModuleType("kadak")


class KadakError(Exception):
    pass


def make_clips(source, out_dir, count=5, min_len=20, max_len=50, layout="face", section=None, progress=None):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "kadak_01.mp4"), "wb") as f:
        f.write(b"0123456789")
    clip = {"file": "kadak_01.mp4", "start": 0.0, "end": 30.0, "at": "0:00:10", "from": "0:00:10", "to": "0:00:40", "dur": "0:30", "title": "“Test”", "why": ["voice +9 dB"]}
    progress({"title": "Test video"})
    progress({"found": {"at": "00:00:10", "what": "WASTED on screen"}})
    progress({"clip": clip})
    return [clip]


fake.KadakError, fake.make_clips = KadakError, make_clips
real = sys.modules.get("kadak")
sys.modules["kadak"] = fake

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
os.environ.update(KADAK_PORT=str(PORT), KADAK_DATA=tempfile.mkdtemp(), KADAK_NO_BROWSER="1")
import server  # noqa: E402

# server keeps the stand-in; put the real engine back so test_kadak still tests the real one in the same run.
if real:
    sys.modules["kadak"] = real
else:
    del sys.modules["kadak"]

BASE = f"http://127.0.0.1:{PORT}"


def call(path, body=None, headers=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None, headers=headers or {})
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read(), r.headers
    except HTTPError as e:
        return e.code, e.read(), e.headers


GOOD = {"url": "https://www.youtube.com/watch?v=x", "consent": True, "count": 2, "layout": "blur"}


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def test_page_and_fonts(self):
        self.assertEqual(call("/")[0], 200)
        self.assertEqual(call("/fonts/Anton-Regular.ttf")[0], 200)
        self.assertEqual(call("/fonts/..%2Fserver.py")[0], 404)

    def test_other_host_names_refused(self):
        self.assertEqual(call("/", headers={"Host": "evil.example"})[0], 403)

    def test_cross_site_post_refused(self):
        self.assertEqual(call("/api/jobs", GOOD, {"Origin": "https://evil.example"})[0], 403)

    def test_consent_and_link_required(self):
        self.assertEqual(call("/api/jobs", dict(GOOD, consent=False))[0], 400)
        self.assertEqual(call("/api/jobs", dict(GOOD, url="file:///etc/passwd"))[0], 400)
        self.assertEqual(call("/api/jobs", dict(GOOD, section="soon"))[0], 400)

    def test_job_runs_and_clip_downloads_with_ranges(self):
        status, body, _ = call("/api/jobs", GOOD, {"Origin": BASE})
        self.assertEqual(status, 200)
        jid = json.loads(body)["id"]
        for _ in range(50):
            job = json.loads(call(f"/api/jobs/{jid}")[1])
            if job["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["found"][0]["what"], "WASTED on screen")
        self.assertEqual(len(job["clips"]), 1)
        status, body, headers = call(f"/clips/{jid}/kadak_01.mp4", headers={"Range": "bytes=2-5"})
        self.assertEqual((status, body), (206, b"2345"))
        status, _, headers = call(f"/clips/{jid}/kadak_01.mp4?dl=1")
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertEqual(call(f"/clips/{jid}/..%2F..%2Fserver.py")[0], 404)


if __name__ == "__main__":
    unittest.main()
