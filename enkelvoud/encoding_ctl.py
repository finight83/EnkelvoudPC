"""
enkelvoud.encoding_ctl - encoder preset persistence and /api/audio/encoding
                          business logic.

Mirrors EVDPlayers.h handleGetAudioEncoding / handlePostAudioEncoding /
handlePostAudioEncodingRevert / handlePostAudioEncodingDefaults.

Persistence model (equivalent to EVDWiFi.h's NVS keys enc_<mode>_{br,cx,vbr,
vc,sig,app,sr,buf} plus play_mode):

  mode_settings[i]  == what's saved to disk for mode i (the preset)
  runtime.new_*     == the INTENDED live settings (may differ pre-Save)
  runtime.current_* == what the encoder thread actually pushed into libopus
  runtime.pending_complexity == a one-shot complexity override

The Save button copies capture_live_into_mode(mode) to disk. Revert and
Defaults both re-invoke apply_mode_settings_to_live(mode) with different
sources for mode_settings[mode].
"""

import json
import os
from typing import Optional, Tuple

from . import constants as C
from .state import (
    runtime, EncodeSettings, _clamp_encode_settings,
)


# ---- Live <-> preset synchronization (EVDWiFi.h) -------------------------

def apply_mode_settings_to_live(mode_idx: int) -> None:
    """Copy mode_settings[mode_idx] into the live runtime variables."""
    with runtime.mode_settings_lock:
        s = runtime.mode_settings[mode_idx]

    runtime.new_bitrate = s.bitrate
    runtime.pending_complexity = s.complexity
    runtime.new_vbr = s.vbr
    runtime.new_vbr_constraint = s.vbr_constraint
    runtime.new_signal = s.signal
    runtime.new_application = s.application
    runtime.audio_buffer_ms = s.buffer_ms

    if s.sample_rate != runtime.opus_sample_rate:
        runtime.opus_sample_rate = s.sample_rate
        runtime.opus_frame_samples = (
            s.sample_rate // (1000 // C.OPUS_FRAME_MS)
        )


def capture_live_into_mode(mode_idx: int) -> None:
    """Copy live runtime variables into mode_settings[mode_idx] (in RAM)."""
    with runtime.mode_settings_lock:
        s = runtime.mode_settings[mode_idx]
        s.bitrate = runtime.new_bitrate
        s.complexity = (
            runtime.pending_complexity
            if runtime.pending_complexity is not None
            else runtime.current_complexity
        )
        s.vbr = runtime.new_vbr
        s.vbr_constraint = runtime.new_vbr_constraint
        s.signal = runtime.new_signal
        s.application = runtime.new_application
        s.buffer_ms = runtime.audio_buffer_ms
        s.sample_rate = runtime.opus_sample_rate


# ---- GET /api/audio/encoding ---------------------------------------------

def get_encoding_state() -> dict:
    """Return the JSON body for GET /api/audio/encoding (EVDPlayers.h)."""
    signal_str = {
        C.OPUS_SIGNAL_MUSIC: "music",
        C.OPUS_SIGNAL_VOICE: "voice",
        C.OPUS_AUTO: "auto",
    }.get(runtime.new_signal, "auto")

    app_str = {
        C.OPUS_APPLICATION_AUDIO: "audio",
        C.OPUS_APPLICATION_VOIP: "voip",
        C.OPUS_APPLICATION_RESTRICTED_LOWDELAY: "lowdelay",
    }.get(runtime.new_application, "audio")

    # [encoder-get-intended-state] - EVDPlayers.h: effective complexity
    # is the pending value if set, else the current one.
    effective_complexity = (
        runtime.pending_complexity
        if runtime.pending_complexity is not None
        else runtime.current_complexity
    )

    return {
        "ok": True,
        "mode": runtime.playback_mode_cstr(),
        "bitrate": runtime.new_bitrate,
        "complexity": effective_complexity,
        "vbr": runtime.new_vbr,
        "vbrConstraint": runtime.new_vbr_constraint,
        "bufferMs": runtime.audio_buffer_ms,
        "sampleRate": runtime.opus_sample_rate,
        "signal": signal_str,
        "application": app_str,
    }


# ---- POST /api/audio/encoding --------------------------------------------

