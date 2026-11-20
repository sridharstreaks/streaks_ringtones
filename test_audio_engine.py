"""Run from repo root: python -m unittest discover -s tests -v"""
import unittest
from pathlib import Path
import numpy as np

from audio_engine import (
    SAMPLE_RATE, Settings, apply_ringtone_curve,
    curve_preview, decode_selection, encode_audio, gain_curve,
    inspect_audio, process_audio,
)


class RingtoneTests(unittest.TestCase):
    def test_gentle_curve_monotonic(self):
        s = Settings()
        curve = gain_curve(30 * SAMPLE_RATE, s)
        self.assertAlmostEqual(curve[0], 0.0)
        self.assertGreater(curve[int(10 * SAMPLE_RATE)], curve[int(2 * SAMPLE_RATE)])
        self.assertGreater(curve[int(20 * SAMPLE_RATE)], curve[int(10 * SAMPLE_RATE)])
        self.assertAlmostEqual(float(curve[int(20 * SAMPLE_RATE)]), 1.0, places=3)
        self.assertLessEqual(float(np.max(np.diff(curve[::400]))), 0.06)

    def test_start_independent_of_loudness(self):
        seconds = 3
        x = np.ones((seconds * SAMPLE_RATE, 2), dtype=np.float32) * .2
        s1 = Settings(opening_db=-40, target_lufs=-16)
        s2 = Settings(opening_db=-40, target_lufs=-10)
        y1, g1 = apply_ringtone_curve(x, s1)
        y2, g2 = apply_ringtone_curve(x, s2)
        self.assertTrue(np.array_equal(g1, g2))
        self.assertTrue(np.array_equal(y1, y2))
        self.assertEqual(float(y1[0, 0]), 0.0)

    def test_short_clip_curve(self):
        setting = Settings()
        gain = gain_curve(3 * SAMPLE_RATE, setting)
        self.assertAlmostEqual(float(gain[-1]), 1.0, delta=.0001)
        ts, db = curve_preview(setting, 3.0)
        self.assertAlmostEqual(float(db[-1]), 0.0)

    def test_reducing_opening_retains_full_volume(self):
        x = np.ones((30 * SAMPLE_RATE, 2), dtype=np.float32) * .1
        a, _ = apply_ringtone_curve(x, Settings(opening_db=-20))
        b, _ = apply_ringtone_curve(x, Settings(opening_db=-40))
        self.assertLess(np.mean(np.abs(b[:SAMPLE_RATE])), np.mean(np.abs(a[:SAMPLE_RATE])))
        self.assertAlmostEqual(float(np.mean(np.abs(a[22*SAMPLE_RATE:23*SAMPLE_RATE]))),
                               float(np.mean(np.abs(b[22*SAMPLE_RATE:23*SAMPLE_RATE]))), places=5)

    def test_processing_peak_and_target(self):
        t = np.arange(3 * SAMPLE_RATE) / SAMPLE_RATE
        src = (.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        x = np.column_stack((src, src))
        y, stats = process_audio(x, Settings(duration_seconds=3, cleanup="Off", harshness=0))
        self.assertTrue(np.isfinite(y).all())
        self.assertEqual(y.shape, x.shape)
        self.assertLess(float(np.max(np.abs(y))), 1.0)
        self.assertLess(stats["final_peak_dbfs"], 0.0)

    def test_exports(self):
        t = np.arange(2 * SAMPLE_RATE) / SAMPLE_RATE
        wave = (.1 * np.sin(2*np.pi*440*t)).astype(np.float32)
        x = np.column_stack((wave, wave))
        for ext in ("mp3", "wav", "m4r"):
            data = encode_audio(x, ext)
            self.assertGreater(len(data), 3000)
            if ext == "wav":
                self.assertEqual(data[:4], b"RIFF")


if __name__ == "__main__":
    unittest.main()
