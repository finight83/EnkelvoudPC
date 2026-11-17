"""
enkelvoud.web_api - Flask application for every /api/* route.

Route table verified against EVDRoutes.h. Every JSON response shape is
copied verbatim from the corresponding C++ handler.

PC stubs:
  - /api/ota/* (except plan/status) -> 503 {"ok":false,"error":"OTA not supported on PC server"}
  - /api/source                       -> 200 {"ok":true,"source":"<s>","transport":"pc"}
  - /api/cmd                          -> 503 {"ok":false,"error":"source board not present on PC"}
  - /api/bt/devices                   -> {"devices":[]}
  - /api/bt/kick, /api/bt/remove      -> 200 {"ok":true} (no-op)
  - /api/source/info                  -> full shape with empty data
  - /api/network/state, /save,
    /api/network-test, /api/wifi/scan  -> PC-shaped stubs
  - /api/restart                      -> {"ok":true}, no restart unless --allow-restart
  - /api/dlna                         -> reflects --dlna flag
"""

import json
import os
from typing import Optional

from flask import Flask, Response, request

from . import constants as C
from . import ctrl
from . import discovery
from . import encoding_ctl
from . import registry
from . import zone_ctl
from .log_ring import log_ring, log_info, log_warn, log_error
from .state import (
    runtime, now_ms, sanitize_server_name, evaluate_audio_compatibility,
    _clamp_encode_settings, EncodeSettings,
)


# ---- Helpers ------------------------------------------------------------

def _json(data: dict, status: int = 200) -> Response:
    return Response(
        json.dumps(data, ensure_ascii=False),
        status=status,
        mimetype="application/json",
    )


def _try_parse_json() -> Optional[dict]:
    try:
        return request.get_json(silent=False, force=True)
    except Exception:
        return None


def _reject_empty_body() -> Optional[Response]:
    """Mirror rejectEmptyBodyPost() for 5-arg routes."""
    if request.content_length is None or request.content_length == 0:
        return _json({"ok": False, "error": "empty body"}, 400)
    return None


def _sig_str(sig: int) -> str:
    return {C.OPUS_SIGNAL_MUSIC: "music",
            C.OPUS_SIGNAL_VOICE: "voice",
            C.OPUS_AUTO: "auto"}.get(sig, "auto")


def _app_str(app: int) -> str:
    return {C.OPUS_APPLICATION_AUDIO: "audio",
            C.OPUS_APPLICATION_VOIP: "voip",
            C.OPUS_APPLICATION_RESTRICTED_LOWDELAY: "lowdelay"}.get(app, "audio")


# ---- /api/state ---------------------------------------------------------

