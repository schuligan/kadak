# Kadak

Paste a YouTube or Twitch link and get vertical 9:16 clips with word-by-word captions. Kadak finds the loud, wild moments in the video (and on a Mac reads WASTED, BUSTED or MISSION PASSED off the screen), cuts them and lets you download MP4s.

Free and open source (MIT). It runs entirely on your computer: no account, no upload, no AI service, no API keys, no tracking, no auto-updates.

## Only clip what you're allowed to

Clip your own videos, or creators who allow it (many streamers run clipping campaigns with their own rules). Kadak asks you to confirm this before every run but can't check it for you.

## Install (Mac)

You need Python 3.10 or newer, yt-dlp and an FFmpeg build that can burn captions (libass):

```bash
brew install python yt-dlp ffmpeg-full
```

Optional: `brew install whisper-cpp` and a Whisper model in `~/.cache/whisper/` (for example `ggml-base.en.bin`) to caption videos that have no captions of their own. Screen reading uses Apple's Vision framework through a small Swift helper, compiled on first run (needs the Xcode command line tools: `xcode-select --install`).

Windows and Linux: the same steps with your package manager. Screen reading is Mac-only for now; everything else works.

## Run

```bash
python3 server.py
```

Your browser opens http://127.0.0.1:8765. Paste a link, pick a layout, tick the permission box and press **Find clips**. For long streams, open "Long stream? Only scan part of it" and give a From and To time.

There is also a command line:

```bash
python3 kadak.py "https://www.youtube.com/watch?v=..." --count 3 --layout face --section 1:05:00-1:17:00
```

## How it picks moments

Every second of the video gets a score from:

- sudden loudness jumps compared with the last two minutes,
- shouting (energy in the 2–4 kHz band),
- hype words in the captions ("no no no", "let's go", "bhaago"…),
- on-screen words like WASTED or MISSION PASSED (Mac).

The best non-overlapping moments become clips, trimmed to pauses in speech. Each clip says why it was picked. Captions come from the video's own YouTube captions when they exist (Hindi is written in English letters), otherwise from local Whisper.

## Layouts

- **Facecam on top**: finds the streamer's webcam automatically (Mac) and stacks it above the gameplay. Falls back to blurred fill if no webcam is found.
- **Blurred fill**: the whole frame, with a blurred copy behind it.
- **Crop centre**: the middle of the frame, full height.

## Limits

- It downloads the whole video (or the part you choose) at up to 1080p. Use the part picker for multi-hour streams.
- Whisper on a CPU is slow, and the small English model is weak on Hindi. YouTube's own captions are better when present.
- Picking is a heuristic. It finds loud, energetic and on-screen-event moments, not guaranteed viral ones. Watch before you post.

## Tests

```bash
python3 -m unittest test_kadak test_server -v
```

## Credits

Fonts: Anton (SIL OFL 1.1) and Yellowtail (Apache 2.0), included in `fonts/` with their licences.
