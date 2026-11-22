#!/usr/bin/env python3
"""Kadak engine: a stream/video link or file in -> best moments -> vertical 9:16 MP4s with word captions.

Fully local. Python stdlib + yt-dlp + FFmpeg; optional whisper-cli and swiftc (macOS Vision for screen text/faces).
"""
import argparse
import atexit
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent
KEYWORDS = ["MISSION PASSED", "MISSION FAILED", "MISSION COMPLETE", "KNOCKED OUT",
            "WASTED", "BUSTED", "ELIMINATED", "VICTORY"]
HYPE = ["oh my god", "omg", "no no", "what the", "lets go", "run", "bro", "wasted", "busted", "police",
        "cops", "holy", "insane", "crazy", "wait what", "oh no", "bhaago", "bhago", "bhai", "arre", "kya",
        "mar gaya", "pakad", "pakdo", "bachao"]
HYPE_TOKENS = [tuple(h.split()) for h in HYPE]
YTDLP_FORMAT = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b"
ACTIVE = r"{\c&H8AD9FF&}"  # #FFD98A in ASS's BGR order


class KadakError(Exception):
    """A user-facing failure; the message is plain English."""


# ---------- small helpers ----------

def hms(t):
    t = max(0, int(t))
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def parse_time(s):
    try:
        return float(sum(float(p) * 60 ** i for i, p in enumerate(reversed(s.strip().split(":")))))
    except ValueError:
        raise KadakError(f"'{s}' is not a time. Use H:MM:SS, like 0:41:06.")


def parse_section(section):
    a, sep, b = section.partition("-")
    if not sep:
        raise KadakError("Write the section as start-end, like 0:41:00-0:45:00.")
    start, end = parse_time(a), parse_time(b)
    if end <= start:
        raise KadakError("The section's end must come after its start, like 0:41:00-0:45:00.")
    return start, end


