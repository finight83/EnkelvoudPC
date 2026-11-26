"""
enkelvoud.ota - player firmware updates pushed from the PC server.

Why this exists
---------------
The ESP32 server ships a firmware spool plus an OTA wizard (EVDOTA.h); the PC
port lost all of it, so the Update tab only had prose. This module puts the
useful half back: keep .bin files in a spool folder, and push one to the nodes
the operator ticked.

What each party expects (verified against EnkelvoudESPPlayer/EVDPWeb.h and
EnkelvoudServer/EVDOTA.h:266-431):

  PC -> node   POST http://<node>:80/api/ota
               multipart/form-data, one part `name="fw"` with the .bin bytes,
               a real Content-Length and a closing boundary. The node streams
               it into its inactive OTA partition and reboots.
  node -> PC   POST /api/player/ota_result {"name": "...", "success": true}
               (informational; the version in /api/state is the real proof)

So a push is plain HTTP over the LAN - no UDP, no ESP-NOW, no chunk protocol.
The node's WebServer needs the whole body and a closing boundary before it
answers, hence the 8 s reply wait and the retry loop.

Server-side version updates are deliberately not implemented: a PC app is
updated by replacing its files, exactly as the old dashboard said.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import tempfile
import threading
from typing import Dict, List, Optional, Tuple

from . import constants as C
from .log_ring import log_error, log_info, log_warn
from .state import now_ms, runtime

# ---- Wire constants (mirror EVDOTA.h / EVDPWeb.h) -------------------------
BOUNDARY = "----EVDOTA"          # EVDOTA.h:290
NODE_OTA_PORT = 80               # EVDOTA.h:309 (hard-coded in the reference)
CHUNK = 512                      # EVDOTA.h:300
CONNECT_TIMEOUT_S = 2.0          # EVDOTA.h:305
WRITE_TIMEOUT_S = 60.0
REPLY_TIMEOUT_S = 8.0            # EVDOTA.h:401
ATTEMPTS = 3                     # EVDOTA.h:302
RETRY_DELAY_S = 1.0
SETTLE_S = 1.5                   # EVDOTA.h:926 - let mDNS settle first
BETWEEN_NODES_S = 0.5            # EVDOTA.h:989
MAX_NAME_LEN = 80                # EVDOTA.h:1422
# ESP32 app partitions are ~2 MB; 8 MiB leaves room without letting a LAN
# client fill the disk through /api/ota/upload.
MAX_FW_BYTES = 8 * 1024 * 1024

# `node_3.8.1.2.bin` - kind, then the version the dashboard verifies against.
_NAME_RE = re.compile(r"^(server|node|source)_([0-9A-Za-z._-]{1,32})\.bin$")

# ESP32 application images start with the 0xE9 image magic and carry an
# esp_app_desc_t (magic 0xABCD5432, little-endian) at offset 32. A bootloader
# also starts with 0xE9 but is ~20 KB and has no app descriptor, and a
# partition table does not start with 0xE9 at all. Pushing either made every
# node answer "commit_failed" -- the node wrote it and then refused to commit.
_APP_DESC_MAGIC = b"\x32\x54\xCD\xAB"
_MIN_APP_BYTES = 256 * 1024


def validate_app_image(path: str) -> Optional[str]:
    """Return an error string if `path` is not a plausible ESP32 app image."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError as e:
        return f"cannot read image: {e}"
    if size < _MIN_APP_BYTES:
        return (f"not an application image ({size} bytes): this looks like a "
                f"bootloader/partitions file; select the sketch's .ino.bin")
    if len(head) < 36 or head[0] != 0xE9:
        return "not an ESP32 image (bad magic)"
    if head[32:36] != _APP_DESC_MAGIC:
        return ("not an ESP32 application image (no app descriptor): this is "
                "a bootloader or a partition table, not the sketch's .ino.bin")
    return None

_spool_dir: Optional[str] = None
_lock = threading.Lock()

# One job at a time, mirroring the ESP32's global otaInProgress.
_job: Dict = {
    "inProgress": False,
    "target": "",
    "progressPct": 0,
    "completed": [],
    "failed": [],
    "lastError": "",
    "startedMs": 0,
    "lastProgressMs": 0,
    "file": "",
}


