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
from . import discovery as discovery_mod
from . import encoder as encoder_mod
from . import encoding_ctl
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
    log_info("  FW_VERSION: %s", C.FW_VERSION)
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
        discovery_mod.discovery.start()
    except OSError as e:
        log_error("Discovery failed to start: %s", e)
        return 1

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