def stream(cmd, on_line=None, **kw):
    """Run cmd, feeding each output line (stdout+stderr, '\\r' counts as a line break) to on_line."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         errors="replace", **kw)
    RUNNING.add(p)
    tail = []
    try:
        for line in p.stdout:
            line = line.rstrip()
            tail = (tail + [line])[-15:]
            if on_line:
                on_line(line)
        return p.wait(), tail
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
        RUNNING.discard(p)


# Long-running helpers (yt-dlp, Whisper, the Vision reader) are stopped when Kadak quits.
# ponytail: short FFmpeg renders run with subprocess.run and can outlive a quit by a few seconds.
RUNNING = set()
atexit.register(lambda: [p.kill() for p in list(RUNNING)])


def find_ffmpeg():
    """First FFmpeg that has the 'ass' filter (libass), else any FFmpeg without captions."""
    cands = [os.environ.get("KADAK_FFMPEG"), "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
             "/usr/local/opt/ffmpeg-full/bin/ffmpeg", shutil.which("ffmpeg")]
    found = []
    for c in cands:
        if not c or not os.path.exists(c):
            continue
        try:
            out = subprocess.run([c, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        except OSError:
            continue
        probe = Path(c).with_name("ffprobe")
        found.append((c, str(probe) if probe.exists() else "ffprobe"))
        if re.search(r"^\s*\S+\s+ass\s", out, re.M):
            return found[-1] + (True,)
    if not found:
        raise KadakError("FFmpeg is not installed. Install it with: brew install ffmpeg-full")
    return found[0] + (False,)


def probe(ffprobe, path):
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
                        "-of", "json", str(path)], capture_output=True, text=True)
    if r.returncode:
        raise KadakError("That file doesn't look like a video FFmpeg can read.")
    info = json.loads(r.stdout)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise KadakError("That file has no picture (video track), so there's nothing to cut.")
    if not any(s.get("codec_type") == "audio" for s in info["streams"]):
        raise KadakError("This video has no audio track, so Kadak can't hear the big moments.")
    return float(info["format"].get("duration") or 0), int(video["width"]), int(video["height"])


# ---------- download ----------

def download(url, out, section, ffmpeg, say):
    if not shutil.which("yt-dlp"):
        raise KadakError("yt-dlp is not installed. Install it with: brew install yt-dlp")
    title = []

    def on_line(line):
        m = re.match(r"\[download\]\s+([\d.]+)%", line)
        if m:
            say("download", int(float(m.group(1)) * 0.3), f"Downloading the video ({float(m.group(1)):.0f}%)")
        elif line.startswith("KADAK_TITLE "):
            title.append(line[12:])
    # ponytail: downloads the whole video (or the whole section); live streams must have ended.
    # YouTube sometimes blocks a connection's default client ("confirm you're not a bot");
    # other clients often still work, so try them in turn before giving up.
    for client in YT_CLIENTS:
        cmd = ["yt-dlp", "-f", YTDLP_FORMAT, "--merge-output-format", "mp4", "--no-playlist", "--newline", "--force-overwrites",
               "--progress", "--no-simulate", "--print", "before_dl:KADAK_TITLE %(title)s",
               "--ffmpeg-location", str(Path(ffmpeg).parent), "-o", str(out / "source.%(ext)s"), *client_args(client),
               "--", url]
        if section:
            cmd[1:1] = ["--download-sections", f"*{section}"]
        code, tail = stream(cmd, on_line)
        media = out / "source.mp4"
        if not media.exists():
            media = next((p for p in sorted(out.glob("source.*")) if p.suffix in (".mp4", ".mkv", ".webm")), None)
        if not code and media:
            return media, (title[0] if title else media.stem), client
        err = next((l for l in reversed(tail) if "ERROR" in l), tail[-1] if tail else "")
        if not any(s in err for s in ("not a bot", "reloaded", "format is not available", "403")):
            break
        say("download", 0, "YouTube blocked that request, trying another way")
    if "not a bot" in err:
        raise KadakError("YouTube asked this connection to prove it isn't a bot. Wait a few minutes and try "
                         "again, or try another network (turning a VPN off often helps).")
    raise KadakError(f"Couldn't download that link. Check it's a public video and try again. ({err[:200]})")


YT_CLIENTS = [None, "mweb", "android_vr"]


def client_args(client):
    return ["--extractor-args", f"youtube:player_client={client}"] if client else []


def fetch_captions(url, out, client=None):
    """Captions in a separate call; failure is fine (YouTube often rate-limits subtitles; Twitch has none)."""
    for old in out.glob("subs.*.json3"):
        old.unlink()
    try:
        subprocess.run(["yt-dlp", "--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs",
                        "en,en-orig,en-US,en-GB,hi,hi-orig", "--sub-format", "json3", "--no-playlist", "-o", str(out / "subs"),
                        *client_args(client), "--", url],
                       capture_output=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return None
    best = None
    for f in out.glob("subs.*.json3"):
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except ValueError:
            continue
        # Speech-recognition tracks carry acAsrConf: they are what was actually said, word-timed.
        # A track without it may be a machine translation (e.g. English of a Hindi stream), so it comes second.
        # ponytail: this also ranks rare human-made captions below the ASR track.
        asr = any("acAsrConf" in s for e in data.get("events", []) for s in e.get("segs", []))
        rank = (not asr, not f.name.split(".")[-2].startswith("en"))
        if best is None or rank < best[0]:
            best = (rank, data)
    return best and best[1]


# ---------- words ----------

CONS = dict(zip("कखगघङचछजझञटठडढणतथदधनपफबभमयरलवशषसह\u0958\u0959\u095a\u095b\u095c\u095d\u095e\u095f",
                "k kh g gh n ch chh j jh n t th d dh n t th d dh n p ph b bh m y r l v sh sh s h "
                "q kh g z r rh f y".split()))
VOWELS = dict(zip("अआइईउऊऋएऐओऔ", "a aa i ee u oo ri e ai o au".split()))
MATRAS = dict(zip("ािीुूृेैोौ", "aa i ee u oo ri e ai o au".split()))
DIGITS = dict(zip("०१२३४५६७८९।", "0123456789."))


def translit(s):
    """Devanagari -> rough Roman letters (Hinglish style); drops the inherent 'a' at word end."""
    out, pend = [], False
    for ch in s:
        if ch in CONS:
            out += ["a"] * pend + [CONS[ch]]
            pend = True
        elif ch in MATRAS:
            out.append(MATRAS[ch])
            pend = False
        elif ch in VOWELS or ch in "ंँः":
            out += ["a"] * pend + [VOWELS.get(ch, "h" if ch == "ः" else "n")]
            pend = False
        elif ch in "़्":  # virama kills the inherent vowel; nukta is ignored
            pend = pend and ch == "़"
        else:
            pend = False
            out.append(DIGITS.get(ch, ch if ch.isascii() else ""))
    return "".join(out)


def clean_word(w):
    w = w.replace("{", "").replace("}", "").replace("\\", "").strip()
    return translit(w) if re.search("[ऀ-ॿ]", w) else w


def json3_words(data, offset=0.0, dur=math.inf):
    """YouTube json3 captions -> [(start, end, text)], times minus offset (the section start)."""
    raw = []
    for e in data.get("events", []):
        segs = [s for s in e.get("segs") or [] if s.get("utf8", "").strip()]
        t0 = e.get("tStartMs", 0)
        for k, s in enumerate(segs):
            start = t0 + s.get("tOffsetMs", 0)
            nxt = t0 + segs[k + 1].get("tOffsetMs", 0) if k + 1 < len(segs) else t0 + e.get("dDurationMs", 1000)
            toks = s["utf8"].split()
            step = max(0, nxt - start) / len(toks)
            raw += [((start + i * step) / 1000 - offset, clean_word(t)) for i, t in enumerate(toks)]
    raw = sorted((t, w) for t, w in raw if w and 0 <= t < dur)
    return [(t, min(raw[i + 1][0] if i + 1 < len(raw) else math.inf, t + 1.0), w) for i, (t, w) in enumerate(raw)]


def whisper_words(wav, out, say):
    exe = shutil.which("whisper-cli")
    model = os.environ.get("KADAK_WHISPER_MODEL") or next(iter(sorted(Path.home().glob(".cache/whisper/ggml-*.bin"))), None)
    if not exe or not model:
        say("listen", 35, "No captions and no Whisper found, so clips will have no captions")
        return []

    def on_line(line):
        m = re.search(r"progress\s*=\s*(\d+)%", line)
        if m:
            say("listen", 32 + int(m.group(1)) * 10 // 100, f"Writing down the words ({m.group(1)}%)")
    # ponytail: Whisper runs at CPU/GPU speed of this Mac; a 3-hour stream takes a while with the base model.
    stream([exe, "-m", str(model), "-f", str(wav), "-ml", "1", "-sow", "-oj", "-pp", "-of", str(out / "whisper")], on_line)
    try:
        data = json.loads((out / "whisper.json").read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        say("listen", 42, "Whisper couldn't write down the words, so clips will have no captions")
        return []
    words = []
    for seg in data.get("transcription", []):
        w = clean_word(seg.get("text", ""))
        if re.search(r"\w", w) and not w.startswith("["):
            words.append((seg["offsets"]["from"] / 1000, seg["offsets"]["to"] / 1000, w))
    return words


# ---------- audio ----------

def rms_per_second(ffmpeg, wav, band=""):
    chain = band + "aresample=16000,asetnsamples=16000,astats=metadata=1:reset=1," \
                   "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-"
    r = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-i", str(wav), "-af", chain, "-f", "null", "-"],
                       capture_output=True, text=True)
    vals = re.findall(r"RMS_level=(\S+)", r.stdout)
    return [max(-70.0, float(v)) if v not in ("-inf", "nan", "inf") else -70.0 for v in vals]


def rise(db, window=120, warmup=30):
    """dB above the rolling median of the previous `window` seconds (never negative).
    The first `warmup` seconds share one baseline, so a quiet intro doesn't make ordinary talk look loud."""
    return [max(0.0, v - statistics.median(db[max(0, i - window):max(i, min(warmup, len(db)))])) for i, v in enumerate(db)]


