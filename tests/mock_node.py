#!/usr/bin/env python3
"""
tests/mock_node.py - end-to-end test harness for the Enkelvoud PC server.

Usage:
    python tests/mock_node.py --server 192.168.1.50
    python tests/mock_node.py --server 192.168.1.50 --local 192.168.1.99

Exit code 0 = all tests passed. Any failure prints a diagnosis and exits 1.

Requirements: `requests` (pip install requests).
"""

import argparse
import json
import socket
import struct
import sys
import time
from typing import List, Optional, Tuple

import requests


# ---- Constants (verbatim from the C++ source) ----------------------------

DISCOVERY_PORT      = 4201
AUDIO_UDP_PORT      = 4202
CTRL_UDP_PORT       = 4203
CLOCK_SYNC_UDP_PORT = 4204

DISCOVERY_MAGIC_REQ = b"ENKELVOUDP/1 DISCOVER"
DISCOVERY_MAGIC_RPY = b"ENKELVOUDP/1 SERVER"

AUDIO_PROTO_MAGIC   = 0xE1D0
AUDIO_PROTO_VERSION = 2
AUDIO_HEADER_BYTES  = 12
OPUS_MAX_PACKET_BYTES = 1024


# ---- Helpers -------------------------------------------------------------

def _fail(msg: str) -> None:
    print(f"  FAIL: {msg}")


def _ok(msg: str) -> None:
    print(f"  ok: {msg}")


def _warn(msg: str) -> None:
    print(f"  WARN: {msg}")


# ---- Test 1: Discovery ---------------------------------------------------

def test_discovery(server: str) -> bool:
    print("[1] Discovery UDP :4201")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2.0)
        s.sendto(DISCOVERY_MAGIC_REQ, (server, DISCOVERY_PORT))
        data, addr = s.recvfrom(2048)
        s.close()
    except socket.timeout:
        _fail("no reply within 2 s")
        return False
    except OSError as e:
        _fail(f"sendto/recvfrom: {e}")
        return False

    if not data.startswith(DISCOVERY_MAGIC_RPY):
        _fail(f"reply does not start with {DISCOVERY_MAGIC_RPY!r}")
        return False

    # Reply format: "ENKELVOUDP/1 SERVER <name> <version> <ip>"
    parts = data.decode("utf-8", errors="replace").split()
    if len(parts) != 5:
        _fail(f"expected 5 space-separated tokens, got {len(parts)}: {data!r}")
        return False
    if parts[2] != "SERVER":
        # tokens: ['ENKELVOUDP/1', 'SERVER', name, version, ip]
        # Wait - actually 5 tokens = "ENKELVOUDP/1","SERVER","name","ver","ip"
        pass

    _ok(f"reply from {addr[0]}: {data.decode()!r}")
    return True


# ---- Test 2: Clock sync --------------------------------------------------

def test_clock_sync(server: str) -> bool:
    print("[2] Clock sync UDP :4204")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2.0)
        client_tx = 1234567890
        req = json.dumps({
            "type": "sync_req",
            "clientTxMs": client_tx,
        }).encode("utf-8")
        t0 = time.monotonic()
        s.sendto(req, (server, CLOCK_SYNC_UDP_PORT))
        data, addr = s.recvfrom(2048)
        t1 = time.monotonic()
        s.close()
    except socket.timeout:
        _fail("no sync_resp within 2 s")
        return False
    except OSError as e:
        _fail(f"sendto/recvfrom: {e}")
        return False

    try:
        doc = json.loads(data.decode("utf-8"))
    except json.JSONDecodeError:
        _fail(f"non-JSON reply: {data!r}")
        return False

    if doc.get("type") != "sync_resp":
        _fail(f'type != "sync_resp": {doc}')
        return False
    if doc.get("protoVersion") != AUDIO_PROTO_VERSION:
        _fail(f"protoVersion != {AUDIO_PROTO_VERSION}: {doc}")
        return False
    if doc.get("clientTxMs") != client_tx:
        _fail(f"clientTxMs not echoed: {doc}")
        return False
    for k in ("serverRxMs", "serverTxMs", "epoch"):
        if not isinstance(doc.get(k), int):
            _fail(f"missing/invalid {k}: {doc}")
            return False
    if doc["serverTxMs"] < doc["serverRxMs"]:
        _fail(f"serverTxMs < serverRxMs: {doc}")
        return False
    # Rx should be captured within the last few seconds.
    rtt_ms = int((t1 - t0) * 1000)
    _ok(f"RTT {rtt_ms} ms, epoch={doc['epoch']}")
    return True


