# Kadak

Paste a YouTube or Twitch link and get vertical 9:16 clips with word-by-word captions. Kadak finds the loud, wild moments in the video (and on a Mac reads WASTED, BUSTED, FAILED or MISSION PASSED off the screen), cuts them and lets you download MP4s.

Free and open source (MIT). It runs entirely on your computer: no account, no upload, no AI service, no API keys, no tracking, no auto-updates.

## Only clip what you're allowed to

Clip your own videos, or creators who allow it (many streamers run clipping campaigns with their own rules). Kadak asks you to confirm this before every run but can't check it for you.

## Install (Mac)

You need [Homebrew](https://brew.sh). Then, in Terminal:

```bash
brew install python yt-dlp ffmpeg-full
git clone https://github.com/schuligan/kadak.git
cd kadak
```

`ffmpeg-full` matters: the plain `ffmpeg` formula can't burn captions, so Kadak would make clips without them.

Optional, for videos with no captions of their own (Twitch, your own files): local Whisper.

```bash
brew install whisper-cpp
mkdir -p ~/.cache/whisper
curl -L -o ~/.cache/whisper/ggml-base.en.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
```

Screen reading (WASTED, FAILED, the facecam…) uses Apple's Vision framework through a small Swift helper that Kadak builds on its first run. It needs the Xcode command line tools: `xcode-select --install`.

Anything optional that's missing is skipped, and Kadak says so while it works.

Keep yt-dlp current (`brew upgrade yt-dlp`): YouTube and Twitch change often, and an old yt-dlp is the usual reason a download fails.

Windows and Linux: not tested yet. The same tools exist there (Python, yt-dlp, an FFmpeg with libass), but screen reading is Mac-only.

## Run

```bash
python3 server.py
```

Your browser opens http://127.0.0.1:8765. If anything is missing, the Terminal says what and how to install it. Paste a link, pick a layout, tick the permission box and press **Find clips**. For long streams, open "Long stream? Only scan part of it" and give a From and To time.

There is also a command line:

```bash
python3 kadak.py "https://www.youtube.com/watch?v=..." --count 3 --layout face --section 1:05:00-1:17:00
```

## How it picks moments

Every second of the video gets a score from:

- sudden loudness jumps compared with the last two minutes,
- shouting (energy in the 2–4 kHz band),
- hype words in the captions ("no no no", "let's go", "bhaago"…),
- on-screen words like WASTED, FAILED or MISSION PASSED (Mac).

Menus (screens full of text) and cutscenes (black bars top and bottom) are skipped (Mac).

The best non-overlapping moments become clips. Each one starts in the calm just before the action builds and ends soon after the payoff: when things go quiet, or a few seconds into a black or frozen screen. Cuts land in pauses in speech. Each clip says why it was picked, where it is in the original video (like 1:10:58 – 1:11:27) and is titled with a sentence said at its peak. Captions come from the video's own YouTube captions when they exist (Hindi is written in English letters), otherwise from local Whisper.

## Layouts

- **Facecam on top**: finds the streamer's webcam automatically (Mac) and stacks it above the gameplay. Falls back to blurred fill if no webcam is found.
- **Blurred fill**: the whole frame, with a blurred copy behind it.
- **Crop centre**: the middle of the frame, full height.

## Limits

- It downloads the whole video (or the part you choose) at up to 1080p. Use the part picker for multi-hour streams.
- YouTube's own captions are used first (Hindi comes out in English letters). Whisper is the fallback: with the English model (`ggml-base.en.bin`) Kadak leaves captions off on non-English videos rather than invent English. For Hindi Twitch streams or your own files, download the multilingual `ggml-base.bin` instead (same link, without `.en`).
- Every clip's sound is evened out to the loudness reels use (-14 LUFS).
- Picking is a heuristic. It finds loud, energetic and on-screen-event moments, not guaranteed viral ones. It skips menus and letterboxed cutscenes, but not every cutscene has black bars. Watch before you post.
- Tested so far on one Apple Silicon Mac: YouTube streams in English and Hindi, a Twitch VOD, local files, and all three layouts. A 10-minute part takes about 2–11 minutes, mostly downloading.

## Tests

```bash
python3 -m unittest test_kadak test_server -v
```

## Credits

Fonts: Anton (SIL OFL 1.1) and Yellowtail (Apache 2.0), included in `fonts/` with their licences.
