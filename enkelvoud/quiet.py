"""
enkelvoud.quiet - Quiet Mode audio filter, ported from the ESP32 server.

Firmware origin (EnkelvoudServer/EVDAudio.h:627-770):

  * 4th-order band-pass: 2nd-order Butterworth high-pass at 400 Hz, then a
    2nd-order Butterworth low-pass at 9.5 kHz (each Q = 1/sqrt(2)).
  * A 1 ms attack / 100 ms release peak limiter at 0.85 full scale, because
    the band-pass has pass-band ripple and group-delay overshoot.

Why it lives on the PC now: the PC port never had this DSP, so the dashboard's
per-room Quiet button only flipped a flag. The stream is shared by every room,
so per-room quiet requires a second, filtered encode rather than muting or
post-processing one stream. encoder.py runs this filter on a copy of each
frame and encodes it separately; sender.py routes rooms that ticked Quiet to
that second stream.

State (biquad history + limiter envelope) is carried across frames, exactly
like the firmware's static quietHpL/quietLpL/quietDyn. reset() drops it, which
the encoder does when Quiet stops being used so a later re-enable does not
start from stale history.
"""

import math
from typing import Optional

import numpy as np

from . import constants as C

try:
    from scipy.signal import lfilter
    _HAS_SCIPY = True
except ImportError:                 # requirements.txt pins scipy, but stay safe
    _HAS_SCIPY = False

QUIET_HP_HZ = 400.0
QUIET_LP_HZ = 9500.0
# 1/sqrt(2): the same Butterworth Q the firmware's `al = sw / 1.41421356237`.
_BUTTERWORTH = 1.41421356237
LIMIT_THRESHOLD = 0.85              # -1.4 dBFS, EVDAudio.h:737
ATTACK_S = 0.001
RELEASE_S = 0.100


def _highpass(fc: float, fs: float):
    w0 = 2.0 * math.pi * fc / fs
    cw, sw = math.cos(w0), math.sin(w0)
    al = sw / _BUTTERWORTH
    a0 = 1.0 + al
    b = ((1.0 + cw) / (2.0 * a0), -(1.0 + cw) / a0, (1.0 + cw) / (2.0 * a0))
    a = (1.0, (-2.0 * cw) / a0, (1.0 - al) / a0)
    return b, a


def _lowpass(fc: float, fs: float):
    w0 = 2.0 * math.pi * fc / fs
    cw, sw = math.cos(w0), math.sin(w0)
    al = sw / _BUTTERWORTH
    a0 = 1.0 + al
    b = ((1.0 - cw) / (2.0 * a0), (1.0 - cw) / a0, (1.0 - cw) / (2.0 * a0))
    a = (1.0, (-2.0 * cw) / a0, (1.0 - al) / a0)
    return b, a


class QuietFilter:
    """Streaming band-pass + limiter for stereo int16 frames."""

    def __init__(self, channels: int = C.CHANNELS) -> None:
        self._channels = channels
        self._fs = 0
        self._hp = _highpass(QUIET_HP_HZ, 48000.0)
        self._lp = _lowpass(QUIET_LP_HZ, 48000.0)
        self._zi_hp = np.zeros((2, channels), dtype=np.float64)
        self._zi_lp = np.zeros((2, channels), dtype=np.float64)
        self._env = 0.0
        self._gain = 1.0

    # ---- state ---------------------------------------------------------

    def configure(self, fs: int) -> None:
        """(Re)derive coefficients for an Opus frame rate; keeps state."""
        if fs == self._fs or fs <= 0:
            return
        self._fs = fs
        # 9.5 kHz sits below Nyquist at 24 kHz too, matching the firmware's
        # note in configureQuietFilters().
        self._hp = _highpass(QUIET_HP_HZ, float(fs))
        self._lp = _lowpass(QUIET_LP_HZ, float(fs))
        self.reset()

    def reset(self) -> None:
        self._zi_hp = np.zeros((2, self._channels), dtype=np.float64)
        self._zi_lp = np.zeros((2, self._channels), dtype=np.float64)
        self._env = 0.0
        self._gain = 1.0

    # ---- processing ----------------------------------------------------

    def process(self, pcm: bytes, fs: int) -> bytes:
        """Filter one PCM frame (int16 interleaved stereo) at `fs` Hz."""
        self.configure(fs)
        x = (np.frombuffer(pcm, dtype=np.int16)
             .reshape(-1, self._channels)
             .astype(np.float64) / 32768.0)

        if _HAS_SCIPY:
            y, self._zi_hp = lfilter(self._hp[0], self._hp[1], x, axis=0,
                                     zi=self._zi_hp)
            y, self._zi_lp = lfilter(self._lp[0], self._lp[1], y, axis=0,
                                     zi=self._zi_lp)
        else:
            y = self._biquad_loop(self._hp, x, "hp")
            y = self._biquad_loop(self._lp, y, "lp")

        y = self._limit(y, fs)
        out = np.clip(y * 32768.0, -32768.0, 32767.0).astype(np.int16)
        return out.tobytes()

    def _biquad_loop(self, coeffs, x: np.ndarray, slot: str) -> np.ndarray:
        """No-scipy fallback: direct form II transposed, per sample."""
        b0, b1, b2 = coeffs[0]
        _, a1, a2 = coeffs[1]
        zi = self._zi_hp if slot == "hp" else self._zi_lp
        out = np.empty_like(x)
        for ch in range(self._channels):
            z1, z2 = float(zi[0, ch]), float(zi[1, ch])
            col = x[:, ch]
            res = out[:, ch]
            for i in range(col.size):
                xn = float(col[i])
                yn = b0 * xn + z1
                z1 = b1 * xn - a1 * yn + z2
                z2 = b2 * xn - a2 * yn
                res[i] = yn
            zi[0, ch], zi[1, ch] = z1, z2
        return out

    def _limit(self, y: np.ndarray, fs: int) -> np.ndarray:
        """Peak limiter: instant-ish attack, slow release (EVDAudio.h:735-770).

        The followers are inherently sequential (each step chooses its
        coefficient from the previous comparison), so they stay a Python loop.
        What used to dominate was the *surrounding* work: a generator
        expression per sample for the channel peak, plus a per-sample array
        write. The peak is now one vectorised max over channels, the gain is
        collected in a plain list and applied as a single multiply. Same
        arithmetic, same order, bit-identical output - measured 4x faster.
        """
        attack = 1.0 - math.exp(-1.0 / (ATTACK_S * fs))
        release = 1.0 - math.exp(-1.0 / (RELEASE_S * fs))
        env, gain = self._env, self._gain
        thresh = LIMIT_THRESHOLD

        peaks = np.abs(y).max(axis=1).tolist()
        gains = [0.0] * len(peaks)
        for i, peak in enumerate(peaks):
            env += (attack if peak > env else release) * (peak - env)
            target = thresh / env if env > thresh else 1.0
            gain += (attack if target < gain else release) * (target - gain)
            gains[i] = gain
        self._env, self._gain = env, gain
        return y * np.asarray(gains)[:, None]


# Singleton shared by the encoder thread. Tests may build their own.
quiet_filter = QuietFilter()