def _build_state_response() -> dict:
    players = runtime.snapshot_players(now_ms())
    zones = runtime.snapshot_zones()

    # Inject synthetic main zone if the operator deleted it somehow
    # (parity with handleApiState).
    if players:
        has_main = any(z.id == C.MAIN_ZONE_ID for z in zones)
        if not has_main:
            from .state import Zone
            zones.insert(0, Zone(id=C.MAIN_ZONE_ID, name="Main Room",
                                 volume=100, muted=False, node_ips=[]))
        # Add unassigned players to main (response only, not persisted).
        for p in players:
            if any(p.ip in z.node_ips for z in zones):
                continue
            for z in zones:
                if z.id == C.MAIN_ZONE_ID:
                    z.node_ips.append(p.ip)
                    break

    # [sourceNominalRate] I2S/USB paths = 48k nominal.
    source_nominal_rate = C.SOURCE_SAMPLE_RATE

    # [pc-heap-stats] Honest values from psutil if available, else zeros.
    try:
        import psutil
        vm = psutil.virtual_memory()
        heap_used_pct = int(vm.percent)
        heap_free_bytes = int(vm.available)
        heap_total_bytes = int(vm.total)
        heap_max_alloc = int(vm.available)
    except Exception:
        heap_used_pct = 0
        heap_free_bytes = 0
        heap_total_bytes = 0
        heap_max_alloc = 0

    zone_json = []
    for z in zones:
        node_entries = []
        for ip in z.node_ips:
            matched = next((p for p in players if p.ip == ip), None)
            if matched is not None:
                node_entries.append({
                    "name": matched.name,
                    "ip": ip,
                    "version": matched.version,
                    "known": True,
                })
            else:
                node_entries.append({
                    "name": ip,
                    "ip": ip,
                    "version": "",
                    "known": False,
                })
        zone_json.append({
            "id": z.id, "name": z.name, "volume": z.volume,
            "muted": z.muted, "nodes": node_entries,
        })

    return {
        "ok": True,
        "ip": discovery.get_local_ip(),
        "serverName": runtime.server_name,
        "masterMuted": runtime.master_muted,
        "quietMode": runtime.quiet_mode,
        "cpuCore0Pct": -1,
        "cpuCore1Pct": -1,
        "heapUsedPct": heap_used_pct,
        "heapFreeBytes": heap_free_bytes,
        "heapTotalBytes": heap_total_bytes,
        "heapMaxAlloc": heap_max_alloc,
        "psramUsedPct": 0,
        "psramFreeBytes": 0,
        "psramTotalBytes": 0,
        "masterVolume": runtime.master_volume,
        "potVolume": runtime.pot_volume,
        "latencyAdjustmentMs": runtime.latency_adjustment_ms,
        "audioBufferMs": runtime.audio_buffer_ms,
        "postResampleRate": C.SOURCE_SAMPLE_RATE,
        "sourceSampleRate": C.SOURCE_SAMPLE_RATE,
        "sourceNominalRate": source_nominal_rate,
        "opusSampleRate": runtime.opus_sample_rate,
        "mode": runtime.playback_mode_cstr(),
        "audioProtocolVersion": C.AUDIO_PROTO_VERSION,
        "serverEpoch": runtime.server_epoch & 0xFFFFFFFF,
        "receiverAudio": {"source": runtime.receiver_source},
        "players": [
            {
                "name": p.name, "ip": p.ip, "version": p.version,
                "declaredAudioProtocolVersion": p.declared_audio_protocol_version,
                "negotiatedAudioProtocolVersion": p.negotiated_audio_protocol_version,
                "audioCompatible": p.audio_compatible,
                "audioCompatReason": p.audio_compat_reason,
            }
            for p in players
        ],
        "zones": zone_json,
    }


# ---- /api/stats ---------------------------------------------------------

