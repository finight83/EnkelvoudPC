"""
enkelvoud.zone_ctl - zone CRUD, persistence, and HTTP handler helpers.

Mirrors EVDZones.h. Each handler returns (status_code, body_dict).

Persistence: JSON file, not the ESP32's pipe-delimited NVS string. The
in-memory Zone dataclass and its JSON round-trip are exact.
"""

import json
import os
import re
from typing import List, Optional, Tuple

from . import constants as C
from . import ctrl
from .state import runtime, now_ms, Zone
from .log_ring import log_info, log_warn


_zones_path: Optional[str] = None
_ALNUM = re.compile(r"^[A-Za-z0-9]+$")


def set_persistence_path(path: str) -> None:
    global _zones_path
    _zones_path = path


# ---- Persistence --------------------------------------------------------

def load_zones_from_disk() -> None:
    if _zones_path is None or not os.path.exists(_zones_path):
        return
    try:
        with open(_zones_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, list):
        return
    loaded: List[Zone] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        zid = str(entry.get("id", "")).strip()
        name = str(entry.get("name", "")).strip()
        if not zid or not name:
            continue
        try:
            vol = int(entry.get("volume", 100))
            vol = max(0, min(100, vol))
        except (TypeError, ValueError):
            vol = 100
        muted = bool(entry.get("muted", False))
        quiet = bool(entry.get("quiet", False))
        node_ips = [str(ip) for ip in entry.get("node_ips", [])
                    if isinstance(ip, str)]
        loaded.append(Zone(id=zid, name=name, volume=vol,
                           muted=muted, quiet=quiet, node_ips=node_ips))

    has_main = any(z.id == C.MAIN_ZONE_ID for z in loaded)
    if not has_main:
        loaded.insert(0, Zone(id=C.MAIN_ZONE_ID, name="Main Room",
                              volume=100, muted=False, node_ips=[]))

    with runtime.zones_lock:
        runtime.zones = loaded


def save_zones_to_disk() -> None:
    if _zones_path is None:
        return
    try:
        os.makedirs(os.path.dirname(_zones_path), exist_ok=True)
        with runtime.zones_lock:
            data = [
                {
                    "id": z.id, "name": z.name, "volume": z.volume,
                    "muted": z.muted, "quiet": z.quiet,
                    "node_ips": list(z.node_ips),
                }
                for z in runtime.zones
            ]
        tmp = _zones_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _zones_path)
    except OSError as e:
        log_warn("zones: save failed: %s", e)


# ---- Broadcast helpers (EVDPlayers.h broadcastNodeConfig) --------------

def broadcast_node_config() -> None:
    """Send a host_audio_sync (broadcast form) to every subscriber."""
    subs = runtime.snapshot_subscribers(now_ms())
    zones = runtime.snapshot_zones()

    main_vol = 100
    main_mute = False
    for z in zones:
        if z.id == C.MAIN_ZONE_ID:
            main_vol = z.volume
            main_mute = z.muted
            break

    for s in subs:
        zvol, zmute = main_vol, main_mute
        for z in zones:
            if s.ip in z.node_ips:
                zvol = z.volume
                zmute = z.muted
                break
        ctrl.send_host_audio_sync_broadcast(
            s.ip,
            s.negotiated_audio_protocol_version,
            s.audio_compatible,
            zvol, zmute,
        )


# ---- Helpers ------------------------------------------------------------

def _make_zone_id() -> Optional[str]:
    base = (now_ms() ^ runtime.server_epoch) & 0xFFFFFFFF
    with runtime.zones_lock:
        for n in range(1000):
            candidate = f"z{(base + n) & 0xFFFFFFFF:x}"
            if not any(z.id == candidate for z in runtime.zones):
                return candidate
    return None


def _snapshot_players():
    return runtime.snapshot_players(now_ms())


# ---- Handler: /api/zones/create -----------------------------------------

def create_zone(data: dict) -> Tuple[int, dict]:
    name = data.get("name", "")
    if not isinstance(name, str) or not (1 <= len(name) <= 24):
        return 400, {"ok": False,
                     "error": "zone name must be 1-24 alphanumeric characters"}
    if not _ALNUM.match(name):
        return 400, {"ok": False,
                     "error": "zone name must contain only letters and digits"}

    new_id = _make_zone_id()
    if new_id is None:
        return 503, {"ok": False,
                     "error": "could not allocate a unique zone id"}

    with runtime.zones_lock:
        runtime.zones.append(Zone(id=new_id, name=name, volume=100,
                                  muted=False, node_ips=[]))
    save_zones_to_disk()
    broadcast_node_config()
    log_info("Zone: created %s (%s)", name, new_id)
    return 200, {"ok": True, "id": new_id}


# ---- Handler: /api/zones/delete -----------------------------------------