def hype_hits(words, n):
    """Per-second hype score (cap 1) and per-second list of the hype phrases heard."""
    toks = [(int(s), re.sub(r"[^a-z0-9 ]", "", w.lower())) for s, _, w in words]
    toks = [(s, t) for s, t in toks if t]
    per = [[] for _ in range(n)]
    for j, (sec, _) in enumerate(toks):
        for ph in HYPE_TOKENS:
            if tuple(t for _, t in toks[j:j + len(ph)]) == ph and sec < n:
                per[sec].append(" ".join(ph))
    return [min(1.0, len(p)) for p in per], per


# ---------- screen (macOS Vision) ----------

def vision_binary(say):
    exe, src = REPO / "bin" / "kadak-vision", REPO / "ocr.swift"
    if exe.exists() and exe.stat().st_mtime >= src.stat().st_mtime:
        return exe
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        say("read", 45, "Screen reading needs a Mac with Xcode tools, skipping it")
        return None
    exe.parent.mkdir(exist_ok=True)
    r = subprocess.run(["swiftc", "-O", "-module-cache-path", str(exe.parent / ".swiftcache"), str(src), "-o", str(exe)],
                       capture_output=True, text=True)
    if r.returncode or not exe.exists():
        say("read", 45, "Couldn't build the screen reader, skipping it")
        return None
    return exe


