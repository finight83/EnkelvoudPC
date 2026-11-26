"""
enkelvoud.registry - player registration orchestration and zone resolution.

Pure data / orchestration on top of state.Runtime. Sends no HTTP responses
itself; that is web_api.py's job. This module:
  - handles the full /api/player/register backend flow
  - resolves zone state for an IP
  - leaves a new node UNASSIGNED (the Discovered pool) until the operator
    drags it into a room; unassigned nodes are muted (no audio)
"""

from typing import Tuple

from . import constants as C
from . import ctrl
from .log_ring import log_info
from .state import (
    runtime,
    now_ms,
    PlayerNode,
    evaluate_audio_compatibility,
)


# ---- Zone resolution (EVDPlayers.h resolve blocks) -----------------------

def resolve_zone_state_for_ip(ip: str) -> Tuple[int, bool]:
    """Return (volume, muted) of the zone containing ip, else Main's state."""
    zones = runtime.snapshot_zones()
    for z in zones:
        if ip in z.node_ips:
            return z.volume, z.muted
    for z in zones:
        if z.id == C.MAIN_ZONE_ID:
            return z.volume, z.muted
    return 100, False


# ---- Player registration orchestration -----------------------------------

def handle_player_registration(
    ip: str, name: str, version: str, declared_proto: int
) -> Tuple[bool, Tuple[bool, int, str], int, bool]:
    """Core backend for POST /api/player/register.

    Returns:
        is_new      : True if this is the first time we've seen this IP
        decision    : (compatible, negotiated_protocol_version, reason)
        zvol        : this node's current zone volume (0..100)
        zmute       : this node's current zone mute

    Order of operations matches EVDPlayers.h handlePlayerRegistration():

      1. Update playerNodes
      2. Update subscribers (findOrCreate -- clears failures, keeps clock-sync)
      3. New nodes stay unassigned (Discovered pool) until the operator
         drags them into a room; they are muted until then
      4. Resolve zone state
      5. If new: send host_audio_sync (first-sight form)
      6. If new: send time_sync   (first-sight form, no authoritative)
    """
    compatible, negotiated_ver, reason = evaluate_audio_compatibility(
        declared_proto, C.AUDIO_PROTO_VERSION
    )
    decision = (compatible, negotiated_ver, reason)
    now = now_ms()

    # ---- 1. Update playerNodes --------------------------------------------
    is_new = False
    with runtime.players_lock:
        runtime.prune_stale_players_locked(now)
        found = False
        for p in runtime.player_nodes:
            if p.ip == ip:
                p.name = name
                p.version = version
                p.last_seen_ms = now
                p.declared_audio_protocol_version = declared_proto
                p.negotiated_audio_protocol_version = negotiated_ver
                p.audio_compatible = compatible
                p.audio_compat_reason = reason
                found = True
                break
        if not found and len(runtime.player_nodes) < C.MAX_UDP_SUBSCRIBERS * 2:
            runtime.player_nodes.append(PlayerNode(
                name=name, ip=ip, version=version, last_seen_ms=now,
                declared_audio_protocol_version=declared_proto,
                negotiated_audio_protocol_version=negotiated_ver,
                audio_compatible=compatible, audio_compat_reason=reason,
            ))
            is_new = True

    # ---- 2. Update subscribers --------------------------------------------
    with runtime.subs_lock:
        slot = runtime.find_or_create_subscriber_locked(
            ip, name, declared_proto, negotiated_ver, compatible
        )
        if slot >= 0:
            # Heartbeat upgrades any provisional classification.
            runtime.subscribers[slot].provisional = False
            if is_new:
                runtime.subscribers[slot].last_seen_ms = now

    # ---- 3. Do NOT auto-assign: new nodes land in the Discovered pool ----
    # The operator drags a node into a room on the dashboard; until then
    # AudioSender._fanout sends it no audio (muted). This makes "first join"
    # explicit instead of every new node immediately playing in Main Room.

    # ---- 4. Resolve current zone state -----------------------------------
    zvol, zmute = resolve_zone_state_for_ip(ip)

    # ---- 5 & 6. First-sight pushes ---------------------------------------
    if is_new:
        log_info("Player joined: %s (%s) v%s%s", name, ip, version or "?",
                 "" if compatible else " - audio incompatible: " + reason)
        ctrl.send_host_audio_sync_first_sight(ip, decision, zvol, zmute)
        ctrl.send_time_sync_first_sight(ip)
    # else: no per-heartbeat UDP push; the HTTP response carries the state.

    return is_new, decision, zvol, zmute