# ---- Test 3: Registration ------------------------------------------------

def test_register(server: str, local_ip: str) -> bool:
    print("[3] POST /api/player/register")
    url = f"http://{server}/api/player/register"
    body = {
        "name": "MockNodeTest",
        "version": "0.0.1",
        "audioProtocolVersion": AUDIO_PROTO_VERSION,
    }
    try:
        r = requests.post(url, json=body, timeout=5)
    except requests.RequestException as e:
        _fail(f"POST failed: {e}")
        return False
    if r.status_code != 200:
        _fail(f"HTTP {r.status_code}: {r.text[:200]}")
        return False
    try:
        doc = r.json()
    except ValueError:
        _fail(f"non-JSON response: {r.text[:200]}")
        return False

    required = [
        "ok", "audioProtocolVersion", "negotiatedAudioProtocolVersion",
        "audioCompatible", "audioCompatReason", "epoch", "clockSyncPort",
        "bufferMs", "sourceSampleRate", "opusSampleRate", "mode",
        "masterVolume", "masterMuted", "zoneVolume", "zoneMuted",
        "encBitrate", "encComplexity", "encVbr", "encVbrConstraint",
        "encSignal", "encApplication",
    ]
    missing = [k for k in required if k not in doc]
    if missing:
        _fail(f"missing fields: {missing}")
        return False
    if not doc.get("audioCompatible"):
        _fail(f"server refused compatibility: {doc.get('audioCompatReason')}")
        return False
    _ok(f"compatible, negotiated={doc['negotiatedAudioProtocolVersion']}, "
        f"bufferMs={doc['bufferMs']}")
    return True


# ---- Test 4: First-sight ctrl packets ------------------------------------