def frame_hits(texts, t, last):
    """New (t, KEYWORD) hits in one frame's OCR strings; `last` remembers sightings so the same
    keyword within 20 s counts once."""
    joined, new = " | ".join(texts).upper(), []
    for kw in KEYWORDS:
        if kw in joined:
            if t - last.get(kw, -99) >= 20:
                new.append((t, kw))
            last[kw] = t
    return new


def keyword_hits(frame_texts, step=2.0):
    last = {}
    return [h for i, texts in enumerate(frame_texts) for h in frame_hits(texts, i * step, last)]


def facecam(frames, W, H):
    """Facecam region (x, y, w, h) in pixels, aspect 1080:640, or None. frames = per-frame lists of [x,y,w,h] 0..1."""
    boxes, counts = {}, Counter()
    for faces in frames:
        cells = set()
        for x, y, w, h in faces:
            if w * h < 0.12:
                cell = (int((x + w / 2) * 10), int((y + h / 2) * 10))
                boxes.setdefault(cell, []).append((x, y, w, h))
                cells.add(cell)
        counts.update(cells)  # a cell counts once per frame
    if not counts:
        return None
    (cell, n), = counts.most_common(1)
    if n < 0.3 * len(frames):
        return None
    x, y, w, h = (statistics.median(b[i] for b in boxes[cell]) for i in range(4))
    cx, cy, bw, bh = (x + w / 2) * W, (y + h / 2) * H, w * W * 2.4, h * H * 2.4
    r = 1080 / 640
    bw, bh = (bh * r, bh) if bw / bh < r else (bw, bw / r)
    if bw > W:
        bw, bh = W, W / r
    if bh > H:
        bw, bh = H * r, H
    x0, y0 = min(max(0, cx - bw / 2), W - bw), min(max(0, cy - bh / 2), H - bh)
    return int(x0), int(y0), int(bw), int(bh)


# ---------- pick ----------

def pick_moments(scores, words, count, min_len, max_len, dur=None):
    """Greedy best-first windows [(start, end, peak)] over smoothed per-second scores."""
    dur = len(scores) if dur is None else dur
    if not scores:
        return []
    if dur < min_len:
        peak = max(range(len(scores)), key=scores.__getitem__)
        return [(0.0, dur, peak)] if scores[peak] >= 0.15 else []
    L = (min_len + max_len) / 2
    gaps = [(a[1] + 0.2, b[0] - 0.2) for a, b in zip(words, words[1:]) if b[0] - a[1] >= 0.5]
    if words:
        gaps = [(0.0, words[0][0] - 0.2)] * (words[0][0] >= 0.5) + gaps + [(words[-1][1] + 0.2, dur)]

    def snap(target, lo, hi):
        cands = [min(max(target, g0), g1) for g0, g1 in gaps]
        cands = [c for c in cands if abs(c - target) <= 3 and lo <= c <= hi]
        return min(cands, key=lambda c: abs(c - target)) if cands else target

    picked = []
    for p in sorted(range(len(scores)), key=lambda i: -scores[i]):
        if len(picked) >= count or scores[p] < 0.15:
            break
        if any(s - 3 <= p <= e + 3 for s, e, _ in picked):
            continue
        start = min(max(0.0, p + 0.5 - 0.6 * L), max(0.0, dur - L))
        start = snap(start, 0.0, max(0.0, dur - min_len))
        end = min(start + L, dur)
        end = snap(end, start + min_len, min(start + max_len, dur))
        if end - start < min_len:
            start = max(0.0, end - min_len)
        if any(start < e + 3 and end > s - 3 for s, e, _ in picked):
            continue
        picked.append((round(start, 2), round(end, 2), p))
    return picked


