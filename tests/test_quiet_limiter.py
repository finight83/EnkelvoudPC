"""The vectorised limiter must be bit-identical to the per-sample original.

The original implementation is kept here verbatim as the reference. Both are
driven with the same frames *and the same carried state*, because the
envelope/gain followers are stateful across frames.
"""

import math
import unittest

import numpy as np

from enkelvoud.quiet import (
    ATTACK_S,
    LIMIT_THRESHOLD,
    RELEASE_S,
    QUIET_HP_HZ,
    QUIET_LP_HZ,
    QuietFilter,
    _highpass,
    _lowpass,
)


class ReferenceQuietFilter:
    """Pre-optimisation implementation, for comparison only."""

    def __init__(self, channels: int = 2) -> None:
        self._channels = channels
        self._fs = 0
        self._zi_hp = np.zeros((2, channels), dtype=np.float64)
        self._zi_lp = np.zeros((2, channels), dtype=np.float64)
        self._env = 0.0
        self._gain = 1.0

    def configure(self, fs: int) -> None:
        if fs == self._fs or fs <= 0:
            return
        self._fs = fs
        from scipy.signal import lfilter  # noqa: F401  (reference used scipy)

        self._hp = _highpass(QUIET_HP_HZ, float(fs))
        self._lp = _lowpass(QUIET_LP_HZ, float(fs))
        self._zi_hp = np.zeros((2, self._channels), dtype=np.float64)
        self._zi_lp = np.zeros((2, self._channels), dtype=np.float64)
        self._env = 0.0
        self._gain = 1.0

    def _limit(self, y: np.ndarray, fs: int) -> np.ndarray:
        attack = 1.0 - math.exp(-1.0 / (ATTACK_S * fs))
        release = 1.0 - math.exp(-1.0 / (RELEASE_S * fs))
        env, gain = self._env, self._gain
        out = np.empty_like(y)
        cols = y.shape[1]
        for i in range(y.shape[0]):
            row = y[i]
            peak = max(abs(float(row[c])) for c in range(cols))
            env += (attack if peak > env else release) * (peak - env)
            target = LIMIT_THRESHOLD / env if env > LIMIT_THRESHOLD else 1.0
            gain += (attack if target < gain else release) * (target - gain)
            out[i] = row * gain
        self._env, self._gain = env, gain
        return out

    def process(self, pcm: bytes, fs: int) -> bytes:
        self.configure(fs)
        from scipy.signal import lfilter

        x = (np.frombuffer(pcm, dtype=np.int16)
             .reshape(-1, self._channels)
             .astype(np.float64) / 32768.0)
        y, self._zi_hp = lfilter(self._hp[0], self._hp[1], x, axis=0, zi=self._zi_hp)
        y, self._zi_lp = lfilter(self._lp[0], self._lp[1], y, axis=0, zi=self._zi_lp)
        y = self._limit(y, fs)
        return np.clip(y * 32768.0, -32768.0, 32767.0).astype(np.int16).tobytes()


def _frames(n: int, seed: int, sr: int = 48000, samples: int = 960):
    rng = np.random.default_rng(seed)
    t = np.arange(samples) / sr
    out = []
    for k in range(n):
        # Sweeps loud and quiet so the limiter both engages and releases.
        gain = 0.9 if (k // 7) % 2 == 0 else 0.15
        sig = gain * (0.5 * np.sin(2 * np.pi * (120 + 8 * k) * t)
                      + 0.3 * np.sin(2 * np.pi * 900 * t)
                      + 0.05 * rng.standard_normal(t.size))
        stereo = np.stack([sig, sig * 0.8], axis=1)
        out.append((np.clip(stereo, -1, 1) * 32767).astype(np.int16).tobytes())
    return out


class QuietLimiterEquivalenceTest(unittest.TestCase):
    def test_bit_identical_to_reference_over_320_frames(self):
        for sr in (48000, 24000):
            fast = QuietFilter()
            ref = ReferenceQuietFilter()
            for i, pcm in enumerate(_frames(320, seed=sr + 1, sr=sr)):
                self.assertEqual(
                    fast.process(pcm, sr),
                    ref.process(pcm, sr),
                    msg=f"frame {i} differs at {sr} Hz",
                )

    def test_state_survives_a_reset_the_same_way(self):
        fast, ref = QuietFilter(), ReferenceQuietFilter()
        a = _frames(20, seed=99)
        for pcm in a:
            fast.process(pcm, 48000)
            ref.process(pcm, 48000)
        fast.reset()
        ref._env = 0.0
        ref._gain = 1.0
        ref._zi_hp[:] = 0
        ref._zi_lp[:] = 0
        for pcm in _frames(20, seed=100):
            self.assertEqual(fast.process(pcm, 48000), ref.process(pcm, 48000))

    def test_limiter_actually_engages(self):
        """Guard against a change that silently disables the ceiling."""
        qf = QuietFilter()
        loud = (np.ones((960, 2)) * 32767 * 0.99).astype(np.int16).tobytes()
        peak_in = 0.99
        # Filter removes the DC, so drive it with a loud in-band tone instead.
        t = np.arange(960) / 48000.0
        tone = (0.99 * np.sin(2 * np.pi * 1000 * t))[:, None].repeat(2, axis=1)
        qf.process((tone * 32767).astype(np.int16).tobytes(), 48000)
        for _ in range(10):
            out = qf.process((tone * 32767).astype(np.int16).tobytes(), 48000)
        peak_out = np.abs(np.frombuffer(out, dtype=np.int16)).max() / 32768.0
        self.assertLess(peak_out, peak_in)
        # The 1 ms attack is not instantaneous, so the first few samples of a
        # sudden peak overshoot the 0.85 ceiling before the gain lands.
        self.assertLessEqual(peak_out, LIMIT_THRESHOLD + 0.06)


if __name__ == "__main__":
    unittest.main()
