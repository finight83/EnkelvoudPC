"""
enkelvoud.eq - 10-band peaking equaliser for the master stream.

Placement: encoder.py runs this on the PCM frame *before* Opus encoding, so
the EQ applies to everything that leaves the server. The Quiet stream filters
the already-EQ'd frame, which is the order a mastering chain would use
(EQ -> band-pass -> limiter).

Bands are the ISO octave centres, +/-12 dB, RBJ peaking biquads with Q=1.41.
Bands whose centre is above the current Opus Nyquist are skipped, so the
16 kHz band simply does nothing at the 24 kHz sample rate.

Performance: 10 biquads x 2 channels via scipy.signal.lfilter on 960-sample
frames is cheap; when every gain is 0 the filter is bypassed entirely and the
frame passes through bit-exact.
"""

from __future__ import annotations

import json
import math
import os
from typing import List, Optional

import numpy as np

from . import constants as C
from .log_ring import log_info, log_warn
from .state import runtime

try:
    from scipy.signal import lfilter
    _HAS_SCIPY = True
except ImportError:                 # requirements.txt pins scipy; stay safe
    _HAS_SCIPY = False

MAX_GAIN_DB = 12.0
# Q for a one-octave peaking section: sqrt(2^1) / (2^1 - 1) = 1.4142.
BAND_Q = 1.41421356237
_EPS = 0.05                         # below this a band is "flat"

# [eq-presets] Named curves, dB per EQ_BANDS entry. The ESP32 server's
# dashboard carries the identical table, so a curve picked on one dashboard
# is the same curve on the other. "custom" is not here: it is whatever the
# operator last dialled in, kept in runtime.eq_custom_db.
EQ_PRESETS: dict = {
    "flat":      [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "pop":       [-1, 2, 4, 5, 3, 0, -1, -1, 1, 2],
    "rock":      [5, 4, 3, 1, -1, -2, -1, 1, 3, 4],
    "jazz":      [3, 2, 1, 2, -1, -1, 0, 1, 2, 3],
    "classical": [4, 3, 2, 1, -1, -1, 0, 2, 3, 4],
    "edm":       [6, 5, 2, 0, -2, 2, 1, 2, 5, 4],
    "vocal":     [-3, -2, -1, 1, 4, 5, 4, 2, -1, -2],
}
EQ_PRESET_NAMES = ["custom"] + list(EQ_PRESETS.keys())


def preset_curve(name: str) -> Optional[List[float]]:
    """The dB curve for a preset name, or None if it is not one."""
    curve = EQ_PRESETS.get((name or "").lower())
    return list(curve) if curve else None


def curve_preset_name(gains: List[float]) -> str:
    """'flat'/'rock'/... when the curve matches a preset exactly, else 'custom'."""
    for name, curve in EQ_PRESETS.items():
        if all(abs((gains[i] if i < len(gains) else 0.0) - curve[i]) < 0.01
               for i in range(len(curve))):
            return name
    return "custom"


def _peaking(fc: float, gain_db: float, fs: float):
    """RBJ peaking EQ coefficients, normalised by a0."""
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * fc / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2.0 * BAND_Q)
    a0 = 1.0 + alpha / A
    return (
        ((1.0 + alpha * A) / a0,
         (-2.0 * cw) / a0,
         (1.0 - alpha * A) / a0),
        (1.0,
         (-2.0 * cw) / a0,
         (1.0 - alpha / A) / a0),
    )


def clamp_gains(gains) -> List[float]:
    out = []
    for i in range(len(C.EQ_BANDS)):
        try:
            g = float(gains[i]) if i < len(gains) else 0.0
        except (TypeError, ValueError, IndexError):
            g = 0.0
        out.append(max(-MAX_GAIN_DB, min(MAX_GAIN_DB, g)))
    return out


class Equalizer:
    def __init__(self, channels: int = C.CHANNELS) -> None:
        self._channels = channels
        self._fs = 0
        self._gains: List[float] = [0.0] * len(C.EQ_BANDS)
        self._coeffs: list = []
        self._zi: list = []
        self._bypass = True

    def configure(self, gains: List[float], fs: int) -> None:
        if fs <= 0:
            return
        gains = clamp_gains(gains)
        if gains == self._gains and fs == self._fs:
            return
        self._gains = gains
        self._fs = fs
        coeffs = []
        nyquist = fs / 2.0
        for fc, gain_db in zip(C.EQ_BANDS, gains):
            if abs(gain_db) < _EPS or fc >= nyquist * 0.9:
                continue
            coeffs.append(_peaking(fc, gain_db, fs))
        self._coeffs = coeffs
        # New coefficients make the old biquad history meaningless; reset it.
        self._zi = [np.zeros((2, self._channels), dtype=np.float64)
                    for _ in coeffs]
        self._bypass = not coeffs

    def process(self, pcm: bytes, fs: int, gains: Optional[List[float]] = None) -> bytes:
        """Filter one int16 interleaved stereo frame at `fs` Hz."""
        if gains is not None:
            self.configure(gains, fs)
        elif fs != self._fs:
            self.configure(self._gains, fs)
        if self._bypass or not self._coeffs:
            return pcm

        x = (np.frombuffer(pcm, dtype=np.int16)
             .reshape(-1, self._channels)
             .astype(np.float64) / 32768.0)
        for i, (b, a) in enumerate(self._coeffs):
            if _HAS_SCIPY:
                x, self._zi[i] = lfilter(b, a, x, axis=0, zi=self._zi[i])
            else:
                x = self._biquad_loop(b, a, x, i)
        out = np.clip(x * 32768.0, -32768.0, 32767.0).astype(np.int16)
        return out.tobytes()

    def _biquad_loop(self, b, a, x: np.ndarray, slot: int) -> np.ndarray:
        b0, b1, b2 = b
        _, a1, a2 = a
        zi = self._zi[slot]
        out = np.empty_like(x)
        for ch in range(self._channels):
            z1, z2 = float(zi[0, ch]), float(zi[1, ch])
            col, res = x[:, ch], out[:, ch]
            for i in range(col.size):
                xn = float(col[i])
                yn = b0 * xn + z1
                z1 = b1 * xn - a1 * yn + z2
                z2 = b2 * xn - a2 * yn
                res[i] = yn
            zi[0, ch], zi[1, ch] = z1, z2
        return out


