"""
enkelvoud.__main__ - CLI entry point.

Usage:
  python -m enkelvoud [options]

Options:
  --host HOST                HTTP bind address (default 0.0.0.0)
  --port PORT                HTTP port (default 80; needs admin on Windows)
  --name NAME                Initial server name (default from config)
  --data-dir PATH            Directory for persisted state (default ./data)
  --web-dir PATH             Directory containing dashboard.html (default ./web)
  --dlna on|off              DLNA stub flag (default off)
  --log-level 0..3           Initial log verbosity (default 3)
  --allow-restart            /api/restart actually exits the process
"""

import argparse
import os
import signal
import socket
import sys
import threading
import time

from . import capture as capture_mod
from . import clock_sync as clock_sync_mod
from . import constants as C
from . import ctrl as ctrl_mod
from . import discovery as discovery_mod
from . import encoder as encoder_mod
from . import encoding_ctl
from . import peers as peers_mod
from . import registry
from . import sender as sender_mod
from . import zone_ctl
from .log_ring import log_info, log_warn, log_error
from .state import runtime, now_ms


def _stats_meter(stop: threading.Event) -> None:
    """Mirror EVDMain.h loop()'s 15 s stats window."""
    prev_dropped = 0
    prev_encoded = 0
    prev_packets = 0
    prev_bytes = 0
    while not stop.is_set():
        if stop.wait(15.0):
            break
        cur_dropped = runtime.stat_opus_dropped
        cur_encoded = runtime.stat_opus_encoded
        cur_packets = runtime.stat_udp_packets_sent
        cur_bytes = runtime.stat_udp_bytes_sent

        runtime.stat_opus_dropped_last_5s = max(0, cur_dropped - prev_dropped)
        runtime.stat_opus_encoded_last_5s = max(0, cur_encoded - prev_encoded)
        runtime.stat_udp_packets_last_5s = max(0, cur_packets - prev_packets)
        runtime.stat_udp_bytes_last_5s = max(0, cur_bytes - prev_bytes)

        prev_dropped = cur_dropped
        prev_encoded = cur_encoded
        prev_packets = cur_packets
        prev_bytes = cur_bytes


def _check_competing_servers() -> None:
    """Report the other Enkelvoud servers on this LAN.

    [servers-decide] With the peer protocol the others are expected: the
    election decides which one the nodes attach to. Only a server that answers
    discovery without a peer beacon (old firmware) still splits the fleet, so
    the legacy probe runs only when no peer beacons were heard.
    """
    time.sleep(2.0)          # let the first peer beacons arrive
    try:
        snap = peers_mod.peers.snapshot()
        others = [s for s in snap.get("servers", []) if not s.get("self")]
    except Exception as e:
        log_warn("Peer servers: snapshot failed: %s", e)
        others = []
        snap = {}
    runtime.competing_servers = others
    if others:
        log_info("Peer servers: %d on this LAN; leader is %s (this server: %s)",
                 len(others), snap.get("leaderId"), snap.get("role"))
        for o in others:
            log_info("Peer: %s at %s, priority %s, %s",
                     o.get("name", o.get("id")), o.get("ip"),
                     o.get("priority"), "alive" if o.get("alive") else "down")
        return

    # Legacy fallback: a server that predates the peer beacon still answers
    # DISCOVER, and that one really does split the fleet.
    try:
        legacy = discovery_mod.probe_other_servers()
    except Exception as e:
        log_warn("Discovery: competing-server probe failed: %s", e)
        return
    runtime.competing_servers = legacy
    for o in legacy:
        # Keep this short: the log ring truncates lines at 127 chars.
        parts = o["reply"].split()
        ver = (" (v" + parts[3] + ")") if len(parts) >= 4 else ""
        log_warn("Discovery: second Enkelvoud server at %s%s has no peer "
                 "beacon - update it so the election can take over.",
                 o["ip"], ver)
    if legacy:
        log_warn("Discovery: found %d legacy server(s) on this LAN.",
                 len(legacy))


def _notify_standby() -> None:
    """Tell our subscribers to re-discover the new leader.

    Old node firmware ignores the unknown ctrl type and falls back to its
    existing stale-contact re-discovery (~60 s); new node firmware can drop the
    contact immediately.
    """
    try:
        subs = runtime.snapshot_subscribers(now_ms())
        for s in subs:
            ctrl_mod.send_ctrl_to_subscriber(s.ip, '{"type":"server_standby"}')
        if subs:
            log_info("Peer role: told %d node(s) to re-discover", len(subs))
    except Exception as e:
        log_warn("Peer role: standby notice failed: %s", e)