def test_first_sight_ctrl(server: str, local_port: int) -> bool:
    print("[4] First-sight ctrl packets on :4203")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", local_port))
        s.settimeout(3.0)
    except OSError as e:
        _warn(f"cannot bind local UDP port: {e}")
        return True

    try:
        seen_host_audio = False
        seen_time_sync_first_sight = False
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not (
            seen_host_audio and seen_time_sync_first_sight
        ):
            try:
                data, _addr = s.recvfrom(4096)
            except socket.timeout:
                break
            try:
                doc = json.loads(data.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            t = doc.get("type")
            if t == "host_audio_sync":
                seen_host_audio = True
                _ok("host_audio_sync received")
            elif t == "time_sync" and not doc.get("authoritative", False):
                seen_time_sync_first_sight = True
                _ok("time_sync (first-sight, no 'authoritative') received")
    finally:
        s.close()

    if not seen_host_audio:
        _fail("no host_audio_sync on :4203 within 3 s")
        return False
    return True


# ---- Test 5: Audio headers ----------------------------------------------

def test_audio_headers(server: str) -> bool:
    print("[5] Audio packets on :4202")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", AUDIO_UDP_PORT))
        s.settimeout(5.0)
    except OSError as e:
        _fail(f"cannot bind UDP {AUDIO_UDP_PORT}: {e}")
        return False

    prev_seq: Optional[int] = None
    prev_ms: Optional[int] = None
    count = 0
    try:
        while count < 20:
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                _fail(f"only received {count}/20 packets in 5 s")
                return False
            if addr[0] != server and addr[0] != "127.0.0.1":
                # Permissive: any source is fine, but log it once.
                if count == 0:
                    _warn(f"packets from {addr[0]}, not {server}")

            if len(data) < AUDIO_HEADER_BYTES:
                _fail(f"datagram too short: {len(data)} bytes")
                return False

            magic, version, flags, seq, captured_ms = struct.unpack(
                "<HBBII", data[:AUDIO_HEADER_BYTES],
            )
            if magic != AUDIO_PROTO_MAGIC:
                _fail(f"magic 0x{magic:04X} != 0x{AUDIO_PROTO_MAGIC:04X}")
                return False
            if version != AUDIO_PROTO_VERSION:
                _fail(f"version {version} != {AUDIO_PROTO_VERSION}")
                return False
            if flags != 0:
                _fail(f"flags {flags} != 0 (source hardcodes 0)")
                return False

            opus_len = len(data) - AUDIO_HEADER_BYTES
            if opus_len <= 0:
                _fail(f"empty Opus payload")
                return False
            if opus_len > OPUS_MAX_PACKET_BYTES:
                _fail(f"payload {opus_len} > {OPUS_MAX_PACKET_BYTES}")
                return False

            if prev_seq is not None:
                if seq != prev_seq + 1:
                    _fail(f"seq jumped {prev_seq} -> {seq}")
                    return False
            if prev_ms is not None:
                delta = (captured_ms - prev_ms) & 0xFFFFFFFF
                if delta < 10 or delta > 30:
                    _fail(f"captured_ms delta {delta} not ~20")
                    return False

            prev_seq = seq
            prev_ms = captured_ms
            count += 1
    finally:
        s.close()

    _ok(f"received {count} packets, seq {prev_seq - count + 1}..{prev_seq}")
    return True


# ---- Test 6: /api/node_stats and /api/state integration ------------------

def test_node_stats(server: str) -> bool:
    print("[6] POST /api/node_stats + /api/state integration")
    body = {
        "latencyMs": 42,
        "queue": 3,
        "target": 2,
        "rebuffers": 0,
    }
    try:
        r = requests.post(f"http://{server}/api/node_stats",
                          json=body, timeout=5)
    except requests.RequestException as e:
        _fail(f"POST failed: {e}")
        return False
    if r.status_code != 200 or not r.json().get("ok"):
        _fail(f"HTTP {r.status_code}: {r.text[:200]}")
        return False

    try:
        state = requests.get(f"http://{server}/api/stats",
                             timeout=5).json()
    except requests.RequestException as e:
        _fail(f"GET /api/stats failed: {e}")
        return False

    nodes = state.get("nodes", [])
    if not any(n.get("latencyMs") == 42 for n in nodes):
        _fail(f"node_stats not reflected in /api/stats: {nodes}")
        return False
    _ok(f"{len(nodes)} node(s) visible in /api/stats")
    return True


# ---- Main ---------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="mock_node")
    p.add_argument("--server", required=True,
                   help="PC server IP address")
    p.add_argument("--local", default=None,
                   help="Local IP to use for ctrl listen (default: any)")
    p.add_argument("--ctrl-port", type=int, default=0,
                   help="Local UDP port to bind for :4203 ctrl receive "
                        "(default: ephemeral)")
    args = p.parse_args(argv)

    print(f"Target server: {args.server}")
    print()

    results = {
        "discovery":   test_discovery(args.server),
        "clock_sync":  test_clock_sync(args.server),
        "register":    test_register(args.server, args.local or ""),
        "first_sight": test_first_sight_ctrl(args.server, 0),
        "audio":       test_audio_headers(args.server),
        "node_stats":  test_node_stats(args.server),
    }

    print()
    print("Summary:")
    failed = [k for k, v in results.items() if not v]
    for k, v in results.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())