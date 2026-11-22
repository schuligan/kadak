import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import kadak


class TestKadak(unittest.TestCase):
    def test_whisper_sound_notes_are_dropped(self):
        segs = ["(upbeat", "music)", "No", "way", "[laughs]", "bro", "*sighs*", "(Music)", "okay"]
        data = {"transcription": [{"text": t, "offsets": {"from": i * 500, "to": i * 500 + 400}}
                                  for i, t in enumerate(segs)]}
        self.assertEqual([w for _, _, w in kadak.whisper_json_words(data)], ["No", "way", "bro", "okay"])
        unclosed = {"transcription": [{"text": t, "offsets": {"from": i * 500, "to": i * 500 + 400}}
                                      for i, t in enumerate(["(upbeat", "No", "way"])]}
        self.assertEqual([w for _, _, w in kadak.whisper_json_words(unclosed)], ["No", "way"])

    def test_english_whisper_model_is_not_used_on_other_languages(self):
        for model in ("/x/ggml-base.en.bin", "/x/ggml-base.en-q5_1.bin"):
            said, out = [], Path(tempfile.mkdtemp())
            with mock.patch.dict(os.environ, {"KADAK_WHISPER_MODEL": model}), \
                    mock.patch("kadak.shutil.which", return_value="/x/whisper-cli"), \
                    mock.patch("kadak.stream", side_effect=AssertionError("Whisper ran")) as run:
                self.assertEqual(kadak.whisper_words(out / "a.wav", out, lambda *a: said.append(a[2]), "hi"), [])
                run.assert_not_called()
            self.assertIn("'hi'", said[-1])

    def test_multilingual_whisper_model_is_told_the_language(self):
        out = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"KADAK_WHISPER_MODEL": "/x/ggml-base.bin"}), \
                mock.patch("kadak.shutil.which", return_value="/x/whisper-cli"), \
                mock.patch("kadak.stream", return_value=(0, [])) as run:
            kadak.whisper_words(out / "a.wav", out, lambda *a: None, "hi")
            kadak.whisper_words(out / "a.wav", out, lambda *a: None, None)
        cmds = [c.args[0] for c in run.call_args_list]
        self.assertEqual(cmds[0][cmds[0].index("-l") + 1], "hi")
        self.assertEqual(cmds[1][cmds[1].index("-l") + 1], "auto")

    def test_captions_ask_original_language_first_and_skip_refusals(self):
        with mock.patch("kadak.subprocess.run") as run:
            kadak.fetch_captions("https://example.com/v", Path(tempfile.mkdtemp()))
        cmd = run.call_args.args[0]
        self.assertIn("--ignore-errors", cmd)
        self.assertTrue(cmd[cmd.index("--sub-langs") + 1].startswith("en-orig,hi-orig,"))

    def test_clips_get_even_loudness(self):
        with mock.patch("kadak.subprocess.run", return_value=mock.Mock(returncode=0)) as run:
            kadak.render("ffmpeg", "src.mp4", 0, 30, "[0:v]null[v]", ".", "kadak_01.mp4")
        cmd = run.call_args.args[0]
        af = cmd[cmd.index("-af") + 1]
        self.assertTrue(af.startswith("loudnorm") and "I=-14" in af and "TP=-1.5" in af, af)
        self.assertEqual(cmd[cmd.index("-ar") + 1], "48000")

    def test_caption_markup_is_dropped(self):
        words = [kadak.clean_word(t) for t in "] >> Oh my god. [Music] [ __ ] Rose".split()]
        self.assertEqual([w for w in words if w], ["Oh", "my", "god.", "Rose"])

    def test_short_silent_video_gives_no_clip(self):
        self.assertEqual(kadak.pick_moments([0.0] * 10, [], 3, 20, 50), [])
        self.assertEqual(len(kadak.pick_moments([0.0] * 5 + [0.9] * 5, [], 3, 20, 50)), 1)

    def test_face_layout_keeps_portrait_sources_unsquashed(self):
        vf = kadak.video_filter("face", (0, 0, 540, 320), 1080, 1920, None)
        self.assertIn(r"crop=min(iw\,ih*1080/1280):min(ih\,iw*1280/1080)", vf)

    def test_tidy_keeps_clips_and_removes_work_files(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        for n in ("kadak_01.mp4", "source.mp4", "audio.wav", "whisper.json", "subs.en.json3", "kadak_01.ass", "notes.txt"):
            (d / n).write_text("x")
        (d / "frames").mkdir()
        (d / "frames" / "f0001.jpg").write_text("x")
        kadak.tidy(d)
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["kadak_01.mp4", "notes.txt"])

    def test_quiet_intro_is_not_a_loud_moment(self):
        db = [-60.0] * 5 + [-20.0] * 60
        self.assertEqual(kadak.rise(db)[5], 0.0)  # normal talk right after a silent intro
        self.assertGreater(kadak.rise(db + [-5.0])[-1], 10)  # a real jump later still counts

    def test_json3_words_with_section_offset(self):
        data = {"events": [
            {"tStartMs": 0, "dDurationMs": 99999},
            {"tStartMs": 10000, "dDurationMs": 3000, "segs": [{"utf8": "bhaago", "acAsrConf": 0},
                                                              {"utf8": " police", "tOffsetMs": 400}]},
            {"tStartMs": 11000, "aAppend": 1, "segs": [{"utf8": "\n"}]},
            {"tStartMs": 14000, "dDurationMs": 2000, "segs": [{"utf8": "aa gayi"}]},
        ]}
        words = kadak.json3_words(data, offset=5.0)
        self.assertEqual([w for _, _, w in words], ["bhaago", "police", "aa", "gayi"])
        self.assertEqual(words[0][:2], (5.0, 5.4))
        self.assertAlmostEqual(words[1][1], 6.4)  # next word is far away: capped at +1.0 s
        self.assertEqual(words[2][0], 9.0)
        self.assertEqual(words[3][0], 10.0)  # two words in one seg spread over the event

    def test_translit(self):
        out = kadak.translit("पुलिस आ गयी, भागो!")
        self.assertTrue(out.isascii())
        self.assertTrue(out.startswith("pul"))
        self.assertIn("bhago", out)
        self.assertEqual(kadak.translit("भाई क्या हुआ नहीं मर गया"), "bhai kya hua nahin mar gaya")

    def test_hindi_captions_count_as_hype_words(self):
        words = [(i, i + 0.5, kadak.clean_word(t)) for i, t in enumerate("अरे भाई क्या मर गया".split())]
        hype, per = kadak.hype_hits(words, 6)
        self.assertEqual(sorted(w for sec in per for w in sec), ["bhai", "kya", "mar gaya"])

    def test_pick_moments(self):
        scores = [0.0] * 600
        for peak, h in [(100, 1.0), (300, 0.8), (500, 0.5), (200, 0.1)]:
            for i in range(peak - 3, peak + 4):
                scores[i] = h
        picks = kadak.pick_moments(scores, [], 5, 20, 50)
        self.assertEqual(len(picks), 3)  # the 0.1 bump is under the 0.15 floor
        self.assertEqual([round(p[2] / 100) for p in picks], [1, 3, 5])  # best first
        for s, e, p in picks:
            self.assertTrue(20 <= e - s <= 50 and s <= p <= e)
        spans = sorted((s, e) for s, e, _ in picks)
        self.assertTrue(all(a[1] <= b[0] for a, b in zip(spans, spans[1:])))
        self.assertEqual(len(kadak.pick_moments(scores, [], 2, 20, 50)), 2)
        self.assertEqual(kadak.pick_moments([0.5] * 10, [], 3, 20, 50), [(0.0, 10, 0)])

    def test_pick_snaps_to_word_gap(self):
        scores = [0.0] * 200
        scores[100] = 1.0
        words = [(t / 2, t / 2 + 0.4, "w") for t in range(0, 400) if not 150 <= t < 156]  # gap 75.0..78.0
        (s, e, _), = kadak.pick_moments(scores, words, 1, 20, 50)
        self.assertTrue(74.9 <= s <= 78.0, s)

    def test_build_ass(self):
        words = [(12.5, 13.0, "bhaago"), (13.0, 13.4, "police"), (15.0, 15.5, "aa")]
        ass = kadak.build_ass(words, 10.0, 40.0, 1350)
        events = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
        self.assertEqual(len(events), 3)
        self.assertTrue(all("&H8AD9FF&" in e and r"\pos(540,1350)" in e for e in events))
        self.assertIn("0:00:02.50,0:00:03.00", events[0])
        self.assertIn("BHAAGO", events[0])
        self.assertIn("POLICE", events[0])  # same group shown, other word not active
        self.assertNotIn("BHAAGO", events[2])  # gap > 0.6 s starts a new group
        self.assertIn("PlayResY: 1920", ass)

    def test_facecam(self):
        corner = [[[0.82, 0.70, 0.08, 0.14]] + ([[0.4, 0.3, 0.2, 0.3]] if i % 3 else []) for i in range(20)]
        x, y, w, h = kadak.facecam(corner, 1920, 1080)
        self.assertGreater(x + w / 2, 1920 * 0.6)
        self.assertGreater(y + h / 2, 1080 * 0.5)
        self.assertAlmostEqual(w / h, 1080 / 640, delta=0.02)
        self.assertTrue(x >= 0 and y >= 0 and x + w <= 1920 and y + h <= 1080)
        scattered = [[[(i * 0.09) % 0.9, (i * 0.37) % 0.9, 0.06, 0.1]] for i in range(20)]
        self.assertIsNone(kadak.facecam(scattered, 1920, 1080))
        self.assertIsNone(kadak.facecam([], 1920, 1080))

    def test_keyword_hits_dedupe(self):
        frames = [["WASTED"]] * 5 + [[]] * 15 + [["wasted"], ["Mission Passed!"]]
        self.assertEqual(kadak.keyword_hits(frames), [(0.0, "WASTED"), (40.0, "WASTED"), (42.0, "MISSION PASSED")])


if __name__ == "__main__":
    unittest.main()
