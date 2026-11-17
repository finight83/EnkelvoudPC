"""
enkelvoud.capture - WASAPI loopback capture -> 48 kHz stereo int16 -> 20 ms frames.

Design:
  - Primary backend: pyaudiowpatch (pyaudio fork exposing WASAPI loopback on
    Windows). Captures the default output device.
  - On non-Windows, falls back to a plain microphone capture via pyaudio.
    Loopback via soundcard is a documented alternative but is left as a
    future extension to keep the v1 dependency set small.
  - Captured audio is always resampled to 48000 Hz stereo int16 (or 24000 Hz
    if a future change wants to move that boundary). Sample-rate agnostic
    because Windows output devices vary from 44100 to 96000.
  - The resampler is scipy.signal.resample_poly. The capture-side resampler
    has NO bearing on Opus sample rate; the encoder does the 48->24 kHz
    downsample itself if the dashboard selects 24 kHz output.

Threading:
  _run() runs in its own daemon thread. It pushes EncodeFrame onto
  encode_queue. If the queue is full the OLDEST pending frame is dropped
  (see queues.py). It never blocks the read loop.
"""

import threading
from typing import Optional

import numpy as np

from . import constants as C
from .queues import EncodeFrame, encode_queue
from .state import now_ms
from .log_ring import log_info, log_warn, log_error

try:
    import pyaudiowpatch as pyaudio
    _HAS_PYAUDIO = True
    _BACKEND = "pyaudiowpatch"
except ImportError:
    try:
        import pyaudio
        _HAS_PYAUDIO = True
        _BACKEND = "pyaudio"
    except ImportError:
        pyaudio = None
        _HAS_PYAUDIO = False
        _BACKEND = "none"

try:
    from scipy.signal import resample_poly
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False

from math import gcd


# 20 ms @ 48 kHz stereo
_SRC_RATE = 48000
_FRAME_SAMPLES = 960
_FRAME_BYTES = _FRAME_SAMPLES * 2 * 2   # int16 * 2 channels
_CHUNK_FRAMES = 1024                     # WASAPI read granularity


def _resample_ratio(in_rate: int, out_rate: int) -> tuple[int, int]:
    g = gcd(in_rate, out_rate)
    return out_rate // g, in_rate // g


class AudioCapture:
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pa = None
        self._stream = None
        self._device_rate = 0
        self._device_channels = 0
        self._seq = 0
        self._frame_buf = np.zeros((0, 2), dtype=np.int16)
        self._up = 1
        self._down = 1

    def start(self) -> bool:
        if not _HAS_PYAUDIO:
            log_error("Capture: no audio backend available "
                      "(install pyaudiowpatch)")
            return False
        if not _HAS_SCIPY:
            log_warn("Capture: scipy not installed; resampling disabled. "
                     "Capture will only work if the source rate is already 48 kHz.")
        self._thread = threading.Thread(
            target=self._run, name="capture", daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass

    # ---- Thread -------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._open()
        except Exception as e:
            log_error("Capture: failed to open device: %s", e)
            return

        while not self._stop.is_set():
            try:
                raw = self._stream.read(
                    _CHUNK_FRAMES, exception_on_overflow=False,
                )
            except Exception as e:
                log_warn("Capture: read error: %s", e)
                continue

            self._ingest(raw)

    def _open(self) -> None:
        self._pa = pyaudio.PyAudio()

        dev_index = None
        try:
            if _BACKEND == "pyaudiowpatch":
                loopback = self._pa.get_default_wasapi_loopback()
                dev_index = loopback["index"]
                self._device_rate = int(loopback["defaultSampleRate"])
                self._device_channels = int(loopback["maxInputChannels"])
                log_info("Capture: WASAPI loopback device '%s' "
                         "(%d Hz, %d ch)",
                         loopback["name"], self._device_rate,
                         self._device_channels)
            else:
                info = self._pa.get_default_input_device_info()
                dev_index = info["index"]
                self._device_rate = int(info["defaultSampleRate"])
                self._device_channels = int(info["maxInputChannels"])
                log_info("Capture: input device '%s' (%d Hz, %d ch)",
                         info["name"], self._device_rate,
                         self._device_channels)
        except Exception as e:
            self._pa.terminate()
            self._pa = None
            raise RuntimeError(f"no usable input device ({e})") from e

        self._up, self._down = _resample_ratio(self._device_rate, _SRC_RATE)
        log_info("Capture: resample %d -> %d Hz (up=%d down=%d)",
                 self._device_rate, _SRC_RATE, self._up, self._down)

        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=self._device_channels,
            rate=self._device_rate,
            frames_per_buffer=_CHUNK_FRAMES,
            input=True,
            input_device_index=dev_index,
        )

    def _ingest(self, raw: bytes) -> None:
        arr = np.frombuffer(raw, dtype=np.int16)
        if self._device_channels == 0:
            return
        frames = arr.size // self._device_channels
        if frames == 0:
            return
        arr = arr[:frames * self._device_channels].reshape(frames, self._device_channels)

        # ---- Convert to stereo int16 --------------------------------
        if self._device_channels == 1:
            arr = np.repeat(arr, 2, axis=1)
        elif self._device_channels > 2:
            arr = arr[:, :2]

        # ---- Resample to 48 kHz if needed ---------------------------
        if self._device_rate != _SRC_RATE:
            if _HAS_SCIPY:
                arr = resample_poly(arr, self._up, self._down, axis=0)
            else:
                # Linear interpolation fallback (higher distortion, but
                # never blocks the pipeline).
                out_frames = int(round(arr.shape[0] * _SRC_RATE / self._device_rate))
                xs = np.linspace(0, 1, arr.shape[0], endpoint=False, dtype=np.float64)
                ys = np.linspace(0, 1, out_frames, endpoint=False, dtype=np.float64)
                arr = np.stack(
                    [np.interp(ys, xs, arr[:, c]).astype(np.int16) for c in (0, 1)],
                    axis=1,
                )

        arr = arr.astype(np.int16, copy=False)

        # ---- Accumulate and emit 20 ms frames -----------------------
        self._frame_buf = np.concatenate([self._frame_buf, arr], axis=0)
        while self._frame_buf.shape[0] >= _FRAME_SAMPLES:
            frame = self._frame_buf[:_FRAME_SAMPLES]
            self._frame_buf = self._frame_buf[_FRAME_SAMPLES:]
            self._seq += 1
            encode_queue.put(EncodeFrame(
                pcm=frame.tobytes(),
                captured_ms=now_ms() & 0xFFFFFFFF,
                seq=self._seq,
                samples=_FRAME_SAMPLES,
            ))


# Singleton
capture = AudioCapture()