def _build_stats_response() -> dict:
    avg_latency = (
        runtime.stat_latency_sum_ms / runtime.stat_latency_count
        if runtime.stat_latency_count > 0 else 0.0
    )
    perf = runtime.snapshot_perf()
    total_rebuffers = sum(p.rebuffers for p in perf)

    try:
        from .queues import encode_queue, opus_queue
        raw_qd = encode_queue.qsize()
        opus_qd = opus_queue.qsize()
    except Exception:
        raw_qd = 0
        opus_qd = 0

    # OPUS_FRAME_MS = 20 -> samples/sec / 50
    opus_frame_samples = runtime.opus_sample_rate // (1000 // C.OPUS_FRAME_MS)

    return {
        "ok": True,
        "ip": discovery.get_local_ip(),
        "wifiConnected": True,
        "wifiRssi": 0,
        "wifiSsid": "",
        "wifiDisconnects": 0,
        "wsClients": runtime.subscriber_count,
        "mode": runtime.playback_mode_cstr(),
        "audioBufferMs": runtime.audio_buffer_ms,
        "postResampleRate": C.SOURCE_SAMPLE_RATE,
        "sourceSampleRate": C.SOURCE_SAMPLE_RATE,
        "sourceNominalRate": C.SOURCE_SAMPLE_RATE,
        "opusSampleRate": runtime.opus_sample_rate,
        "opusFrameSamples": opus_frame_samples,
        "latencyAdjustmentMs": runtime.latency_adjustment_ms,
        "masterVolume": runtime.master_volume,
        "potVolume": runtime.pot_volume,
        "masterMuted": runtime.master_muted,
        "i2sReadErrors": 0,
        "udpErrors": runtime.stat_udp_errors,
        "avgEncodeUs": 0,
        "encodeOverrunsLastSec": 0,
        "encodeOverrunsTotal": 0,
        "encodePeakUsLastSec": 0,
        "pendingBufDroppedLastSec": 0,
        "pendingBufDroppedTotal": 0,
        "pendingBufPeakDepthLastSec": 0,
        "measuredInputHz": float(C.SOURCE_SAMPLE_RATE),
        "clockDriftPpm": 0,
        "audioProtocolVersion": C.AUDIO_PROTO_VERSION,
        "serverEpoch": runtime.server_epoch & 0xFFFFFFFF,
        "clockSyncPort": C.CLOCK_SYNC_UDP_PORT,
        "syncRequestsServed": runtime.stat_sync_requests_served,
        "syncRequestErrors": runtime.stat_sync_request_errors,
        "syncRequestsThrottled": runtime.stat_sync_requests_throttled,
        "audioIncompatibleSkips": runtime.stat_audio_incompatible_skips,
        "audioProvisionalSkips": runtime.stat_audio_provisional_skips,
        "audioSyncPending": runtime.stat_audio_sync_pending,
        "usbBytesLastWin": 0,
        "serverInputSource": "USB",
        "streaming": {
            "i2sBytesLast5s": 0,
            "rawDroppedLast5s": 0,
            "opusDroppedLast5s": runtime.stat_opus_dropped_last_5s,
        },
        "rawQueueDepth": raw_qd,
        "rawQueueLen": C.ENCODE_QUEUE_LEN,
        "opusQueueDepth": opus_qd,
        "opusQueueLen": C.OPUS_QUEUE_LEN,
        "opusEncodedLastWin": runtime.stat_opus_encoded_last_5s,
        "opusDroppedLastWin": runtime.stat_opus_dropped_last_5s,
        "wsSentLastWin": runtime.stat_udp_packets_last_5s,
        "wsBytesLastWin": runtime.stat_udp_bytes_last_5s,
        "avgLatencyMs": round(avg_latency, 2),
        "nodes": [
            {
                "ip": p.ip, "latencyMs": p.latency_ms,
                "queueDepth": p.queue_depth, "target": p.target,
                "rebuffers": p.rebuffers,
            }
            for p in perf
        ],
        "rebuffers": total_rebuffers,
        "sourceBoard": {
            "fw": "", "link": False, "btConnected": False,
            "mac": "", "volume": 0, "avrc": False,
            "activeSource": "", "loopTicks": 0,
            "heapFree": 0, "uptimeSec": 0,
            "a2dpConnecting": 0, "a2dpConnected": 0,
            "a2dpDisconnecting": 0, "a2dpDisconnected": 0,
            "volumeEvents": 0, "volumeApplied": 0,
            "uartBytes": 0, "uartLines": 0,
            "uartOverflow": 0, "uartTxDropped": 0,
            "sourceChanges": 0, "heartbeatAgeMs": 0,
        },
        "udpSendLoopCount": runtime.udp_send_loop_count,
        "udpSendStallResets": runtime.udp_send_stall_resets,
        "udpPacerAheadReset": runtime.udp_pacer_ahead_reset,
        "udpPacerBehindReset": runtime.udp_pacer_behind_reset,
        "udpPacerDriftReset": runtime.udp_pacer_drift_reset,
        "udpStageAfterRecv": runtime.udp_stage_after_recv,
        "udpStageAfterStore": runtime.udp_stage_after_store,
        "otaLastProgressAgeMs": 0xFFFFFFFF,
        "receiverAudio": {"state": runtime.receiver_state},
    }


# ---- Flask app factory --------------------------------------------------