def apply_encoding_post(data: dict, save: bool) -> dict:
    """Return the JSON body for POST /api/audio/encoding (EVDPlayers.h)."""
    # ---- bitrate (6000..510000, clamped to 192000 for packet cap) ------
    if isinstance(data.get("bitrate"), int):
        b = int(data["bitrate"])
        if 6000 <= b <= 510000:
            if b > 192000:
                b = 192000
            runtime.new_bitrate = b

    # ---- complexity (0..10) -------------------------------------------
    if isinstance(data.get("complexity"), int):
        c = int(data["complexity"])
        if 0 <= c <= 10:
            runtime.pending_complexity = c

    # ---- vbr / vbrConstraint ------------------------------------------
    if isinstance(data.get("vbr"), bool):
        runtime.new_vbr = bool(data["vbr"])
    if isinstance(data.get("vbrConstraint"), bool):
        runtime.new_vbr_constraint = bool(data["vbrConstraint"])

    # ---- signal -------------------------------------------------------
    if isinstance(data.get("signal"), str):
        s = data["signal"]
        if s == "music":
            runtime.new_signal = C.OPUS_SIGNAL_MUSIC
        elif s == "voice":
            runtime.new_signal = C.OPUS_SIGNAL_VOICE
        elif s == "auto":
            runtime.new_signal = C.OPUS_AUTO

    # ---- application --------------------------------------------------
    if isinstance(data.get("application"), str):
        a = data["application"]
        if a == "audio":
            runtime.new_application = C.OPUS_APPLICATION_AUDIO
        elif a == "voip":
            runtime.new_application = C.OPUS_APPLICATION_VOIP
        elif a == "lowdelay":
            runtime.new_application = C.OPUS_APPLICATION_RESTRICTED_LOWDELAY

    # ---- bufferMs -----------------------------------------------------
    if isinstance(data.get("bufferMs"), int):
        bm = int(data["bufferMs"])
        if C.AUDIO_BUFFER_MIN_MS <= bm <= C.AUDIO_BUFFER_MAX_MS:
            runtime.audio_buffer_ms = bm

    # ---- sampleRate ---------------------------------------------------
    if isinstance(data.get("sampleRate"), int):
        sr = int(data["sampleRate"])
        if sr in (C.OPUS_SAMPLE_RATE_48K, C.OPUS_SAMPLE_RATE_24K):
            runtime.opus_sample_rate = sr
            runtime.opus_frame_samples = sr // (1000 // C.OPUS_FRAME_MS)

    # ---- Save (capture live into mode preset + persist) --------------
    if save:
        capture_live_into_mode(runtime.playback_mode_idx)
        save_encoders_to_disk()

    # [api-audio-encoding-note-verbatim] - EVDPlayers.h handlePostAudioEncoding
    # The note string is retained verbatim even though PC does not require a
    # reboot for application-mode changes. It is part of the API contract.
    return {
        "ok": True,
        "saved": save,
        "mode": runtime.playback_mode_cstr(),
        "note": (
            "Application mode requires reboot to take effect; "
            "sample rate change re-creates encoder on next audio frame"
        ),
    }


# ---- POST /api/audio/encoding/revert -------------------------------------

def revert_encoding() -> dict:
    """Reload current mode's preset from in-memory persisted state."""
    apply_mode_settings_to_live(runtime.playback_mode_idx)
    return {"ok": True, "mode": runtime.playback_mode_cstr()}


# ---- POST /api/audio/encoding/defaults -----------------------------------

def apply_defaults() -> dict:
    """Load factory defaults for the current mode."""
    idx = runtime.playback_mode_idx
    with runtime.mode_settings_lock:
        d = C.MODE_ENCODE_DEFAULTS[idx]
        runtime.mode_settings[idx] = _clamp_encode_settings(
            EncodeSettings.from_dict(d)
        )
    apply_mode_settings_to_live(idx)
    return {"ok": True, "mode": runtime.playback_mode_cstr()}


# ---- Disk persistence ----------------------------------------------------

_encoders_path: Optional[str] = None


def set_persistence_path(path: str) -> None:
    global _encoders_path
    _encoders_path = path


def load_encoders_from_disk() -> None:
    """Called at startup, before any threads spawn."""
    if _encoders_path is None or not os.path.exists(_encoders_path):
        return
    try:
        with open(_encoders_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        modes = data.get("modes", {})
        for idx, name in enumerate(C.MODE_NAMES):
            if name in modes:
                with runtime.mode_settings_lock:
                    runtime.mode_settings[idx] = _clamp_encode_settings(
                        EncodeSettings.from_dict(modes[name])
                    )
        play_mode = data.get("play_mode", "music")
        if play_mode in C.MODE_NAMES:
            runtime.playback_mode_idx = C.MODE_NAMES.index(play_mode)
    except (OSError, json.JSONDecodeError, KeyError):
        pass


def save_encoders_to_disk() -> None:
    if _encoders_path is None:
        return
    try:
        os.makedirs(os.path.dirname(_encoders_path), exist_ok=True)
        with runtime.mode_settings_lock:
            data = {
                "play_mode": runtime.playback_mode_cstr(),
                "modes": {
                    name: runtime.mode_settings[i].to_dict()
                    for i, name in enumerate(C.MODE_NAMES)
                },
            }
        tmp = _encoders_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _encoders_path)
    except OSError:
        pass