def describe(start, end, peak, t0, hits, loud_db, shout_db, hype_per, words):
    lo, hi = int(start), int(math.ceil(end))
    why = [f"{kw} on screen" for t, kw in hits if start - 2 <= t <= end + 2]
    why = list(dict.fromkeys(why))
    jump = max(loud_db[lo:hi], default=0)
    if jump >= 6:
        why.append(f"voice +{round(jump)} dB")
    if max(shout_db[lo:hi], default=0) >= 9:  # ponytail: fixed "shouting" threshold (9 dB above the band's median)
        why.append("shouting")
    top = Counter(w for sec in hype_per[lo:hi] for w in sec).most_common(2)
    if top:
        why.append("hype words: " + ", ".join(w for w, _ in top))
    near = sorted(sorted((w for w in words if abs(w[0] - peak) <= 4), key=lambda w: abs(w[0] - peak))[:8])
    title = " ".join(w for _, _, w in near)
    title = f"“{title[0].upper() + title[1:]}”" if title else f"Moment at {hms(t0 + peak)}"
    return why, title


# ---------- render ----------

def ass_time(t):
    cs = int(round(max(0.0, t) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def build_ass(words, start, end, y):
    """ASS captions for [start, end): groups of up to 3 words, one Dialogue per word, active word coloured."""
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n"
            "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, "
            "SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
            "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Kadak,Anton,84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,6,0,5,"
            "60,60,0,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    ws = [w for w in words if start <= w[0] < end]
    groups = []
    for w in ws:
        if groups and len(groups[-1]) < 3 and w[0] - groups[-1][-1][1] <= 0.6:
            groups[-1].append(w)
        else:
            groups.append([w])
    lines = []
    for g in groups:
        for k, (s, e, _) in enumerate(g):
            text = " ".join(ACTIVE + t.upper() + r"{\r}" if i == k else t.upper() for i, (_, _, t) in enumerate(g))
            lines.append(f"Dialogue: 0,{ass_time(s - start)},{ass_time(min(e, end) - start)},Kadak,,0,0,0,,"
                         f"{{\\an5\\pos(540,{y})}}{text}")
    return head + "\n".join(lines) + "\n"


def video_filter(layout, face, W, H, ass_name):
    if layout == "crop":
        v = r"[0:v]crop=min(iw\,ih*9/16):min(ih\,iw*16/9),scale=1080:1920"
    elif layout == "face":
        x, y, w, h = face
        v = (f"[0:v]split[a][b];[a]crop={w}:{h}:{x}:{y},scale=1080:640[top];"
             f"[b]crop=min(iw\\,ih*1080/1280):min(ih\\,iw*1280/1080),scale=1080:1280[bot];[top][bot]vstack")
    else:
        v = ("[0:v]split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
             "boxblur=20:2[bg];[b]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
             "[bg][fg]overlay=(W-w)/2:(H-h)/2")
    if ass_name:
        # ponytail: a fonts path containing a single quote would break this filter string.
        v += f",ass={ass_name}:fontsdir='{REPO / 'fonts'}'"
    return v + ",setsar=1,format=yuv420p[v]"


ENCODERS = [["-c:v", "h264_videotoolbox", "-b:v", "10M"], ["-c:v", "libx264", "-crf", "20", "-preset", "veryfast"]]


def render(ffmpeg, src, seek, length, vf, out, name):
    while ENCODERS:
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{seek:.3f}", "-t", f"{length:.3f}",
               "-i", str(src), "-filter_complex", vf, "-map", "[v]", "-map", "0:a:0?", *ENCODERS[0],
               "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", name]
        r = subprocess.run(cmd, cwd=out, capture_output=True, text=True)
        if r.returncode == 0:
            return
        if len(ENCODERS) == 1 or "videotoolbox" not in r.stderr.lower():
            raise KadakError(f"Couldn't make {name}. FFmpeg said: {r.stderr.strip()[-300:]}")
        ENCODERS.pop(0)  # this Mac's hardware encoder failed: use libx264 from now on


# ---------- the whole thing ----------

WORK_FILES = ("source.*", "audio.wav", "whisper.*", "subs.*", "kadak_*.ass", "frames")


def tidy(out):
    """Remove the working files Kadak made (big source video, WAV, frames); keep the finished clips."""
    for pattern in WORK_FILES:
        for p in Path(out).glob(pattern):
            shutil.rmtree(p) if p.is_dir() else p.unlink()


def make_clips(source, out_dir, count=5, min_len=20, max_len=50, layout="face", section=None, progress=None):
    try:
        return _make_clips(source, out_dir, count, min_len, max_len, layout, section, progress)
    finally:
        tidy(Path(out_dir).resolve())


def _make_clips(source, out_dir, count, min_len, max_len, layout, section, progress):
    emit = progress or (lambda e: None)

    def say(stage, pct, text):
        emit({"stage": stage, "pct": pct, "text": text})

    if layout not in ("face", "blur", "crop"):
        raise KadakError("Layout must be face, blur or crop.")
    if not 0 < min_len <= max_len:
        raise KadakError("The shortest clip length must be above 0 and not more than the longest.")
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    ffmpeg, ffprobe, has_ass = find_ffmpeg()
    t0, t1 = parse_section(section) if section else (0.0, None)
    is_url = bool(re.match(r"https?://", source.strip()))

    # 1. download / open
    say("download", 0, "Downloading the video" if is_url else "Opening the video")
    if not has_ass:
        say("download", 0, "Captions off: install ffmpeg-full")
    caps = None
    if is_url:
        src, title, client = download(source.strip(), out, section, ffmpeg, say)
        off = 0.0
        caps = fetch_captions(source.strip(), out, client)
    else:
        src = Path(source).expanduser()
        if not src.is_file():
            raise KadakError(f"Can't find the file {source}.")
        title, off = src.stem, t0
    emit({"title": title})
    full, W, H = probe(ffprobe, src)
    if not full:
        raise KadakError("Couldn't tell how long this video is. If it's a live stream, try again after it ends.")
    dur = full if is_url else (min(t1, full) if t1 else full) - off
    if dur <= 0:
        raise KadakError("The section starts after the end of the video.")
    seek = ["-ss", f"{off:.3f}", "-t", f"{dur:.3f}", "-i", str(src)]
    say("download", 30, "Video ready")

    # 2. listen: words + loudness
    say("listen", 30, "Listening to the audio")
    wav = out / "audio.wav"
    # ponytail: 16 kHz mono WAV of the whole media (~115 MB per hour) on disk.
    r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *seek, "-vn", "-ac", "1", "-ar", "16000",
                        "-c:a", "pcm_s16le", str(wav)], capture_output=True, text=True)
    if r.returncode:
        raise KadakError("Couldn't read the audio from this video.")
    words = json3_words(caps, t0, dur) if caps else []
    if not words:
        say("listen", 32, "Writing down the words")
        words = whisper_words(wav, out, say)
    loud_db = rise(rms_per_second(ffmpeg, wav))
    shout_db = rise(rms_per_second(ffmpeg, wav, "highpass=f=2000,lowpass=f=4000,"))
    n = len(loud_db)
    if not n:
        raise KadakError("Couldn't measure the audio of this video.")
    shout_db = (shout_db + [0.0] * n)[:n]
    hype, hype_per = hype_hits(words, n)
    say("listen", 45, f"Heard {len(words)} words")

    # 3. read the screen (macOS only, never fatal)
    say("read", 45, "Reading the screen")
    frame_texts, frame_faces, hits = [], [], []
    exe = vision_binary(say)
    if exe:
        frames = out / "frames"
        shutil.rmtree(frames, ignore_errors=True)
        frames.mkdir()
        # ponytail: one frame per 2 s; a quick WASTED flash shorter than that can be missed.
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", *seek, "-vf", "fps=0.5,scale=960:-2",
                        "-q:v", "4", str(frames / "f%06d.jpg")], capture_output=True)
        total = max(1, len(list(frames.glob("*.jpg"))))
        say("read", 55, f"Reading {total} frames")
        seen = {}

        def on_line(line):
            if not line.startswith("{"):
                return
            try:
                d = json.loads(line)
            except ValueError:
                return
            frame_texts.append(d.get("text", []))
            frame_faces.append(d.get("faces", []))
            for t, kw in frame_hits(frame_texts[-1], (len(frame_texts) - 1) * 2.0, seen):
                hits.append((t, kw))
                emit({"found": {"at": hms(t0 + t), "what": f"{kw} on screen"}})
            if len(frame_texts) % 25 == 0:
                say("read", 55 + 15 * len(frame_texts) // total, "Reading the screen")
        try:
            code, _ = stream([str(exe), str(frames)], on_line)
        except OSError:
            code = 1
        if code:
            say("read", 70, "Screen reader failed, using sound only")
    say("read", 70, f"Found {len(hits)} screen moments")

    # 4. pick
    say("pick", 70, "Picking the best moments")
    screen = [0.0] * n
    for t, _ in hits:
        for s in range(int(t) - 2, int(t) + 3):
            if 0 <= s < n:
                screen[s] = 1.0
    raw = [0.35 * min(1, loud_db[i] / 12) + 0.25 * min(1, shout_db[i] / 12) + 0.25 * hype[i] + screen[i]
           for i in range(n)]
    smooth = [statistics.fmean(raw[max(0, i - 2):i + 3]) for i in range(n)]
    picks = pick_moments(smooth, words, count, min_len, max_len, dur)
    face = None
    if layout == "face":
        face = facecam(frame_faces, W, H)
        if not face:
            layout = "blur"
            say("pick", 75, "No facecam found, using blurred fill")
    moments = []
    for start, end, peak in picks:
        why, ttl = describe(start, end, peak, t0, hits, loud_db, shout_db, hype_per, words)
        emit({"found": {"at": hms(t0 + peak), "what": why[0] if why else "big moment"}})
        moments.append((start, end, why, ttl))
    if not moments:
        say("done", 100, "No strong moments found, try a longer video or another section")
        return []
    say("pick", 75, f"Picked {len(moments)} moments")

    # 5. render
    clips = []
    for i, (start, end, why, ttl) in enumerate(moments, 1):
        say("render", 75 + 25 * (i - 1) // len(moments), f"Making clip {i} of {len(moments)}")
        name, ass = f"kadak_{i:02d}.mp4", None
        if has_ass and words:
            ass = f"kadak_{i:02d}.ass"
            (out / ass).write_text(build_ass(words, start, end, 720 if layout == "face" else 1350), encoding="utf-8")
        render(ffmpeg, src, off + start, end - start, video_filter(layout, face, W, H, ass), out, name)
        d = round(end - start)
        clip = {"file": name, "start": round(off + start, 2), "end": round(off + end, 2), "at": hms(t0 + start),
                "dur": f"{d // 60}:{d % 60:02d}", "title": ttl, "why": why}
        clips.append(clip)
        emit({"clip": clip})
    say("done", 100, f"Made {len(clips)} clips")
    return clips


def main():
    ap = argparse.ArgumentParser(description="Kadak: find the best moments and cut vertical clips.")
    ap.add_argument("source", help="video link or local file")
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--min", type=float, default=20)
    ap.add_argument("--max", type=float, default=50)
    ap.add_argument("--layout", choices=["face", "blur", "crop"], default="face")
    ap.add_argument("--section", help="only this part, like 0:41:00-0:45:00")
    ap.add_argument("--out", default="kadak-data/cli")
    a = ap.parse_args()

    def show(e):
        if "stage" in e:
            print(f"[{e['pct']:3d}%] {e['text']}", flush=True)
        elif "found" in e:
            print(f"       found {e['found']['at']}  {e['found']['what']}", flush=True)
        elif "clip" in e:
            print(f"       made {e['clip']['file']}  {e['clip']['title']}", flush=True)
        elif "title" in e:
            print(f"       title: {e['title']}", flush=True)
    try:
        clips = make_clips(a.source, a.out, a.count, a.min, a.max, a.layout, a.section, show)
    except KadakError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(clips, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