def _on_peer_role(role: str) -> None:
    """Peer election handed this server a new role (servers decide)."""
    is_leader = (role == "leader")
    try:
        discovery_mod.discovery.set_leader(is_leader)
    except Exception as e:
        log_warn("Peer role: discovery switch failed: %s", e)
    if not is_leader:
        _notify_standby()


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="enkelvoud")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=80)
    p.add_argument("--name", default=None)
    p.add_argument("--data-dir", default="./data")
    p.add_argument("--web-dir", default="./web")
    p.add_argument("--dlna", choices=["on", "off"], default="off")
    p.add_argument("--log-level", type=int, default=3, choices=[0, 1, 2, 3])
    p.add_argument("--allow-restart", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    # ---- Persistence paths ----------------------------------------------
    os.makedirs(args.data_dir, exist_ok=True)
    zone_ctl.set_persistence_path(os.path.join(args.data_dir, "zones.json"))
    encoding_ctl.set_persistence_path(
        os.path.join(args.data_dir, "encoders.json"))

    # ---- Initial state --------------------------------------------------
    runtime.log_level = args.log_level
    runtime.dlna_enabled = (args.dlna == "on")
    if args.name:
        runtime.server_name = args.name

    zone_ctl.load_zones_from_disk()
    encoding_ctl.load_encoders_from_disk()
    encoding_ctl.apply_mode_settings_to_live(runtime.playback_mode_idx)

    log_info("Enkelvoud PC server starting")
    log_info("  version: %s", C.VERSION)
    log_info("  serverEpoch: %d", runtime.server_epoch)
    log_info("  serverName: %s", runtime.server_name)
    log_info("  mode: %s", runtime.playback_mode_cstr())
    log_info("  dlna: %s", runtime.dlna_enabled)

    # ---- Start background threads --------------------------------------
    stop_event = threading.Event()

    meter_thread = threading.Thread(
        target=_stats_meter, args=(stop_event,),
        name="stats-meter", daemon=True,
    )
    meter_thread.start()

    try:
        discovery_mod.discovery.start(args.port)
    except OSError as e:
        log_error("Discovery failed to start: %s", e)
        return 1

    if args.port != 80:
        log_warn("HTTP is on port %d, but player nodes always POST to "
                 "http://<server-ip>/ (port 80 is hard-coded in the node "
                 "firmware). Nodes cannot register while the server is not on "
                 "port 80.", args.port)

    # [servers-decide] Peer beacons + election. The leader answers nodes; a
    # standby steps discovery and audio down until the leader disappears.
    try:
        peers_mod.peers.configure(
            os.path.join(args.data_dir, "peers.json"),
            args.port, on_role_change=_on_peer_role,
        )
        peers_mod.peers.start()
    except Exception as e:
        log_warn("Peer election failed to start: %s", e)
    log_info("  servers: role %s, priority %d",
             peers_mod.peers.role, peers_mod.peers.priority)

    # A second Enkelvoud server on this LAN (e.g. an ESP32-S3 left powered)
    # splits the node fleet between the two. Warn clearly instead of letting it
    # look like "only some nodes connect".
    threading.Thread(
        target=_check_competing_servers, name="discovery-probe", daemon=True,
    ).start()

    try:
        clock_sync_mod.clock_sync.start()
    except OSError as e:
        log_error("Clock sync failed to start: %s", e)
        return 1

    capture_ok = capture_mod.capture.start()
    if not capture_ok:
        log_error("Capture failed to start; audio pipeline will not run")

    encoder_ok = encoder_mod.encoder.start()
    if not encoder_ok:
        log_error("Encoder failed to start")
        capture_ok = False

    if capture_ok and encoder_ok:
        sender_mod.sender.start()
    else:
        log_warn("Sender will not start (capture or encoder unavailable)")

    # ---- Flask app -----------------------------------------------------
    from .web_api import create_app
    app = create_app(
        web_dir=args.web_dir,
        allow_restart=args.allow_restart,
        allow_ota_success=False,
    )

    # ---- Graceful shutdown --------------------------------------------
    def _shutdown(_signum=None, _frame=None):
        log_info("Shutting down")
        stop_event.set()
        try:
            peers_mod.peers.stop()
        except Exception:
            pass
        try:
            discovery_mod.discovery.stop()
        except Exception:
            pass
        try:
            clock_sync_mod.clock_sync.stop()
        except Exception:
            pass
        try:
            capture_mod.capture.stop()
        except Exception:
            pass
        try:
            encoder_mod.encoder.stop()
        except Exception:
            pass
        try:
            sender_mod.sender.stop()
        except Exception:
            pass

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    log_info("HTTP listening on %s:%d", args.host, args.port)
    try:
        app.run(host=args.host, port=args.port, threaded=True,
                use_reloader=False, debug=False)
    except OSError as e:
        if args.port == 80:
            log_error("Failed to bind port 80: %s", e)
            log_error("Try --port 8080 (needs admin/root for port 80)")
        else:
            log_error("Failed to bind %s:%d: %s", args.host, args.port, e)
        return 1
    finally:
        _shutdown()

    return 0


if __name__ == "__main__":
    sys.exit(main())