# ---- Spool ---------------------------------------------------------------

def set_spool_dir(path: str) -> None:
    """Called once at startup with <data-dir>/ota_spool."""
    global _spool_dir
    _spool_dir = path
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        log_warn("OTA: cannot create spool %s: %s", path, e)


def parse_fw_name(name: str) -> Optional[Dict]:
    """`node_3.8.1.2.bin` -> {'name','kind','version'}, else None."""
    if not isinstance(name, str) or not name or len(name) > MAX_NAME_LEN:
        return None
    if "/" in name or "\\" in name or ".." in name:
        return None
    m = _NAME_RE.match(name)
    if not m:
        return None
    return {"name": name, "kind": m.group(1), "version": m.group(2)}


def _version_key(version: str):
    """Sort key for dotted versions: '3.10.1' > '3.9.9' (not string order)."""
    return [(0, int(p)) if p.isdigit() else (1, p)
            for p in re.split(r"[._-]", version or "")]


def scan_spool() -> List[Dict]:
    """Every valid firmware file, newest version first per kind."""
    if not _spool_dir or not os.path.isdir(_spool_dir):
        return []
    out = []
    try:
        for entry in os.listdir(_spool_dir):
            parsed = parse_fw_name(entry)
            if not parsed:
                continue
            path = os.path.join(_spool_dir, entry)
            if not os.path.isfile(path):
                continue
            parsed["size"] = os.path.getsize(path)
            out.append(parsed)
    except OSError as e:
        log_warn("OTA: spool scan failed: %s", e)
        return []
    # Newest version first within each kind: the default pick must be the
    # newest image, not whichever file happens to be largest.
    out.sort(key=lambda f: (f["kind"], _version_key(f["version"])), reverse=True)
    return out


# ---- Nodes ---------------------------------------------------------------

def _nodes_for_plan() -> List[Dict]:
    """Registered players, as the tick list the dashboard renders."""
    out = []
    try:
        players = runtime.snapshot_players(now_ms())
        assigned = runtime.assigned_node_ips()
    except Exception:
        players, assigned = [], set()
    for p in players:
        out.append({
            "ip": p.ip,
            "name": p.name,
            "version": p.version or "unknown",
            "reachable": True,
            "assigned": p.ip in assigned,
        })
    out.sort(key=lambda n: n["ip"])
    return out


# ---- Status / plan -------------------------------------------------------

def status() -> Dict:
    with _lock:
        job = dict(_job)
    return {
        "ok": True,
        "inProgress": job["inProgress"],
        "progressPct": job["progressPct"],
        "target": job["target"],
        "error": job["lastError"],
        "completed": list(job["completed"]),
        "failed": list(job["failed"]),
        "file": job["file"],
    }


def plan() -> Dict:
    files = scan_spool()
    with _lock:
        busy = _job["inProgress"]
        job = dict(_job)
    node_bins = [f for f in files if f["kind"] == "node"]
    return {
        "ok": True,
        "serverVersion": C.VERSION,
        "inProgress": busy,
        "progressPct": job["progressPct"],
        "lastTarget": job["target"],
        "lastError": job["lastError"],
        "files": files,
        "nodeFiles": node_bins,
        "nodes": _nodes_for_plan(),
    }


# ---- Push ----------------------------------------------------------------

