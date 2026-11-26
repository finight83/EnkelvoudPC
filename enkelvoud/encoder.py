"""
enkelvoud.encoder - Opus encode thread, driven by encode_queue -> opus_queue.

Reads EncodeFrame (PCM int16 stereo, 960 samples @ 48 kHz) from encode_queue,
encodes with libopus, and pushes OpusPacket onto opus_queue.

Encoder settings are read live from `runtime.new_*` / `runtime.opus_sample_rate`
every frame. Cheap fields (bitrate, complexity, vbr, vbr_constraint, signal)
are applied via libopus properties. Create-time-only changes (application,
sample_rate) trigger an immediate encoder recreate -- on PC that is fast
enough to be synchronous. This is an INFERRED simplification relative to the
ESP32's pending/drain handshake; see the verification table at the bottom of
this file.

Opus frame size:
  sample_rate == 48000 -> 960 samples per 20 ms, encode directly
  sample_rate == 24000 -> downsample 960 -> 480 with resample_poly, encode
"""

import sys
import threading
from typing import Optional

import numpy as np

from . import constants as C
from . import eq as eq_mod
from .queues import (
    EncodeFrame, OpusPacket, encode_queue, opus_queue, quiet_opus_queue,
)
from .quiet import QuietFilter
from .state import runtime
from .log_ring import log_info, log_warn, log_error

# [opus-auto-provision] `pip install opuslib` is only the ctypes binding; it
# needs a native libopus, which a fresh Windows box does not have, so the
# encoder used to fail on every install. opus_runtime.ensure_opus() returns a
# working binding (opuslib_next with the library inside its wheel, else a
# system-backed opuslib) and installs the bundled one if neither is present.
# Imported lazily: it may run pip, which must not happen at import time.
opuslib = None
_HAS_OPUSLIB = False


def _opus_binding():
    """Resolve (once) and cache a working Opus binding, or None."""
    global opuslib, _HAS_OPUSLIB
    if _HAS_OPUSLIB:
        return opuslib
    try:
        import opus_runtime
        opuslib = opus_runtime.ensure_opus()
    except Exception as e:              # never let this kill the thread
        log_warn("Encoder: Opus provisioning failed (%s)", e)
        opuslib = None
    _HAS_OPUSLIB = opuslib is not None
    if _HAS_OPUSLIB:
        log_info("Encoder: Opus ready (%s)", opuslib.__name__)
    return opuslib

try:
    from scipy.signal import resample_poly as _resample_poly
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


# ---- opuslib integer enum values ----------------------------------------
# These mirror the constants in constants.py; opuslib uses its own names.
_OPUS_APPLICATIONS = {
    C.OPUS_APPLICATION_AUDIO: "audio",
    C.OPUS_APPLICATION_VOIP: "voip",
    C.OPUS_APPLICATION_RESTRICTED_LOWDELAY: "restricted_lowdelay",
}


