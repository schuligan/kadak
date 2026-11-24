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
# "FAILED" on its own: GTA's fail screen says just "Failed" (in a stylised font) over the reason.
KEYWORDS = ["MISSION PASSED", "MISSION COMPLETE", "FAILED", "KNOCKED OUT",
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
    """Seconds -> H:MM:SS, the way YouTube and Twitch show times."""
    t = max(0, int(t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


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

def download(url, out, section, ffmpeg, say, emit):
    if not shutil.which("yt-dlp"):
        raise KadakError("yt-dlp is not installed. Install it with: brew install yt-dlp")
    title, lang = [], []

    def on_line(line):
        # ponytail: a section is fetched by FFmpeg inside yt-dlp, which reports no percentage until the end;
        # the page shows a running timer instead.
        m = re.match(r"\[download\]\s+([\d.]+)%", line)
        if m:
            say("download", int(float(m.group(1)) * 0.3), f"Downloading the video ({float(m.group(1)):.0f}%)")
        elif line.startswith("KADAK_TITLE "):
            title.append(line[12:])
            emit({"title": line[12:]})
        elif line.startswith("KADAK_LANG "):
            lang.append(line[11:].strip())
    # ponytail: downloads the whole video (or the whole section); live streams must have ended.
    # YouTube sometimes blocks a connection's default client ("confirm you're not a bot");
    # other clients often still work, so try them in turn before giving up.
    for client in YT_CLIENTS:
        cmd = ["yt-dlp", "-f", YTDLP_FORMAT, "--merge-output-format", "mp4", "--no-playlist", "--newline", "--force-overwrites",
               "--progress", "--no-simulate", "--print", "before_dl:KADAK_TITLE %(title)s",
               "--print", "before_dl:KADAK_LANG %(language)s",
               "--ffmpeg-location", str(Path(ffmpeg).parent), "-o", str(out / "source.%(ext)s"), *client_args(client),
               "--", url]
        if section:
            cmd[1:1] = ["--download-sections", f"*{section}"]
        code, tail = stream(cmd, on_line)
        media = out / "source.mp4"
        if not media.exists():
            media = next((p for p in sorted(out.glob("source.*")) if p.suffix in (".mp4", ".mkv", ".webm")), None)
        if not code and media:
            known = lang and lang[0] not in ("NA", "none", "", "und")
            return media, (title[0] if title else media.stem), client, (lang[0] if known else None)
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


CAPTION_LANGS = "en-orig,hi-orig,en,hi,en-US,en-GB"


def fetch_captions(url, out, client=None):
    """Captions in a separate call; failure is fine (YouTube often rate-limits subtitles; Twitch has none)."""
    for old in out.glob("subs.*.json3"):
        old.unlink()
    # Original-language tracks first, and --ignore-errors so one refused track (YouTube often answers
    # "429 Too Many Requests" for machine translations) doesn't stop yt-dlp before the others.
    try:
        subprocess.run(["yt-dlp", "--skip-download", "--write-subs", "--write-auto-subs", "--ignore-errors", "--sub-langs",
                        CAPTION_LANGS, "--sub-format", "json3", "--no-playlist", "-o", str(out / "subs"),
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
# Short vowels for long ones, the way Hinglish is typed ("bhai", "kya", "nahin", not "bhaaee", "kyaa", "naheen"),
# which is also how the Hindi words in HYPE are spelled.
VOWELS = dict(zip("अआइईउऊऋएऐओऔ", "a a i i u u ri e ai o au".split()))
MATRAS = dict(zip("ािीुूृेैोौ", "a i i u u ri e ai o au".split()))
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
    # Drop caption markup: speaker changes (">>"), sound notes ("[Music]") and YouTube's bleep "[ __ ]".
    if re.fullmatch(r"\s*(\[.*\]|>+|_+)\s*", w):
        return ""
    w = re.sub(r"[{}\\\[\]<>]", "", w).strip()
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


def whisper_model():
    return os.environ.get("KADAK_WHISPER_MODEL") or next(iter(sorted(Path.home().glob(".cache/whisper/ggml-*.bin"))), None)


def setup_notes():
    """Plain-English lines about anything missing, for the start of the web app. Empty when all is set up."""
    notes = []
    if not shutil.which("yt-dlp"):
        notes.append("Missing yt-dlp, which downloads the videos. Install it with: brew install yt-dlp")
    try:
        if not find_ffmpeg()[2]:
            notes.append("Your FFmpeg can't draw captions, so clips will have none. Fix: brew install ffmpeg-full")
    except KadakError as e:
        notes.append(str(e))
    if not shutil.which("whisper-cli"):
        notes.append("Optional: Whisper isn't installed, so Twitch clips will have no captions. "
                     "See 'Install' in the README (brew install whisper-cpp, then one model file).")
    elif not whisper_model():
        notes.append("Optional: Whisper is installed but its model file isn't in ~/.cache/whisper, so Twitch clips "
                     "will have no captions. The README shows the one-line download.")
    if sys.platform == "darwin" and not (REPO / "bin" / "kadak-vision").exists() and not shutil.which("swiftc"):
        notes.append("Optional: screen reading (WASTED, FAILED, facecam) needs Apple's command line tools: "
                     "xcode-select --install")
    return notes


def whisper_words(wav, out, say, lang=None):
    """Words from local Whisper. `lang` is the video's language if the site said (e.g. "hi"), else None."""
    exe, model = shutil.which("whisper-cli"), whisper_model()
    if not exe or not model:
        fix = "brew install whisper-cpp" if not exe else "download a Whisper model file"
        say("listen", 35, f"This video has no captions of its own and Whisper isn't set up, so clips will have "
                          f"no captions. To fix it: {fix} (see Install in the README)")
        return []
    english_only = bool(re.search(r"\.en[.-]", Path(model).name))  # ggml-base.en.bin, ggml-base.en-q5_1.bin
    if english_only and lang and not lang.lower().startswith("en"):
        # An English-only model turns other languages into made-up English: no captions beat wrong ones.
        say("listen", 35, f"This video is in '{lang}' and only an English Whisper model is installed, "
                          "so clips will have no captions")
        return []

    def on_line(line):
        m = re.search(r"progress\s*=\s*(\d+)%", line)
        if m:
            say("listen", 32 + int(m.group(1)) * 10 // 100, f"Writing down the words ({m.group(1)}%)")
    # ponytail: Whisper runs at CPU/GPU speed of this Mac; a 3-hour stream takes a while with the base model.
    lang_args = [] if english_only else ["-l", lang.split("-")[0] if lang else "auto"]
    stream([exe, "-m", str(model), "-f", str(wav), "-ml", "1", "-sow", "-oj", "-pp", *lang_args,
            "-of", str(out / "whisper")], on_line)
    try:
        data = json.loads((out / "whisper.json").read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        say("listen", 42, "Whisper couldn't write down the words, so clips will have no captions")
        return []
    return whisper_json_words(data)


def whisper_json_words(data):
    """whisper-cli JSON (one word per segment) -> [(start, end, text)], without sound notes like "(upbeat music)"."""
    segs = [(s, s.get("text", "").strip()) for s in data.get("transcription", [])]
    words, skip_to = [], -1
    for i, (seg, raw) in enumerate(segs):
        if i <= skip_to:
            continue
        if raw.startswith(("(", "[", "*")):
            # Drop the note up to its closing word if that comes within 6 words; an unclosed one is just this word.
            skip_to = next((j for j in range(i, min(i + 6, len(segs))) if segs[j][1].endswith((")", "]", "*"))), i)
            continue
        w = clean_word(raw)
        if re.search(r"\w", w):
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
        # "FAILED" only as the whole line ("Failed" screen, "MISSION FAILED"), not "lol failed" in a chat overlay.
        hit = any(re.fullmatch(r"\W*(MISSION\s+)?FAILED\W*", t.upper()) for t in texts) if kw == "FAILED" else kw in joined
        if hit:
            if t - last.get(kw, -99) >= 20:
                new.append((t, kw))
            last[kw] = t
    return new


def keyword_hits(frame_texts, step=2.0):
    last = {}
    return [h for i, texts in enumerate(frame_texts) for h in frame_hits(texts, i * step, last)]


def screen_flags(frame_texts, frame_thumbs):
    """Per frame: (dull, skip). dull = black or frozen screen; skip = not gameplay: a menu (lots of text)
    or a cutscene (black bars top and bottom). Thumbs are 32x18 grey pixels, top row first."""
    n = len(frame_thumbs)
    counts = [len(t) for t in frame_texts]
    many = max(10, statistics.median(counts) + 6) if counts else math.inf  # a chat overlay raises the median

    def diff(a, b):
        return sum(abs(x - y) for x, y in zip(a, b)) / 576 if len(a) == len(b) == 576 else 99

    dull, bars = [], []
    for i, th in enumerate(frame_thumbs):
        if len(th) != 576:
            dull.append(False)
            bars.append(False)
            continue
        other = frame_thumbs[i - 1] if i else frame_thumbs[1] if n > 1 else []
        dull.append(sum(th) / 576 <= 12 or diff(th, other) <= 2)
        bars.append(statistics.median(th[:32]) <= 6 and statistics.median(th[-32:]) <= 20
                    and sum(th[64:-64]) / 448 >= 20)
    if sum(bars) > 0.6 * n:  # the stream itself has black bars, so they say nothing about cutscenes
        bars = [False] * n
    return dull, [b or c >= many for b, c in zip(bars, counts)]


def per_second(flags, n, step=2.0, pad=0):
    """Per-frame flags (one frame every `step` s) -> per-second flags, widened by `pad` seconds."""
    out = [False] * n
    for k, f in enumerate(flags):
        if f:
            for s in range(int(k * step) - pad, int((k + 1) * step) + pad):
                if 0 <= s < n:
                    out[s] = True
    return out


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
    # ponytail: a small webcam is only found in some frames, and menus or cutscenes can hide it for minutes,
    # so 15% of frames (and at least 3) in one spot is enough.
    if n < max(3, 0.15 * len(frames)):
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

def moment_scores(loud_db, shout_db, hype, screen, skip):
    """Per-second scores, smoothed over 5 s. Sound counts for nothing on menus and cutscenes;
    an on-screen word (WASTED, FAILED...) always counts."""
    raw = [(0.0 if sk else 0.35 * min(1, l / 12) + 0.25 * min(1, sh / 12) + 0.25 * h) + sc
           for l, sh, h, sc, sk in zip(loud_db, shout_db, hype, screen, skip)]
    return [statistics.fmean(raw[max(0, i - 2):i + 3]) for i in range(len(raw))]


def cut_points(scores, dull, p, n):
    """(start, end) around the peak second p: start in the calm before the action builds, end soon after
    the payoff: when things go quiet, or 3 s into a black or frozen screen (like a FAILED screen)."""
    top = scores[p]
    lo, hi = max(0, p - 25), max(0, p - 6)
    # The calmest second 6-25 s before the peak (not on a black, frozen or menu screen);
    # near-ties go to the one closest to 12 s before.
    cands = [i for i in range(lo, hi + 1) if not dull[i]] or list(range(lo, hi + 1))
    calm = min(cands, key=lambda i: (round(5 * scores[i] / top), abs(i - (p - 12))))
    start = calm - 1 if calm and not dull[calm - 1] else calm
    end, run = min(n, p + 20), None
    for e in range(p, min(n, p + 20)):
        run = (e if run is None else run) if dull[e] else None
        if run is not None and e - run >= 2:
            end = run + 3
            break
        if e >= p + 3 and all(v < 0.3 * top for v in scores[e:e + 2]):
            end = e + 1.5
            break
    return float(start), float(min(end, n))


def pick_moments(scores, words, count, min_len, max_len, dur=None, dull=None):
    """Greedy best-first clips [(start, end, peak)] over smoothed per-second scores.
    `dull` marks seconds of black, frozen or menu screen, which clips don't open on or linger on."""
    dur = len(scores) if dur is None else dur
    if not scores:
        return []
    if dur < min_len:
        peak = max(range(len(scores)), key=scores.__getitem__)
        return [(0.0, dur, peak)] if scores[peak] >= 0.15 else []
    n = len(scores)
    dull = dull or [False] * n
    gaps = [(a[1] + 0.2, b[0] - 0.2) for a, b in zip(words, words[1:]) if b[0] - a[1] >= 0.5]
    if words:
        gaps = [(0.0, words[0][0] - 0.2)] * (words[0][0] >= 0.5) + gaps + [(words[-1][1] + 0.2, dur)]

    def snap(target, lo, hi):
        """Move a cut into the nearest pause in speech (up to 3 s away), so no word is cut in half."""
        cands = [min(max(target, g0), g1) for g0, g1 in gaps]
        cands = [c for c in cands if abs(c - target) <= 3 and lo <= c <= hi]
        return min(cands, key=lambda c: abs(c - target)) if cands else target

    picked = []
    for p in sorted(range(n), key=lambda i: -scores[i]):
        if len(picked) >= count or scores[p] < 0.15:
            break
        if any(s - 3 <= p <= e + 3 for s, e, _ in picked):
            continue
        start, end = cut_points(scores, dull, p, min(n, int(dur)))
        if end - start > max_len:  # too long: less build-up, but keep the peak
            start = max(start, min(end - max_len, p - 2))
            end = min(end, start + max_len)
        if end - start < min_len:  # too short: more build-up first, then more after
            start = max(0.0, end - min_len)
            while start < p - 2 and dull[int(start)]:  # but don't open on a black, frozen or menu screen
                start += 1
            end = min(dur, start + min_len)
            start = min(start, max(0.0, end - min_len))  # at the very end of the video, length wins
        start = snap(start, max(0.0, end - max_len), max(0.0, min(p - 2, end - min_len)))
        end = snap(end, max(p + 1, start + min_len, end - 2), min(dur, start + max_len, end + 2))
        if any(start < e + 3 and end > s - 3 for s, e, _ in picked):
            continue
        picked.append((round(start, 2), round(end, 2), p))
    return picked


def sentences(words):
    """Words -> sentences, split after . ! ? or at a pause of 0.6 s or more."""
    out, cur = [], []
    for w in words:
        if cur and w[0] - cur[-1][1] >= 0.6:
            out.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.!?…]$", w[2]):
            out.append(cur)
            cur = []
    return out + [cur] * bool(cur)


def clip_title(words, peak):
    """A whole sentence said at (or nearest to) the peak, preferring ones of 3+ words; None if nobody spoke."""
    near = [s for s in sentences(words) if s[0][0] <= peak + 4 and s[-1][1] >= peak - 6
            and any(re.search(r"\w", w) for _, _, w in s)]
    if not near:
        return None
    best = min(near, key=lambda s: (len(s) < 3, max(0.0, s[0][0] - peak, peak - s[-1][1])))
    text = " ".join(w for _, _, w in best[:12]).rstrip(",;:-") + ("…" if len(best) > 12 else "")
    return text[0].upper() + text[1:]


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
    title = clip_title(words, peak)
    return why, f"“{title}”" if title else f"Moment at {hms(t0 + peak)}"


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
            "Style: Kadak,Anton,100,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,7,0,5,"
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
# Every clip at the same loudness reels use (-14 LUFS), with peaks kept under -1.5 dB so nothing distorts.
LOUDNESS = "loudnorm=I=-14:TP=-1.5:LRA=11"


def render(ffmpeg, src, seek, length, vf, out, name):
    while ENCODERS:
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{seek:.3f}", "-t", f"{length:.3f}",
               "-i", str(src), "-filter_complex", vf, "-map", "[v]", "-map", "0:a:0?", *ENCODERS[0],
               "-af", LOUDNESS, "-ar", "48000", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", name]
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
        say("download", 0, "This FFmpeg can't draw captions, so clips will have none. "
                           "To fix it: brew install ffmpeg-full")
    caps = None
    if is_url:
        src, title, client, lang = download(source.strip(), out, section, ffmpeg, say, emit)
        off = 0.0
        caps = fetch_captions(source.strip(), out, client)
    else:
        src = Path(source).expanduser().resolve()  # clips render from inside the output folder
        if not src.is_file():
            raise KadakError(f"Can't find the file {source}.")
        title, off, lang = src.stem, t0, None
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
        words = whisper_words(wav, out, say, lang)
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
    frame_texts, frame_faces, frame_thumbs, hits = [], [], [], []
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
            frame_thumbs.append(d.get("thumb", []))
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
    # Menus and cutscenes aren't moments: they score nothing. Black or frozen screens cut a clip short.
    dull, skip = screen_flags(frame_texts, frame_thumbs)
    dull, skip = per_second(dull, n), per_second(skip, n, pad=2)
    smooth = moment_scores(loud_db, shout_db, hype, screen, skip)
    picks = pick_moments(smooth, words, count, min_len, max_len, dur, [d or k for d, k in zip(dull, skip)])
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
        # start/end/from/to are times in the whole source video, whether or not a part was downloaded.
        clip = {"file": name, "start": round(t0 + start, 2), "end": round(t0 + end, 2), "at": hms(t0 + start),
                "from": hms(t0 + start), "to": hms(t0 + end), "dur": f"{d // 60}:{d % 60:02d}", "title": ttl,
                "why": why}
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
            c = e["clip"]
            print(f"       made {c['file']}  {c['from']}-{c['to']} in the video  {c['title']}", flush=True)
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