def _multipart_head(filename: str, size: int) -> bytes:
    return (
        "--" + BOUNDARY + "\r\n"
        'Content-Disposition: form-data; name="fw"; filename="' + filename + '"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()


def _multipart_tail() -> bytes:
    return ("\r\n--" + BOUNDARY + "--\r\n").encode()


def push_to_node(ip: str, path: str, filename: str,
                 progress_cb=None) -> Tuple[bool, str]:
    """POST one firmware file to a node. Returns (ok, detail)."""
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return False, f"cannot read {filename}: {e}"

    head = _multipart_head(filename, size)
    tail = _multipart_tail()
    request = (
        "POST /api/ota HTTP/1.1\r\n"
        f"Host: {ip}\r\n"
        "Content-Type: multipart/form-data; boundary=" + BOUNDARY + "\r\n"
        f"Content-Length: {len(head) + size + len(tail)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()

    sock = None
    try:
        sock = socket.create_connection((ip, NODE_OTA_PORT), timeout=CONNECT_TIMEOUT_S)
        # Headers + 512-byte writes: with Nagle on, each write can wait for a
        # delayed ACK from the ESP32 and the push crawls (the ESP32 server sets
        # setNoDelay(true) for the same reason).
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(WRITE_TIMEOUT_S)
        sock.sendall(request + head)
        sent = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                sock.sendall(chunk)          # node writes one Update.write per chunk
                sent += len(chunk)
                if progress_cb:
                    progress_cb(sent, size)
        sock.sendall(tail)

        sock.settimeout(REPLY_TIMEOUT_S)     # node commits, then answers
        reply = b""
        while True:
            try:
                part = sock.recv(4096)
            except socket.timeout:
                break
            if not part:
                break
            reply += part
            if b"\r\n\r\n" in reply and (b'"ok"' in reply or b"status" in reply):
                break
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    if b'"ok":true' in reply.replace(b" ", b""):
        return True, "commit accepted"
    if not reply:
        return False, "node did not reply (timeout after upload)"
    return False, "node rejected the upload: " + reply[:160].decode("latin-1", "replace").replace("\r\n", " ")


def _job_thread(node_ips: List[str], path: str, filename: str) -> None:
    """Run one push job; the busy flag is cleared whatever happens."""
    try:
        _run_job(node_ips, path, filename)
    finally:
        with _lock:
            _job["inProgress"] = False
            _job["target"] = ""
            _job["progressPct"] = 0
        log_info("OTA: done - %d updated, %d failed",
                 len(_job["completed"]), len(_job["failed"]))


def _run_job(node_ips: List[str], path: str, filename: str) -> None:
    total = len(node_ips)
    log_info("OTA: pushing %s to %d node(s)", filename, total)
    time_sleep = _sleep
    time_sleep(SETTLE_S)
    for idx, ip in enumerate(node_ips):
        with _lock:
            _job["target"] = ip
            _job["progressPct"] = 0
            _job["lastProgressMs"] = now_ms()
        ok = False
        detail = ""
        for attempt in range(1, ATTEMPTS + 1):
            def cb(sent: int, size: int) -> None:
                with _lock:
                    _job["progressPct"] = int(sent * 100 / size) if size else 100
                    _job["lastProgressMs"] = now_ms()
            try:
                ok, detail = push_to_node(ip, path, filename, cb)
            except Exception as e:      # one bad node must not kill the job
                ok, detail = False, f"{type(e).__name__}: {e}"
            if ok:
                break
            log_warn("OTA: %s attempt %d/%d failed: %s", ip, attempt, ATTEMPTS, detail)
            if attempt < ATTEMPTS:
                time_sleep(RETRY_DELAY_S * (attempt + 1))
        with _lock:
            if ok:
                _job["completed"].append(ip)
                _job["progressPct"] = 100
            else:
                _job["failed"].append(ip)
                _job["lastError"] = f"{ip}: {detail}"
        if ok:
            log_info("OTA: %s accepted %s", ip, filename)
        else:
            log_error("OTA: %s failed: %s", ip, detail)
        if idx + 1 < total:
            time_sleep(BETWEEN_NODES_S)


def _sleep(seconds: float) -> None:
    threading.Event().wait(seconds)


# ---- Operations ----------------------------------------------------------

def start_job(node_ips: List[str], file_name: str) -> Tuple[int, Dict]:
    """POST /api/ota/apply - {nodes:[ip...], nodeFile:'node_x.bin'}."""
    if not _spool_dir:
        return 503, {"ok": False, "error": "OTA spool unavailable"}
    parsed = parse_fw_name(file_name)
    if not parsed:
        return 400, {"ok": False, "error": "invalid firmware filename"}
    if parsed["kind"] != "node":
        # A server_/source_ image in a player's OTA partition bricks it until
        # USB reflash; the ESP32 server only ever picks kind == "node".
        return 400, {"ok": False,
                     "error": "only node_<version>.bin can be pushed to players"}
    path = os.path.join(_spool_dir, file_name)
    if not os.path.isfile(path):
        return 400, {"ok": False, "error": "selected node bin not in spool"}
    bad = validate_app_image(path)
    if bad:
        return 400, {"ok": False, "error": bad}

    now = now_ms()
    try:
        alive = {p.ip for p in runtime.snapshot_players(now)
                 if now - p.last_seen_ms <= C.PLAYER_STALE_TIMEOUT_MS}
    except Exception:
        alive = set()

    ips = []
    for ip in node_ips if isinstance(node_ips, list) else []:
        if not isinstance(ip, str) or ip in ips:
            continue
        try:
            ipaddress.IPv4Address(ip)      # never dial arbitrary hosts
        except ValueError:
            log_warn("OTA: ignoring invalid node address %r", ip)
            continue
        if ip not in alive:
            log_warn("OTA: skipping %s (not a registered player with a fresh heartbeat)", ip)
            continue
        ips.append(ip)
    if not ips:
        return 400, {"ok": False,
                     "error": "no reachable player nodes selected"}

    with _lock:
        if _job["inProgress"]:
            return 409, {"ok": False, "error": "update already running"}
        _job.update({
            "inProgress": True,
            "target": "",
            "progressPct": 0,
            "completed": [],
            "failed": [],
            "lastError": "",
            "startedMs": now_ms(),
            "lastProgressMs": now_ms(),
            "file": file_name,
        })

    threading.Thread(target=_job_thread, args=(ips, path, file_name),
                     name="ota-job", daemon=True).start()
    return 202, {"ok": True, "state": "running", "nodes": ips, "file": file_name}


def save_upload(storage) -> Tuple[int, Dict]:
    """POST /api/ota/upload - one multipart file part."""
    if not _spool_dir:
        return 503, {"ok": False, "error": "OTA spool unavailable"}
    name = os.path.basename(storage.filename or "")
    parsed = parse_fw_name(name)
    if not parsed or parsed["kind"] != "node":
        return 400, {"ok": False,
                     "error": "filename must look like node_<version>.bin"}
    with _lock:
        if _job["inProgress"]:
            return 409, {"ok": False, "error": "update already running"}
    path = os.path.join(_spool_dir, name)
    # Unique temp per upload: two clients uploading the same name must not
    # interleave bytes into one shared .part file.
    fd, tmp = tempfile.mkstemp(dir=_spool_dir, prefix=name + ".", suffix=".part")
    os.close(fd)
    if storage.content_length and storage.content_length > MAX_FW_BYTES:
        return 413, {"ok": False, "error": "firmware too large"}
    try:
        storage.save(tmp)
        size = os.path.getsize(tmp)
        if size > MAX_FW_BYTES:
            os.remove(tmp)
            return 413, {"ok": False, "error": "firmware too large"}
        bad = validate_app_image(tmp)
        if bad:
            os.remove(tmp)
            return 400, {"ok": False, "error": bad}
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return 500, {"ok": False, "error": f"could not store firmware: {e}"}
    log_info("OTA: uploaded %s (%d bytes)", name, size)
    return 200, {"ok": True, "name": name, "size": size,
                 "version": parse_fw_name(name)["version"]}


def delete_file(file_name: str) -> Tuple[int, Dict]:
    if not _spool_dir:
        return 503, {"ok": False, "error": "OTA spool unavailable"}
    if not parse_fw_name(file_name or ""):
        return 400, {"ok": False, "error": "invalid filename"}
    with _lock:
        if _job["inProgress"]:
            return 409, {"ok": False, "error": "update already running"}
    path = os.path.join(_spool_dir, file_name)
    if not os.path.isfile(path):
        return 404, {"ok": False, "error": "file not found"}
    try:
        os.remove(path)
    except OSError as e:
        return 500, {"ok": False, "error": f"delete failed: {e}"}
    return 200, {"ok": True}


def note_result(data: Dict) -> None:
    """POST /api/player/ota_result - informational only."""
    name = data.get("name") if isinstance(data, dict) else None
    success = data.get("success") if isinstance(data, dict) else None
    if isinstance(name, str) and success is not None:
        log_info("OTA result: %s success=%s", name, bool(success))