def create_app(web_dir: str, allow_restart: bool = False,
               allow_ota_success: bool = False) -> Flask:
    app = Flask("enkelvoud")

    dashboard_path = os.path.join(web_dir, "dashboard.html")

    # ---- Dashboard -----------------------------------------------------
    @app.route("/", methods=["GET"])
    def dashboard():
        if os.path.exists(dashboard_path):
            with open(dashboard_path, "rb") as f:
                body = f.read()
            return Response(body, status=200,
                            mimetype="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-store, max-age=0",
                                     "Pragma": "no-cache"})
        # Placeholder when the user hasn't dropped the dashboard yet.
        placeholder = (
            "<!doctype html><html><head><title>Enkelvoud</title></head>"
            "<body style='font-family: sans-serif; max-width: 720px; margin: 2em auto;'>"
            "<h1>Enkelvoud PC server</h1>"
            "<p>The dashboard HTML is not present.</p>"
            "<p>Place the un-gzipped contents of <code>EVDCTRL.h</code> "
            "(everything between <code>R\"HTML(</code> and <code>)HTML\"</code>) "
            "at <code>web/dashboard.html</code> and reload.</p>"
            "<p>Endpoints that already work: "
            "<code>/api/state</code>, <code>/api/stats</code>, "
            "<code>/api/fw</code>, <code>/api/log/tail</code>, "
            "all node-facing routes.</p>"
            "</body></html>"
        )
        return Response(placeholder, status=200,
                        mimetype="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store, max-age=0"})

    # ---- /api/fw -------------------------------------------------------
    @app.route("/api/fw", methods=["GET"])
    def api_fw():
        return _json({"version": C.FW_VERSION})

    # ---- /api/state ----------------------------------------------------
    @app.route("/api/state", methods=["GET"])
    def api_state():
        body = _build_state_response()
        return Response(
            json.dumps(body, ensure_ascii=False), status=200,
            mimetype="application/json",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    # ---- /api/stats ----------------------------------------------------
    @app.route("/api/stats", methods=["GET"])
    def api_stats():
        body = _build_stats_response()
        return Response(
            json.dumps(body, ensure_ascii=False), status=200,
            mimetype="application/json",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    # ---- /api/discover -------------------------------------------------
    @app.route("/api/discover", methods=["GET"])
    def api_discover():
        safe_name = runtime.server_name
        return _json({
            "name": safe_name,
            "ip": discovery.get_local_ip(),
            "hostname": f"{safe_name}.local",
        })

    # ---- /api/log/level (GET/POST) ------------------------------------
    @app.route("/api/log/level", methods=["GET"])
    def api_log_level_get():
        return _json({"ok": True, "level": runtime.log_level})

    @app.route("/api/log/level", methods=["POST"])
    def api_log_level_post():
        empty = _reject_empty_body()
        if empty is not None:
            return empty
        data = _try_parse_json()
        if not isinstance(data, dict) or not isinstance(data.get("level"), int):
            return _json({"ok": False}, 400)
        v = max(0, min(3, int(data["level"])))
        runtime.log_level = v
        return _json({"ok": True, "level": v})

    # ---- /api/log/tail -------------------------------------------------
    @app.route("/api/log/tail", methods=["GET"])
    def api_log_tail():
        try:
            since = int(request.args.get("since", "0"))
        except ValueError:
            since = 0
        entries, next_seq = log_ring.tail(since)
        return Response(
            json.dumps({"ok": True, "lines": entries, "nextSeq": next_seq},
                       ensure_ascii=False),
            status=200, mimetype="application/json",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    # ---- /api/dlna (GET/POST) -----------------------------------------
    @app.route("/api/dlna", methods=["GET"])
    def api_dlna_get():
        return _json({"ok": True, "enabled": runtime.dlna_enabled})

    @app.route("/api/dlna", methods=["POST"])
    def api_dlna_post():
        empty = _reject_empty_body()
        if empty is not None:
            return empty
        data = _try_parse_json()
        if not isinstance(data, dict) or not isinstance(data.get("enabled"), bool):
            return _json({"ok": False}, 400)
        runtime.dlna_enabled = bool(data["enabled"])
        log_info("DLNA flag set to %s (stub, no SSDP/renderer implemented)",
                 runtime.dlna_enabled)
        return _json({"ok": True, "enabled": runtime.dlna_enabled})

    # ---- /api/player/register -----------------------------------------
    @app.route("/api/player/register", methods=["POST"])
    def api_player_register():
        empty = _reject_empty_body()
        if empty is not None:
            return empty

        ip = request.remote_addr or "0.0.0.0"
        p_name = "Player Node"
        p_ver = ""
        declared_proto = -1

        data = _try_parse_json()
        if isinstance(data, dict):
            if isinstance(data.get("name"), str):
                raw = data["name"]
                safe = "".join(
                    c for c in raw[:32]
                    if c.isalnum() or c in " -_."
                )
                if safe:
                    p_name = safe
            if isinstance(data.get("version"), str):
                from .state import sanitize_version
                p_ver = sanitize_version(data["version"])
            if isinstance(data.get("audioProtocolVersion"), int):
                declared_proto = int(data["audioProtocolVersion"])

        is_new, decision, _zvol, _zmute = registry.handle_player_registration(
            ip, p_name, p_ver, declared_proto,
        )
        compatible, negotiated_ver, reason = decision

        # Response body — exact field set from EVDPlayers.h.
        resp = {
            "ok": True,
            "audioProtocolVersion": C.AUDIO_PROTO_VERSION,
            "negotiatedAudioProtocolVersion": negotiated_ver,
            "audioCompatible": compatible,
            "audioCompatReason": reason,
            "epoch": runtime.server_epoch & 0xFFFFFFFF,
            "clockSyncPort": C.CLOCK_SYNC_UDP_PORT,
            "bufferMs": runtime.audio_buffer_ms,
            "sourceSampleRate": C.SOURCE_SAMPLE_RATE,
            "opusSampleRate": runtime.opus_sample_rate,
            "mode": runtime.playback_mode_cstr(),
            "masterVolume": round(runtime.master_volume / 100.0, 3),
            "masterMuted": runtime.master_muted,
            "zoneVolume": round(_zvol / 100.0, 3),
            "zoneMuted": _zmute,
            "encBitrate": runtime.current_bitrate,
            "encComplexity": runtime.current_complexity,
            "encVbr": runtime.current_vbr,
            "encVbrConstraint": runtime.current_vbr_constraint,
            "encSignal": _sig_str(runtime.new_signal),         # INTENDED
            "encApplication": _app_str(runtime.current_application),  # APPLIED
        }
        return _json(resp)

    # ---- /api/node_stats -----------------------------------------------
    @app.route("/api/node_stats", methods=["POST"])
    def api_node_stats():
        empty = _reject_empty_body()
        if empty is not None:
            return empty
        data = _try_parse_json()
        if isinstance(data, dict):
            ip = request.remote_addr or "0.0.0.0"
            try:
                latency = int(data.get("latencyMs", 0))
                queue = int(data.get("queue", 0))
                target = int(data.get("target", 2))
                rebuffers = int(data.get("rebuffers", 0))
                runtime.update_node_perf(
                    ip, latency, queue, target, rebuffers, now_ms(),
                )
            except (TypeError, ValueError):
                pass
        return _json({"ok": True})

    # ---- /api/player/ota_result ---------------------------------------
    @app.route("/api/player/ota_result", methods=["POST"])
    def api_player_ota_result():
        empty = _reject_empty_body()
        if empty is not None:
            return empty
        data = _try_parse_json()
        if isinstance(data, dict):
            ip = request.remote_addr or "0.0.0.0"
            who = data.get("name", ip)
            success = bool(data.get("success", False))
            log_info("OTA result (stub): %s (%s) success=%s",
                     who, ip, success)
        return _json({"ok": True})

    # ---- /api/config ---------------------------------------------------
    @app.route("/api/config", methods=["POST"])
    def api_config():
        empty = _reject_empty_body()
        if empty is not None:
            return empty
        # [config-body-size] - maxLen=1024, mirror accumulateReqBody.
        if request.content_length and request.content_length > 1024:
            return _json({"ok": False,
                          "error": "Config payload too large"}, 400)
        data = _try_parse_json()
        if not isinstance(data, dict):
            return _json({"ok": False, "error": "Invalid JSON"}, 400)
        if not data:
            return _json({"ok": False, "error": "Empty request body"}, 400)

        # ---- Phase 1: validate ----------------------------------------
        mute_explicit = ("masterMuted" in data
                         and isinstance(data["masterMuted"], bool))

        if "masterVolume" in data and isinstance(data["masterVolume"], int):
            v = int(data["masterVolume"])
            if v < 0 or v > 100:
                return _json({"ok": False,
                              "error": "Volume must be between 0 and 100"}, 400)

        if "audioBufferMs" in data and isinstance(data["audioBufferMs"], int):
            v = int(data["audioBufferMs"])
            if v < C.AUDIO_BUFFER_MIN_MS or v > C.AUDIO_BUFFER_MAX_MS:
                return _json({"ok": False,
                              "error": (f"Buffer must be between "
                                        f"{C.AUDIO_BUFFER_MIN_MS} and "
                                        f"{C.AUDIO_BUFFER_MAX_MS} ms")}, 400)

        if "mode" in data and isinstance(data["mode"], str):
            m = data["mode"].lower()
            if m not in ("off", "music", "movie"):
                return _json({"ok": False,
                              "error": "mode must be off, music, or movie"}, 400)

        if "serverName" in data and isinstance(data["serverName"], str):
            v = data["serverName"]
            if (len(v) == 0 or len(v) > 32
                    or v != sanitize_server_name(v)):
                return _json({"ok": False,
                              "error": "Server name must use 1-32 letters or numbers"}, 400)

        if "defaultSource" in data and isinstance(data["defaultSource"], str):
            v = data["defaultSource"].upper()
            if v not in ("BT", "AUX", "USB"):
                return _json({"ok": False,
                              "error": "defaultSource must be BT, AUX or USB"}, 400)

        # ---- Phase 2: apply -------------------------------------------
        changed = False

        if mute_explicit:
            runtime.master_muted = bool(data["masterMuted"])
            changed = True

        if "mode" in data and isinstance(data["mode"], str):
            m = data["mode"].lower()
            if m in ("off", "music", "movie"):
                encoding_ctl.capture_live_into_mode(runtime.playback_mode_idx)
                runtime.playback_mode_idx = C.MODE_NAMES.index(m)
                encoding_ctl.apply_mode_settings_to_live(runtime.playback_mode_idx)
                changed = True

        if "quietMode" in data and isinstance(data["quietMode"], bool):
            runtime.quiet_mode = bool(data["quietMode"])
            changed = True

        if "masterVolume" in data and isinstance(data["masterVolume"], int):
            runtime.master_volume = max(0, min(100, int(data["masterVolume"])))
            if not mute_explicit:
                runtime.master_muted = False
            changed = True

        if "audioBufferMs" in data and isinstance(data["audioBufferMs"], int):
            runtime.audio_buffer_ms = int(data["audioBufferMs"])
            # Persist immediately, matching [audio-buffer-persist-mode-safe].
            with runtime.mode_settings_lock:
                runtime.mode_settings[runtime.playback_mode_idx].buffer_ms = \
                    runtime.audio_buffer_ms
            encoding_ctl.save_encoders_to_disk()
            changed = True

        if "serverName" in data and isinstance(data["serverName"], str):
            new_name = data["serverName"]
            if new_name != runtime.server_name:
                runtime.server_name = new_name
                try:
                    discovery.discovery.restart_mdns(new_name)
                except Exception:
                    pass
            changed = True

        if "defaultSource" in data and isinstance(data["defaultSource"], str):
            v = data["defaultSource"].upper()
            runtime.boot_source_idx = {"BT": 0, "AUX": 1, "USB": 2}[v]
            changed = True

        if not changed:
            return _json({"ok": False,
                          "error": "No valid settings were supplied"}, 400)

        zone_ctl.broadcast_node_config()
        return _json({"ok": True})

    # ---- /api/audio/encoding (GET/POST) -------------------------------
    @app.route("/api/audio/encoding", methods=["GET"])
    def api_encoding_get():
        return _json(encoding_ctl.get_encoding_state())

    @app.route("/api/audio/encoding", methods=["POST"])
    def api_encoding_post():
        empty = _reject_empty_body()
        if empty is not None:
            return empty
        data = _try_parse_json()
        if not isinstance(data, dict):
            return _json({"ok": False, "error": "bad json"}, 400)
        save = bool(data.get("save", False))
        body = encoding_ctl.apply_encoding_post(data, save)
        zone_ctl.broadcast_node_config()
        return _json(body)

    @app.route("/api/audio/encoding/revert", methods=["POST"])
    def api_encoding_revert():
        body = encoding_ctl.revert_encoding()
        zone_ctl.broadcast_node_config()
        return _json(body)

    @app.route("/api/audio/encoding/defaults", methods=["POST"])
    def api_encoding_defaults():
        body = encoding_ctl.apply_defaults()
        zone_ctl.broadcast_node_config()
        return _json(body)

    # ---- /api/source ---------------------------------------------------
    @app.route("/api/source", methods=["POST"])
    def api_source():
        empty = _reject_empty_body()
        if empty is not None:
            return empty
        data = _try_parse_json()
        src = ""
        if isinstance(data, dict) and isinstance(data.get("source"), str):
            src = data["source"].strip().upper()
        if not src or len(src) > 8 or src not in ("BT", "AUX", "USB"):
            return _json({"ok": False, "error": "invalid source"}, 400)
        runtime.receiver_source = src
        log_info("Source request (PC stub): %s", src)
        return _json({"ok": True, "source": src, "transport": "pc"})

    # ---- /api/cmd ------------------------------------------------------
    @app.route("/api/cmd", methods=["POST"])
    def api_cmd():
        empty = _reject_empty_body()
        if empty is not None:
            return empty
        log_info("Cmd request (PC stub): source board not present")
        return _json({"ok": False,
                      "error": "source board not present on PC"}, 503)

    # ---- /api/source/info ---------------------------------------------
    @app.route("/api/source/info", methods=["GET"])
    def api_source_info():
        return _json({
            "ok": True,
            "link": False,
            "btConnected": False,
            "connectedMac": "",
            "volume": 0,
            "activeSource": runtime.receiver_source,
            "a2dpConnecting": 0,
            "a2dpConnected": 0,
            "a2dpDisconnected": 0,
            "avrcConnected": False,
            "volumeEvents": 0,
            "uartLines": 0,
            "heartbeatAgeMs": 0,
            "activeMac": "",
            "devices": [],
            "log": [],
        })

    # ---- /api/bt/* -----------------------------------------------------
    @app.route("/api/bt/devices", methods=["GET"])
    def api_bt_devices():
        return _json({"devices": []})

    @app.route("/api/bt/kick", methods=["POST"])
    def api_bt_kick():
        log_info("BT kick requested (PC stub)")
        return _json({"ok": True})

    @app.route("/api/bt/remove", methods=["POST"])
    def api_bt_remove():
        empty = _reject_empty_body()
        if empty is not None:
            return empty
        data = _try_parse_json()
        if isinstance(data, dict) and isinstance(data.get("mac"), str):
            log_info("BT remove requested (PC stub): %s", data["mac"])
            return _json({"ok": True})
        return _json({"ok": False}, 400)

    # ---- Zones ---------------------------------------------------------
    @app.route("/api/zones/create", methods=["POST"])
    def api_zones_create():
        empty = _reject_empty_body()
        if empty is not None:
            return empty
        data = _try_parse_json()
        if not isinstance(data, dict):
            return _json({"ok": False, "error": "bad json"}, 400)
        status, body = zone_ctl.create_zone(data)
        return _json(body, status)

    @app.route("/api/zones/delete", methods=["POST"])
    def api_zones_delete():
        empty = _reject_empty_body()
        if empty is not None:
            return empty
        data = _try_parse_json()
        if not isinstance(data, dict):
            return _json({"ok": False}, 400)
        status, body = zone_ctl.delete_zone(data)
        return _json(body, status)

    @app.route("/api/zones/move", methods=["POST"])
    def api_zones_move():
        empty = _reject_empty_body()
        if empty is not None:
            return empty
        data = _try_parse_json()
        if not isinstance(data, dict):
            return _json({"ok": False}, 400)
        status, body = zone_ctl.move_node(data)
        return _json(body, status)

    @app.route("/api/zones/config", methods=["POST"])
    def api_zones_config():
        empty = _reject_empty_body()
        if empty is not None:
            return empty
        data = _try_parse_json()
        if not isinstance(data, dict):
            return _json({"ok": False}, 400)
        status, body = zone_ctl.configure_zone(data)
        return _json(body, status)

    @app.route("/api/zones/remove-node", methods=["POST"])
    def api_zones_remove_node():
        empty = _reject_empty_body()
        if empty is not None:
            return empty
        data = _try_parse_json()
        if not isinstance(data, dict):
            return _json({"ok": False, "error": "bad json"}, 400)
        status, body = zone_ctl.remove_node(data)
        return _json(body, status)

    # ---- Network (PC stubs) -------------------------------------------
    @app.route("/api/network/state", methods=["GET"])
    def api_network_state():
        return _json({
            "ok": True,
            "serverName": runtime.server_name,
            "ssid": "",
            "passwordSet": False,
            "useStatic": False,
            "ip": "",
            "gateway": "",
            "subnet": "255.255.255.0",
            "dns": "",
            "activeIp": discovery.get_local_ip(),
            "apMode": False,
        })

    @app.route("/api/network-test", methods=["GET"])
    def api_network_test():
        return _json({"state": "idle"})

    @app.route("/save", methods=["POST"])
    def api_save():
        # Form-encoded. Accept and log, no actual network change.
        server_name = request.form.get("server_name", "").strip()
        ssid = request.form.get("ssid", "").strip()
        if not server_name or len(server_name) > 32:
            return _json({"ok": False,
                          "error": "Server name must be 1-32 letters or digits"}, 400)
        if not all(c.isalnum() for c in server_name):
            return _json({"ok": False,
                          "error": "Server name must contain only letters and digits"}, 400)
        if not ssid:
            return _json({"ok": False,
                          "error": "Wi-Fi SSID is required"}, 400)
        log_info("Network save request (PC stub): ssid='%s'", ssid)
        if server_name != runtime.server_name:
            runtime.server_name = server_name
            try:
                discovery.discovery.restart_mdns(server_name)
            except Exception:
                pass
        return _json({"ok": True, "state": "running"})

    @app.route("/api/wifi/scan/start", methods=["GET"])
    def api_wifi_scan_start():
        log_info("WiFi scan requested (PC stub)")
        return _json({"ok": True, "state": "started"})

    @app.route("/api/wifi/scan/status", methods=["GET"])
    def api_wifi_scan_status():
        return _json({"state": "done", "networks": []})

    @app.route("/api/restart", methods=["POST"])
    def api_restart():
        log_info("Restart requested (PC stub, allow_restart=%s)",
                 allow_restart)
        if allow_restart:
            # Let Flask respond before exiting.
            import threading
            threading.Timer(0.3, lambda: os._exit(0)).start()
        return _json({"ok": True})

    @app.route("/settings", methods=["GET"])
    def api_settings_page():
        html = (
            "<!doctype html><html><head><title>Settings</title></head>"
            "<body style='font-family: sans-serif; max-width: 720px; "
            "margin: 2em auto;'><h1>Network settings</h1>"
            "<p>Network configuration is managed by the operating system "
            "on the PC server. Use the dashboard's Network tab for the "
            "same information.</p></body></html>"
        )
        return Response(html, status=200, mimetype="text/html; charset=utf-8")

    # ---- OTA ----------------------------------------------------------
    @app.route("/api/ota/plan", methods=["GET"])
    def api_ota_plan():
        players = runtime.snapshot_players(now_ms())
        return _json({
            "ok": True,
            "serverVersion": C.FW_VERSION,
            "inProgress": False,
            "progressPct": 0,
            "lastTarget": "",
            "lastError": C.OTA_STUB_ERROR,
            "totalBytes": 0,
            "usedBytes": 0,
            "allSpoolFiles": [],
            "files": [],
            "nodes": [
                {"ip": p.ip, "name": p.name, "version": p.version,
                 "reachable": True}
                for p in players
            ],
        })

    @app.route("/api/ota/status", methods=["GET"])
    def api_ota_status():
        return _json({
            "ok": True,
            "inProgress": False,
            "stalled": False,
            "stalledAgeMs": 0,
            "progressPct": 0,
            "target": "",
            "error": "",
            "completed": [],
            "failed": [],
        })

    @app.route("/api/ota/apply", methods=["POST"])
    def api_ota_apply():
        log_warn("OTA apply requested (PC stub)")
        return _json({"ok": False, "error": C.OTA_STUB_ERROR}, 503)

    @app.route("/api/ota/upload", methods=["POST"])
    def api_ota_upload():
        log_warn("OTA upload requested (PC stub)")
        return _json({"ok": False, "error": C.OTA_STUB_ERROR}, 503)

    @app.route("/api/ota/delete", methods=["POST"])
    def api_ota_delete():
        log_warn("OTA delete requested (PC stub)")
        return _json({"ok": False, "error": C.OTA_STUB_ERROR}, 503)

    # ---- 404 catch-all -------------------------------------------------
    @app.errorhandler(404)
    def api_not_found(_e):
        return _json({"ok": False, "error": "not found"}, 404)

    return app