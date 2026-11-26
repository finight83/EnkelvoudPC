"""10-band equaliser: bypass, boost/cut response, band limits, persistence."""

import json
import os
import shutil
import unittest

import numpy as np

from enkelvoud import constants as C
from enkelvoud import eq as eq_mod
from enkelvoud.eq import Equalizer, clamp_gains
from enkelvoud.state import runtime

BAND_1K = C.EQ_BANDS.index(1000.0)
BAND_16K = C.EQ_BANDS.index(16000.0)
BAND_100HZ = C.EQ_BANDS.index(125.0)


def tone(hz, amp=0.2, frames=960, fs=48000):
    t = np.arange(frames) / fs
    x = (amp * 32767.0 * np.sin(2.0 * np.pi * hz * t)).astype(np.int16)
    return np.stack([x, x], axis=1).tobytes()


def peak(pcm):
    return int(np.abs(np.frombuffer(pcm, dtype=np.int16)).max())


def gains_with(index, db):
    g = [0.0] * len(C.EQ_BANDS)
    g[index] = db
    return g


class EqualizerFilterTest(unittest.TestCase):
    def test_flat_is_bit_exact_bypass(self):
        e = Equalizer()
        pcm = tone(1000.0)
        out = e.process(pcm, 48000, [0.0] * len(C.EQ_BANDS))
        self.assertEqual(out, pcm)

    def test_boost_at_band_center(self):
        e = Equalizer()
        base = peak(tone(1000.0))
        for _ in range(6):                       # settle the biquad
            out = e.process(tone(1000.0), 48000, gains_with(BAND_1K, 12.0))
        ratio = peak(out) / base
        # +12 dB is 4x; allow for filter settling and int16 rounding.
        self.assertGreater(ratio, 3.0)
        self.assertLess(ratio, 4.2)

    def test_cut_at_band_center(self):
        e = Equalizer()
        base = peak(tone(1000.0, amp=0.4))
        for _ in range(6):
            out = e.process(tone(1000.0, amp=0.4), 48000, gains_with(BAND_1K, -12.0))
        ratio = peak(out) / base
        self.assertGreater(ratio, 0.2)
        self.assertLess(ratio, 0.34)

    def test_other_bands_are_left_alone(self):
        e = Equalizer()
        base = peak(tone(125.0, amp=0.4))
        for _ in range(6):
            out = e.process(tone(125.0, amp=0.4), 48000, gains_with(BAND_1K, 12.0))
        ratio = peak(out) / base
        self.assertGreater(ratio, 0.9)
        self.assertLess(ratio, 1.1)

    def test_16k_band_is_skipped_at_24k_rate(self):
        e = Equalizer()
        pcm = tone(6000.0, fs=24000)
        out = e.process(pcm, 24000, gains_with(BAND_16K, 12.0))
        self.assertEqual(out, pcm)               # above Nyquist -> bypassed

    def test_clamp_gains(self):
        g = clamp_gains([99, -99, "x"] + [3.0] * 7)
        self.assertEqual(len(g), len(C.EQ_BANDS))
        self.assertEqual(g[0], eq_mod.MAX_GAIN_DB)
        self.assertEqual(g[1], -eq_mod.MAX_GAIN_DB)
        self.assertEqual(g[2], 0.0)
        self.assertEqual(g[3], 3.0)


class EqualizerStateTest(unittest.TestCase):
    def setUp(self):
        self._saved = (runtime.eq_enabled, list(runtime.eq_gains_db))
        self._dir = os.path.join(os.getcwd(), "_eqtest")
        os.makedirs(self._dir, exist_ok=True)
        self._path = os.path.join(self._dir, "eq.json")
        self._orig_path = eq_mod._eq_path
        eq_mod.set_persistence_path(self._path)
        runtime.eq_enabled = False
        runtime.eq_gains_db = [0.0] * len(C.EQ_BANDS)

    def tearDown(self):
        eq_mod.set_persistence_path(self._orig_path)
        runtime.eq_enabled, gains = self._saved
        runtime.eq_gains_db = gains
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_apply_persists_and_reloads(self):
        gains = gains_with(BAND_1K, 6.0)
        st = eq_mod.apply(enabled=True, gains=gains)
        self.assertTrue(st["enabled"])
        self.assertEqual(st["gains"][BAND_1K], 6.0)
        with open(self._path, encoding="utf-8") as f:
            disk = json.load(f)
        self.assertTrue(disk["enabled"])
        self.assertEqual(disk["gains"][BAND_1K], 6.0)

        runtime.eq_enabled = False
        runtime.eq_gains_db = [0.0] * len(C.EQ_BANDS)
        eq_mod.load_from_disk()
        self.assertTrue(runtime.eq_enabled)
        self.assertEqual(runtime.eq_gains_db[BAND_1K], 6.0)

    def test_apply_clamps_and_reports_bands(self):
        st = eq_mod.apply(gains=[99] * 10)
        self.assertEqual(st["bands"], list(C.EQ_BANDS))
        self.assertTrue(all(g == eq_mod.MAX_GAIN_DB for g in st["gains"]))


if __name__ == "__main__":
    unittest.main()