class OpusEncoderThread:
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._encoder = None            # opuslib.Encoder | None
        self._encoder_rate: int = 0     # rate the current encoder was created at
        self._encoder_app: int = 0      # application the encoder was created with
        # [quiet-stream] Second encoder + the band-pass/limiter state. Only
        # used while at least one room has Quiet Mode on.
        self._qenc = None
        self._q_rate: int = 0
        self._q_app: int = 0
        self._quiet = QuietFilter()
        self._quiet_active = False

    def start(self) -> bool:
        _opus_binding()                 # installs the bundled library if needed
        if not _HAS_OPUSLIB:
            log_error("Encoder: no Opus library available, so audio cannot "
                      "be encoded. Install it with: "
                      "%s -m pip install opuslib-next-bundled",
                      sys.executable)
            return False
        self._thread = threading.Thread(
            target=self._run, name="encoder", daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    # ---- Thread -------------------------------------------------------------

    def _run(self) -> None:
        log_info("Encoder: thread started")
        while not self._stop.is_set():
            frame = encode_queue.get(timeout=0.1)
            if frame is None:
                continue
            try:
                self._ensure_encoder()
                self._apply_cheap_settings()

                if runtime.opus_sample_rate == C.OPUS_SAMPLE_RATE_24K:
                    pcm48 = np.frombuffer(frame.pcm, dtype=np.int16).reshape(-1, 2)
                    if _HAS_SCIPY:
                        pcm24 = _resample_poly(pcm48, 1, 2, axis=0).astype(np.int16)
                    else:
                        pcm24 = pcm48[::2]
                    pcm_bytes = pcm24.tobytes()
                    n_samples = 480
                else:
                    pcm_bytes = frame.pcm
                    n_samples = 960

                # [eq] Master 10-band EQ first, so both the plain stream and
                # the Quiet stream (which filters this same frame) inherit it.
                if runtime.eq_enabled:
                    pcm_bytes = eq_mod.equalizer.process(
                        pcm_bytes, runtime.opus_sample_rate)

                opus_bytes = self._encoder.encode(pcm_bytes, n_samples)
                runtime.current_bitrate = runtime.new_bitrate
                runtime.current_vbr = runtime.new_vbr
                runtime.current_vbr_constraint = runtime.new_vbr_constraint
                runtime.current_signal = runtime.new_signal
                runtime.stat_opus_encoded += 1

                opus_queue.put(OpusPacket(
                    opus=opus_bytes,
                    captured_ms=frame.captured_ms,
                    seq=frame.seq,
                ))

                # ---- Quiet stream: filter this frame, encode it again -----
                # Only while some room actually has Quiet on; rooms are routed
                # to this stream by sender.quiet_sender (see queues.py).
                if runtime.quiet_node_ips():
                    qenc = self._ensure_quiet_encoder()
                    if qenc is not None:
                        quiet_pcm = self._quiet.process(
                            pcm_bytes, runtime.opus_sample_rate)
                        quiet_opus_queue.put(OpusPacket(
                            opus=qenc.encode(quiet_pcm, n_samples),
                            captured_ms=frame.captured_ms,
                            seq=frame.seq,
                        ))
                    self._quiet_active = True
                elif self._quiet_active:
                    # Drop stale filter/limiter history so a later re-enable
                    # does not start mid-envelope.
                    self._quiet.reset()
                    self._quiet_active = False
            except Exception as e:
                log_warn("Encoder: frame failed: %s", e)

    # ---- Encoder lifecycle --------------------------------------------------

    def _ensure_encoder(self) -> None:
        # Recreate if application changed OR sample rate changed.
        need = (
            self._encoder is None
            or self._encoder_app != runtime.new_application
            or self._encoder_rate != runtime.opus_sample_rate
        )
        if need:
            self._recreate()

    def _recreate(self) -> None:
        if self._encoder is not None:
            try:
                self._encoder.destroy()
            except Exception:
                pass

        _opus_binding()
        rate = runtime.opus_sample_rate
        app = runtime.new_application
        try:
            self._encoder = opuslib.Encoder(
                rate, C.CHANNELS, _OPUS_APPLICATIONS[app],
            )
        except KeyError:
            log_error("Encoder: unknown application %d; falling back to audio", app)
            self._encoder = opuslib.Encoder(rate, C.CHANNELS, "audio")
            app = C.OPUS_APPLICATION_AUDIO

        self._encoder_rate = rate
        self._encoder_app = app
        runtime.current_application = app
        with runtime.mode_settings_lock:
            runtime.current_complexity = runtime.mode_settings[runtime.playback_mode_idx].complexity
        self._apply_all_settings()

        log_info("Encoder: created rate=%d app=%d", rate, app)

    def _ensure_quiet_encoder(self):
        """Second encoder for the filtered stream, created/reconfigured lazily."""
        need = (
            self._qenc is None
            or self._q_rate != runtime.opus_sample_rate
            or self._q_app != runtime.new_application
        )
        if need:
            if self._qenc is not None:
                try:
                    self._qenc.destroy()
                except Exception:
                    pass
                self._qenc = None
            rate = runtime.opus_sample_rate
            app = runtime.new_application
            try:
                self._qenc = opuslib.Encoder(
                    rate, C.CHANNELS, _OPUS_APPLICATIONS[app])
            except KeyError:
                self._qenc = opuslib.Encoder(rate, C.CHANNELS, "audio")
                app = C.OPUS_APPLICATION_AUDIO
            self._q_rate = rate
            self._q_app = app
            log_info("Encoder: quiet stream created rate=%d app=%d", rate, app)

        e = self._qenc
        if e is None:
            return None
        # Mirror the live settings the main encoder is running with. The
        # one-shot pending_complexity was already consumed by the main path,
        # so use its effective value.
        e.bitrate = int(runtime.new_bitrate)
        e.vbr = bool(runtime.new_vbr)
        e.vbr_constraint = bool(runtime.new_vbr_constraint)
        e.signal = int(runtime.new_signal)
        e.complexity = int(runtime.current_complexity)
        return e

    def _apply_all_settings(self) -> None:
        e = self._encoder
        if e is None:
            return
        e.bitrate = int(runtime.new_bitrate)
        with runtime.mode_settings_lock:
            e.complexity = int(runtime.mode_settings[runtime.playback_mode_idx].complexity)
        e.vbr = bool(runtime.new_vbr)
        e.vbr_constraint = bool(runtime.new_vbr_constraint)
        e.signal = int(runtime.new_signal)

    def _apply_cheap_settings(self) -> None:
        e = self._encoder
        if e is None:
            return
        if runtime.current_bitrate != runtime.new_bitrate:
            e.bitrate = int(runtime.new_bitrate)
        if runtime.current_vbr != runtime.new_vbr:
            e.vbr = bool(runtime.new_vbr)
        if runtime.current_vbr_constraint != runtime.new_vbr_constraint:
            e.vbr_constraint = bool(runtime.new_vbr_constraint)
        if runtime.current_signal != runtime.new_signal:
            e.signal = int(runtime.new_signal)
        # [pending-complexity] A one-shot override from /api/audio/encoding
        # (or a mode switch via apply_mode_settings_to_live) wins over the
        # mode preset for exactly one frame. Otherwise fall back to the
        # currently selected mode preset.
        if runtime.pending_complexity is not None:
            target_cx = int(runtime.pending_complexity)
            runtime.pending_complexity = None
        else:
            with runtime.mode_settings_lock:
                target_cx = int(runtime.mode_settings[runtime.playback_mode_idx].complexity)
        if runtime.current_complexity != target_cx:
            e.complexity = target_cx
            runtime.current_complexity = target_cx


# Singleton
encoder = OpusEncoderThread()