# ---- Runtime state + persistence -----------------------------------------

equalizer = Equalizer()
_eq_path: Optional[str] = None


def state() -> dict:
    return {
        "ok": True,
        "enabled": bool(runtime.eq_enabled),
        "gains": [round(g, 1) for g in runtime.eq_gains_db],
        "bands": list(C.EQ_BANDS),
        # [eq-presets] The selected curve and the hand-made one that
        # "Custom" restores. Older dashboards ignore both.
        "preset": getattr(runtime, "eq_preset", "custom"),
        "custom": [round(g, 1) for g in getattr(runtime, "eq_custom_db",
                                                runtime.eq_gains_db)],
        "presets": {k: list(v) for k, v in EQ_PRESETS.items()},
        "maxGainDb": MAX_GAIN_DB,
    }


def apply(enabled: Optional[bool] = None,
          gains: Optional[List[float]] = None,
          preset: Optional[str] = None,
          custom: Optional[List[float]] = None) -> dict:
    """Apply an EQ change.

    Three shapes are accepted, matching the ESP32 server's /api/audio/eq:

      * enabled only            -> just switch it on/off
      * preset="rock"           -> load that named curve as the live gains
      * gains=[...]             -> hand-made curve; unless a preset name comes
                                   with it, the selection becomes "custom"
                                   and those gains are saved as the custom
                                   curve, which is what the dashboard expects
                                   when a band is moved off a preset.
    """
    with runtime.eq_lock:
        if enabled is not None:
            runtime.eq_enabled = bool(enabled)
        if preset is not None:
            name = (preset or "").lower()
            if name == "custom":
                runtime.eq_preset = "custom"
                runtime.eq_gains_db = clamp_gains(
                    runtime.eq_custom_db if custom is None else custom)
                if custom is not None:
                    runtime.eq_custom_db = list(runtime.eq_gains_db)
            else:
                curve = preset_curve(name)
                if curve is None:
                    log_warn("EQ: unknown preset '%s'", preset)
                else:
                    runtime.eq_preset = name
                    runtime.eq_gains_db = clamp_gains(curve)
        if custom is not None and preset is None:
            runtime.eq_custom_db = clamp_gains(custom)
        if gains is not None:
            runtime.eq_gains_db = clamp_gains(gains)
            if preset is None:
                # [preset-becomes-custom] A hand-moved curve is the custom one.
                runtime.eq_preset = curve_preset_name(runtime.eq_gains_db)
                if runtime.eq_preset == "custom":
                    runtime.eq_custom_db = list(runtime.eq_gains_db)
        equalizer.configure(runtime.eq_gains_db, runtime.opus_sample_rate)
        save_to_disk()
    log_info("EQ: %s, preset=%s, gains=%s",
             "enabled" if runtime.eq_enabled else "disabled",
             runtime.eq_preset, runtime.eq_gains_db)
    return state()


def set_persistence_path(path: str) -> None:
    global _eq_path
    _eq_path = path


def load_from_disk() -> None:
    if _eq_path is None or not os.path.exists(_eq_path):
        return
    try:
        with open(_eq_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    runtime.eq_enabled = bool(data.get("enabled", False))
    gains = data.get("gains")
    if isinstance(gains, list):
        runtime.eq_gains_db = clamp_gains(gains)
    custom = data.get("custom")
    runtime.eq_custom_db = (clamp_gains(custom) if isinstance(custom, list)
                            else list(runtime.eq_gains_db))
    name = str(data.get("preset", "") or "").lower()
    if name == "custom" or name in EQ_PRESETS:
        runtime.eq_preset = name
    else:
        runtime.eq_preset = curve_preset_name(runtime.eq_gains_db)


def save_to_disk() -> None:
    if _eq_path is None:
        return
    try:
        os.makedirs(os.path.dirname(_eq_path), exist_ok=True)
        tmp = _eq_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"enabled": runtime.eq_enabled,
                       "preset": runtime.eq_preset,
                       "gains": [round(g, 2) for g in runtime.eq_gains_db],
                       "custom": [round(g, 2) for g in runtime.eq_custom_db]}, f,
                      indent=2)
        os.replace(tmp, _eq_path)
    except OSError as e:
        log_warn("EQ: save failed: %s", e)