def delete_zone(data: dict) -> Tuple[int, dict]:
    zone_id = data.get("zoneId", "")
    if not isinstance(zone_id, str) or zone_id == "":
        return 400, {"ok": False}
    if zone_id == C.MAIN_ZONE_ID:
        return 400, {"ok": False, "error": "cannot delete main"}

    moved_ips: List[str] = []
    main_vol = 100
    main_mute = False
    with runtime.zones_lock:
        # Move nodes to main first.
        for z in list(runtime.zones):
            if z.id == zone_id:
                for ip in z.node_ips:
                    moved_ips.append(ip)
                    for m in runtime.zones:
                        if m.id == C.MAIN_ZONE_ID and ip not in m.node_ips:
                            m.node_ips.append(ip)
                runtime.zones.remove(z)
                break
        for m in runtime.zones:
            if m.id == C.MAIN_ZONE_ID:
                main_vol = m.volume
                main_mute = m.muted
                break

    save_zones_to_disk()

    for ip in moved_ips:
        ctrl.send_zone_config_to_ip(ip, main_vol, main_mute)
        runtime.remove_node_perf(ip)

    broadcast_node_config()
    log_info("Zone: deleted %s (moved %d nodes to main)",
             zone_id, len(moved_ips))
    return 200, {"ok": True}


# ---- Handler: /api/zones/move -------------------------------------------

def move_node(data: dict) -> Tuple[int, dict]:
    ip = data.get("ip", "")
    zone_id = data.get("zoneId", "")
    if not isinstance(ip, str) or not ip:
        return 400, {"ok": False, "error": "ip required"}
    try:
        import ipaddress
        ipaddress.IPv4Address(ip)
    except ValueError:
        return 400, {"ok": False, "error": "ip required"}

    new_vol = 100
    new_mute = False
    with runtime.zones_lock:
        target_exists = any(z.id == zone_id for z in runtime.zones)
        if not target_exists:
            return 400, {"ok": False, "error": "unknown zone"}

        for z in runtime.zones:
            if ip in z.node_ips:
                z.node_ips.remove(ip)

        for z in runtime.zones:
            if z.id == zone_id:
                z.node_ips.append(ip)
                new_vol = z.volume
                new_mute = z.muted
                break

    save_zones_to_disk()
    ctrl.send_zone_config_to_ip(ip, new_vol, new_mute)
    broadcast_node_config()
    return 200, {"ok": True}


# ---- Handler: /api/zones/config -----------------------------------------

def configure_zone(data: dict) -> Tuple[int, dict]:
    zone_id = data.get("zoneId", "")
    if not isinstance(zone_id, str) or not zone_id:
        return 400, {"ok": False}

    target_ips: List[str] = []
    new_vol = 100
    new_mute = False
    ok = False

    with runtime.zones_lock:
        for z in runtime.zones:
            if z.id != zone_id:
                continue

            if "name" in data and data["name"] is not None:
                n = data["name"]
                if not isinstance(n, str) or not (1 <= len(n) <= 24):
                    return 400, {"ok": False,
                                 "error": "zone name must be 1-24 alphanumeric characters"}
                if not _ALNUM.match(n):
                    return 400, {"ok": False,
                                 "error": "zone name must contain only letters and digits"}
                z.name = n

            if "volume" in data and data["volume"] is not None:
                try:
                    v = int(data["volume"])
                    z.volume = max(0, min(100, v))
                except (TypeError, ValueError):
                    pass

            if "muted" in data and data["muted"] is not None:
                z.muted = bool(data["muted"])

            if "quiet" in data and data["quiet"] is not None:
                z.quiet = bool(data["quiet"])

            target_ips = list(z.node_ips)
            new_vol = z.volume
            new_mute = z.muted
            ok = True
            break

    if not ok:
        return 400, {"ok": False, "error": "unknown zone"}

    save_zones_to_disk()
    for ip in target_ips:
        ctrl.send_zone_config_to_ip(ip, new_vol, new_mute)
    broadcast_node_config()
    return 200, {"ok": True}


# ---- Handler: /api/zones/remove-node -----------------------------------

def remove_node(data: dict) -> Tuple[int, dict]:
    zone_id = data.get("zoneId", "")
    ip = data.get("ip", "")
    if not isinstance(zone_id, str) or not zone_id:
        return 400, {"ok": False, "error": "zoneId and ip required"}
    if not isinstance(ip, str) or not ip:
        return 400, {"ok": False, "error": "zoneId and ip required"}
    try:
        import ipaddress
        ipaddress.IPv4Address(ip)
    except ValueError:
        return 400, {"ok": False, "error": "zoneId and ip required"}

    removed = False
    with runtime.zones_lock:
        for z in runtime.zones:
            if z.id != zone_id:
                continue
            if ip in z.node_ips:
                z.node_ips.remove(ip)
                removed = True
            break

    if not removed:
        return 404, {"ok": False, "error": "node not in zone"}

    save_zones_to_disk()
    runtime.remove_node_perf(ip)
    broadcast_node_config()
    return 200, {"ok": True}