"""
enkelvoud.web_api - Flask application for every /api/* route.

The PC server is the source and runs on an already-configured machine, so the
ESP32-only surfaces are gone: there is no network config tab, no source-board
(BT/AUX/USB) switching, no OTA wizard, and no master volume/mute (Windows owns
volume). Player-node registration, zones, encoding and diagnostics remain.
"""

import ipaddress
import json
import os
from typing import Optional

from flask import Flask, Response, request

from . import constants as C
from . import ctrl
from . import discovery
from . import encoding_ctl
from . import ota
from . import peers as peers_mod
from . import recorder
from . import registry
from . import sysinfo
from . import zone_ctl
from .log_ring import log_ring, log_info
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
    now = now_ms()
    players = runtime.snapshot_players(now)
    zones = runtime.snapshot_zones()

    # Inject synthetic main zone if the operator deleted it somehow
    # (parity with handleApiState).
    if not any(z.id == C.MAIN_ZONE_ID for z in zones):
        from .state import Zone
        zones.insert(0, Zone(id=C.MAIN_ZONE_ID, name="Main Room",
                             volume=100, muted=False, node_ips=[]))

    # [discovered-nodes] A player is "assigned" only once it is in a zone.
    # Unassigned players stay in the Discovered pool and get no audio until
    # the operator drags them into a room. Do NOT auto-attach them to Main.
    assigned = set()
    for z in zones:
        assigned.update(z.node_ips)

    # PC system load + memory (psutil if available). CPU comes from the shared
    # sampler so /api/state and /api/stats cannot steal each other's baseline.
    cpu = sysinfo.read()
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
            "muted": z.muted, "quiet": z.quiet, "nodes": node_entries,
        })

    # [servers-decide] Election state for the dashboard's Servers card.
    peers_snap = peers_mod.peers.snapshot()

    return {
        "ok": True,
        "ip": discovery.get_local_ip(),
        "serverName": runtime.server_name,
        "role": peers_snap["role"],
        "priority": peers_mod.peers.priority,
        "leaderId": peers_snap["leaderId"],
        "servers": peers_snap["servers"],
        "quietMode": runtime.quiet_mode,
        "cpuPct": cpu["proc"],
        "sysCpuPct": cpu["sys"],
        "heapUsedPct": heap_used_pct,
        "heapFreeBytes": heap_free_bytes,
        "heapTotalBytes": heap_total_bytes,
        "heapMaxAlloc": heap_max_alloc,
        "audioBufferMs": runtime.audio_buffer_ms,
        "opusSampleRate": runtime.opus_sample_rate,
        "mode": runtime.playback_mode_cstr(),
        "audioProtocolVersion": C.AUDIO_PROTO_VERSION,
        "serverEpoch": runtime.server_epoch & 0xFFFFFFFF,
        "players": [
            {
                "name": p.name, "ip": p.ip, "version": p.version,
                "lastSeenAgeMs": now - p.last_seen_ms,
                "assigned": p.ip in assigned,
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

    # Shared sampler — see sysinfo.py for why this is not a per-request call.
    cpu = sysinfo.read()

    try:
        from . import capture as capture_mod
        cinfo = capture_mod.capture.info()
    except Exception:
        cinfo = {"backend": "unknown", "rate": 0, "channels": 0, "ok": False}

    return {
        "ok": True,
        "ip": discovery.get_local_ip(),
        "cpuPct": cpu["proc"],
        "sysCpuPct": cpu["sys"],
        "captureBackend": cinfo["backend"],
        "captureRate": cinfo["rate"],
        "captureChannels": cinfo["channels"],
        "captureOk": cinfo.get("ok", False),
        "competingServers": runtime.competing_servers,
        "wsClients": runtime.subscriber_count,   # no WebSocket on PC; subscribers is the honest proxy
        "mode": runtime.playback_mode_cstr(),
        "audioBufferMs": runtime.audio_buffer_ms,
        "opusSampleRate": runtime.opus_sample_rate,
        "opusFrameSamples": opus_frame_samples,
        "udpErrors": runtime.stat_udp_errors,
        "audioProtocolVersion": C.AUDIO_PROTO_VERSION,
        "serverEpoch": runtime.server_epoch & 0xFFFFFFFF,
        "clockSyncPort": C.CLOCK_SYNC_UDP_PORT,
        "syncRequestsServed": runtime.stat_sync_requests_served,
        "syncRequestErrors": runtime.stat_sync_request_errors,
        "syncRequestsThrottled": runtime.stat_sync_requests_throttled,
        "audioIncompatibleSkips": runtime.stat_audio_incompatible_skips,
        "audioProvisionalSkips": runtime.stat_audio_provisional_skips,
        "audioSyncPending": runtime.stat_audio_sync_pending,
        "streaming": {
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
        "udpSendLoopCount": runtime.udp_send_loop_count,
        "udpSendStallResets": runtime.udp_send_stall_resets,
        "udpPacerAheadReset": runtime.udp_pacer_ahead_reset,
        "udpPacerBehindReset": runtime.udp_pacer_behind_reset,
        "udpPacerDriftReset": runtime.udp_pacer_drift_reset,
        "udpStageAfterRecv": runtime.udp_stage_after_recv,
        "udpStageAfterStore": runtime.udp_stage_after_store,
    }


# ---- Flask app factory --------------------------------------------------

def create_app(web_dir: str, allow_restart: bool = False,
               allow_ota_success: bool = False) -> Flask:
    app = Flask("enkelvoud")
    # Werkzeug's form parser spills request bodies to disk before a route sees
    # them, so cap the whole request rather than trusting the per-part length.
    app.config["MAX_CONTENT_LENGTH"] = ota.MAX_FW_BYTES + (1 << 20)

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
            "<p>Place the dashboard at <code>web/dashboard.html</code> and reload.</p>"
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
        return _json({"version": C.VERSION})

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

        # [servers-decide] Only the elected leader serves nodes. A standby
        # answers 503 so the node's contact fails and it re-discovers the
        # leader (which owns mDNS + the discovery reply).
        if not runtime.is_leader:
            leader = peers_mod.peers.leader_descriptor()
            return _json({
                "ok": False,
                "status": "standby",
                "leaderId": leader.get("id", ""),
                "leaderIp": leader.get("ip", ""),
            }, 503)

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
            "encSignal": _sig_str(runtime.new_signal),         # INTENDED (unknown -> "auto", firmware says "unknown")
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

    # ---- /api/servers (P3: servers decide) -----------------------------
    @app.route("/api/servers", methods=["GET"])
    def api_servers():
        return _json(peers_mod.peers.snapshot())

    @app.route("/api/servers/priority", methods=["POST"])
    def api_servers_priority():
        empty = _reject_empty_body()
        if empty is not None:
            return empty
        data = _try_parse_json()
        if not isinstance(data, dict) or not isinstance(data.get("priority"), int):
            return _json({"ok": False, "error": "priority int required"}, 400)
        value = peers_mod.peers.set_priority(int(data["priority"]))
        return _json({"ok": True, "priority": value,
                      "role": peers_mod.peers.role})

    @app.route("/api/servers/order", methods=["POST"])
    def api_servers_order():
        empty = _reject_empty_body()
        if empty is not None:
            return empty
        data = _try_parse_json()
        if not isinstance(data, dict) or not isinstance(data.get("order"), list):
            return _json({"ok": False, "error": "order list required"}, 400)
        order = [x for x in data["order"] if isinstance(x, str)]
        results = peers_mod.peers.apply_order(order)
        snap = peers_mod.peers.snapshot()
        return _json({"ok": True, "results": results,
                      "servers": snap["servers"], "role": snap["role"],
                      "leaderId": snap["leaderId"]})

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
        if "audioBufferMs" in data and isinstance(data["audioBufferMs"], int):
            v = int(data["audioBufferMs"])
            if v < C.AUDIO_BUFFER_MIN_MS or v > C.AUDIO_BUFFER_MAX_MS:
                return _json({"ok": False,
                              "error": (f"Buffer must be between "
                                        f"{C.AUDIO_BUFFER_MIN_MS} and "
                                        f"{C.AUDIO_BUFFER_MAX_MS} ms")}, 400)

        if "mode" in data and isinstance(data["mode"], str):
            m = data["mode"].lower()
            if m not in C.MODE_NAMES:
                return _json({"ok": False,
                              "error": "mode must be music or movie"}, 400)

        if "serverName" in data and isinstance(data["serverName"], str):
            v = data["serverName"]
            if (len(v) == 0 or len(v) > 32
                    or v != sanitize_server_name(v)):
                return _json({"ok": False,
                              "error": "Server name must use 1-32 letters or numbers"}, 400)

        # ---- Phase 2: apply -------------------------------------------
        changed = False

        if "mode" in data and isinstance(data["mode"], str):
            m = data["mode"].lower()
            if m in C.MODE_NAMES:
                encoding_ctl.capture_live_into_mode(runtime.playback_mode_idx)
                runtime.playback_mode_idx = C.MODE_NAMES.index(m)
                encoding_ctl.apply_mode_settings_to_live(runtime.playback_mode_idx)
                # Persist the choice: load_encoders_from_disk() reads play_mode
                # back on startup, so without this the mode silently reverts.
                encoding_ctl.save_encoders_to_disk()
                changed = True

        if "quietMode" in data and isinstance(data["quietMode"], bool):
            runtime.quiet_mode = bool(data["quietMode"])
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

    # ---- /api/zone/forget-node (PC only) -------------------------------
    @app.route("/api/zone/forget-node", methods=["POST"])
    def api_zone_forget_node():
        """[first-sight-test-hook] Drop one IP from every table, so its next
        /api/player/register is a genuine first sight.

        The firmware has no such endpoint and does not need one: a node
        reboots, the 70 s stale timeout expires, and it registers as new.
        On a PC the server and the test harness live in the same process
        lifetime, so there is no way to re-exercise the first-sight push
        without restarting the server (which loses zones and settings).
        It only removes bookkeeping for an IP; it grants no control.
        """
        ip = (request.args.get("ip") or "").strip()
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            return _json({"ok": False, "error": "ip required"}, 400)

        with runtime.players_lock:
            runtime.player_nodes = [p for p in runtime.player_nodes if p.ip != ip]
        with runtime.subs_lock:
            runtime.subscribers = [s for s in runtime.subscribers if s.ip != ip]
            runtime.subscriber_count = len(runtime.subscribers)
        with runtime.zones_lock:
            for z in runtime.zones:
                if ip in z.node_ips:
                    z.node_ips.remove(ip)
        runtime.remove_node_perf(ip)
        log_info("Node forgotten (test hook): %s", ip)
        return _json({"ok": True, "forgot": ip})

    # ---- OTA (player firmware push) ------------------------------------
    # The PC keeps a spool of .bin files and pushes one to the nodes the
    # operator ticked. A node accepts a plain multipart POST on its own
    # /api/ota - the same request the ESP32 server's otaPushToNode() sends.
    # There is no server self-flash: the PC app is updated by replacing files.
    @app.route("/api/ota/plan", methods=["GET"])
    def api_ota_plan():
        return _json(ota.plan())

    @app.route("/api/ota/status", methods=["GET"])
    def api_ota_status():
        return _json(ota.status())

    @app.route("/api/ota/upload", methods=["POST"])
    def api_ota_upload():
        if request.content_length and request.content_length > ota.MAX_FW_BYTES + 65536:
            return _json({"ok": False, "error": "firmware too large"}, 413)
        storage = next(iter(request.files.values()), None)
        if storage is None:
            return _json({"ok": False, "error": "no file part"}, 400)
        status_code, body = ota.save_upload(storage)
        return _json(body, status_code)

    @app.route("/api/ota/apply", methods=["POST"])
    def api_ota_apply():
        empty = _reject_empty_body()
        if empty is not None:
            return empty
        data = _try_parse_json()
        if not isinstance(data, dict):
            return _json({"ok": False, "error": "bad json"}, 400)
        status_code, body = ota.start_job(data.get("nodes"),
                                          data.get("nodeFile", ""))
        return _json(body, status_code)

    @app.route("/api/ota/delete", methods=["POST"])
    def api_ota_delete():
        empty = _reject_empty_body()
        if empty is not None:
            return empty
        data = _try_parse_json()
        if not isinstance(data, dict):
            return _json({"ok": False, "error": "bad json"}, 400)
        status_code, body = ota.delete_file(data.get("name", ""))
        return _json(body, status_code)

    @app.route("/api/player/ota_result", methods=["POST"])
    def api_player_ota_result():
        data = _try_parse_json()
        ota.note_result(data if isinstance(data, dict) else {})
        return _json({"ok": True})

    # ---- Master recording (server + selected nodes) --------------------
    # One session id ties the ctrl start/stop pushes to the node reports.
    # Nodes with the recorder firmware POST /api/player/rec_ack and serve
    # /api/rec_report; older nodes are covered by the 1 Hz /api/logs poll.
    @app.route("/api/rec/start", methods=["POST"])
    def api_rec_start():
        empty = _reject_empty_body()
        if empty is not None:
            return empty
        data = _try_parse_json()
        if not isinstance(data, dict):
            return _json({"ok": False, "error": "bad json"}, 400)
        status_code, body = recorder.start(
            data.get("recId"), data.get("nodes"), bool(data.get("server", True)))
        return _json(body, status_code)

    @app.route("/api/rec/stop", methods=["POST"])
    def api_rec_stop():
        empty = _reject_empty_body()
        if empty is not None:
            return empty
        data = _try_parse_json()
        if not isinstance(data, dict):
            return _json({"ok": False, "error": "bad json"}, 400)
        status_code, body = recorder.stop(data.get("recId"))
        return _json(body, status_code)

    @app.route("/api/player/rec_ack", methods=["POST"])
    def api_player_rec_ack():
        data = _try_parse_json()
        rec_id = data.get("recId") if isinstance(data, dict) else None
        status_code = recorder.ack(rec_id, request.remote_addr or "")
        return _json({"ok": status_code == 200}, status_code)

    @app.route("/api/restart", methods=["POST"])
    def api_restart():
        log_info("Restart requested (PC stub, allow_restart=%s)",
                 allow_restart)
        if allow_restart:
            # Let Flask respond before exiting.
            import threading
            threading.Timer(0.3, lambda: os._exit(0)).start()
        return _json({"ok": True})

    # ---- 404 catch-all -------------------------------------------------
    @app.errorhandler(404)
    def api_not_found(_e):
        return _json({"ok": False, "error": "not found"}, 404)

    return app