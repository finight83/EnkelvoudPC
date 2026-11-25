"""
enkelvoud.state - runtime mutable state, thread locks, and clock.

All types mirror the C++ structs verbatim:

  Subscriber   ↔ EVDState.h struct Subscriber
  PlayerNode   ↔ EVDState.h struct PlayerNode
  Zone         ↔ EVDState.h struct Zone
  NodePerf     ↔ EVDState.h struct NodePerf

The global `runtime` object is a lock-guarded container for the
process-wide mutable state, playing the role of the C++ globals in
EVDState.h. Every method that reads or writes more than one field takes
the corresponding RLock, mirroring the FreeRTOS mutex usage in
EVDHelpers.h.
"""

import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import constants as C


# ---- Monotonic clock -------------------------------------------------------
# Matches the semantics of ESP32 `millis()` in everything the code depends on:
# monotonic, never decreasing, and safe to subtract from another `now_ms()`.

_T0_NS = time.monotonic_ns()


def now_ms() -> int:
    """Milliseconds since process start. Monotonic. Matches C++ millis()."""
    return (time.monotonic_ns() - _T0_NS) // 1_000_000


def wire_timestamp_ms() -> int:
    """32-bit millisecond timestamp for the on-wire audio header.

    Matches `millis()` masked to 32 bits (the ESP32 uint32_t semantics).
    Safe: nothing depends on the wrap for <49 days of uptime.
    """
    return now_ms() & 0xFFFFFFFF


# ---- POD types (verbatim translation) -------------------------------------

@dataclass
class Subscriber:
    ip: str
    name: str
    last_seen_ms: int
    consecutive_send_failures: int = 0
    declared_audio_protocol_version: int = -1
    negotiated_audio_protocol_version: int = 0
    audio_compatible: bool = False
    has_completed_clock_sync: bool = False
    provisional: bool = False


@dataclass
class PlayerNode:
    name: str
    ip: str
    version: str
    last_seen_ms: int
    declared_audio_protocol_version: int = -1
    negotiated_audio_protocol_version: int = 0
    audio_compatible: bool = False
    audio_compat_reason: str = "no_protocol_declared"


@dataclass
class Zone:
    id: str
    name: str
    volume: int = 100
    muted: bool = False
    node_ips: List[str] = field(default_factory=list)


@dataclass
class NodePerf:
    ip: str
    latency_ms: int
    queue_depth: int
    target: int
    rebuffers: int
    updated_ms: int
    has_data: bool = True


@dataclass
class SubscriberSnap:
    """EVDHelpers.h struct SubscriberSnap, plus the send-path failure count.

    The C++ struct does not carry `consecutiveSendFailures`: udpSendTask()
    copies it into its own local `SubEntry` (EVDUDP.h) while holding
    subsMutex, and gates on `snapBuf[si].failures`. The Python sender has a
    single fan-out loop over this snapshot, so the counter travels with the
    snapshot instead of in a second struct. The audio gate in
    AudioSender._fanout() reads it, and the reset/increment path re-reads
    the live table under subs_lock.
    """
    ip: str
    name: str
    last_seen_ms: int
    consecutive_send_failures: int
    declared_audio_protocol_version: int
    negotiated_audio_protocol_version: int
    audio_compatible: bool
    has_completed_clock_sync: bool
    provisional: bool


@dataclass
class EncodeSettings:
    """Mirrors EVDState.h struct ModeEncSettings."""
    bitrate: int
    complexity: int
    vbr: bool
    vbr_constraint: bool
    signal: int
    application: int
    sample_rate: int
    buffer_ms: int

    def to_dict(self) -> dict:
        return {
            "bitrate": self.bitrate,
            "complexity": self.complexity,
            "vbr": self.vbr,
            "vbr_constraint": self.vbr_constraint,
            "signal": self.signal,
            "application": self.application,
            "sample_rate": self.sample_rate,
            "buffer_ms": self.buffer_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EncodeSettings":
        return cls(
            bitrate=int(d["bitrate"]),
            complexity=int(d["complexity"]),
            vbr=bool(d["vbr"]),
            vbr_constraint=bool(d["vbr_constraint"]),
            signal=int(d["signal"]),
            application=int(d["application"]),
            sample_rate=int(d["sample_rate"]),
            buffer_ms=int(d["buffer_ms"]),
        )


def _clamp_encode_settings(s: EncodeSettings) -> EncodeSettings:
    """Mirror loadEncoderModeSettings() range clamps from EVDWiFi.h."""
    if s.bitrate < 6000:
        s.bitrate = 6000
    if s.bitrate > 192000:
        s.bitrate = 192000
    if s.complexity < 0:
        s.complexity = 0
    if s.complexity > 10:
        s.complexity = 10
    if s.signal not in (C.OPUS_SIGNAL_MUSIC, C.OPUS_SIGNAL_VOICE, C.OPUS_AUTO):
        s.signal = C.OPUS_SIGNAL_MUSIC
    if s.application not in (
        C.OPUS_APPLICATION_AUDIO,
        C.OPUS_APPLICATION_VOIP,
        C.OPUS_APPLICATION_RESTRICTED_LOWDELAY,
    ):
        s.application = C.OPUS_APPLICATION_AUDIO
    if s.sample_rate not in (C.OPUS_SAMPLE_RATE_48K, C.OPUS_SAMPLE_RATE_24K):
        s.sample_rate = C.OPUS_SAMPLE_RATE_48K
    if s.buffer_ms < C.AUDIO_BUFFER_MIN_MS:
        s.buffer_ms = C.AUDIO_BUFFER_MIN_MS
    if s.buffer_ms > C.AUDIO_BUFFER_MAX_MS:
        s.buffer_ms = C.AUDIO_BUFFER_MAX_MS
    return s


# ---- Sanitizers (verbatim from EVDHelpers.h) ------------------------------

def sanitize_server_name(value: str) -> str:
    """Keep [A-Za-z0-9] only; fallback 'Enkelvoud'."""
    out = "".join(c for c in value if c.isalnum() and c.isascii())
    return out if out else "Enkelvoud"


def sanitize_version(value: str) -> str:
    """Keep [0-9A-Za-z._-], max 32 chars."""
    allowed = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz._-")
    out = "".join(c for c in value if c in allowed)[:32]
    return out


# ---- Runtime container ----------------------------------------------------

class Runtime:
    """Process-wide mutable state.

    Every table has its own RLock. Mutex naming matches the C++ names:
      subs_lock    ↔ subsMutex
      players_lock ↔ playersMutex
      zones_lock   ↔ zonesMutex
      perf_lock    ↔ perfMutex
      uart_lock    ↔ uartMutex     (unused on PC, present for parity)
      ota_lock     ↔ otaMutex      (unused on PC, always unlocked)
    """

    def __init__(self) -> None:
        self.subs_lock    = threading.RLock()
        self.players_lock = threading.RLock()
        self.zones_lock   = threading.RLock()
        self.perf_lock    = threading.RLock()
        self.uart_lock    = threading.RLock()
        self.ota_lock     = threading.RLock()
        self.mode_settings_lock = threading.RLock()

        # ---- Subscriber table -------------------------------------------
        self.subscribers: List[Subscriber] = []
        self.subscriber_count: int = 0

        # ---- Player table ----------------------------------------------
        self.player_nodes: List[PlayerNode] = []

        # ---- Zone table -----------------------------------------------
        self.zones: List[Zone] = [Zone(
            id=C.MAIN_ZONE_ID, name="Main Room", volume=100, muted=False, node_ips=[]
        )]

        # ---- Node perf table ------------------------------------------
        self.node_perf: List[NodePerf] = []

        # ---- Server / audio settings ----------------------------------
        self.server_name: str = C.DEFAULT_SERVER_NAME
        self.master_volume: int = C.DEFAULT_MASTER_VOLUME
        self.master_muted: bool = C.DEFAULT_MASTER_MUTED
        self.quiet_mode: bool = False
        self.latency_adjustment_ms: int = 0
        self.audio_buffer_ms: int = C.MODE_ENCODE_DEFAULTS[C.MODE_OFF]["buffer_ms"]
        self.log_level: int = 3              # NEW: /api/log/level
        self.dlna_enabled: bool = False      # NEW: /api/dlna flag (stub)
        # Other Enkelvoud servers answering discovery on this LAN (see
        # discovery.probe_other_servers). Non-empty means nodes are split
        # between servers; surfaced in /api/stats and the Status tab.
        self.competing_servers: List[dict] = []
        # [servers-decide] True when this server is the elected leader -- the
        # only one that answers node discovery and accepts registrations.
        # Single-server installs stay True; the peer election in peers.py
        # flips it when another server outranks this one.
        self.is_leader: bool = True
        # Alive peer servers from the beacon table (dashboard "Servers").
        self.peer_servers: List[dict] = []

        self.playback_mode_idx: int = C.MODE_OFF

        # ---- Encoder per-mode presets (RAM copy) ----------------------
        self.mode_settings: List[EncodeSettings] = [
            _clamp_encode_settings(EncodeSettings.from_dict(d))
            for d in C.MODE_ENCODE_DEFAULTS
        ]

        # Currently-applied encoder settings (mirrors of g_new*).
        self.new_bitrate: int = C.OPUS_BITRATE
        self.new_vbr: bool = True
        self.new_vbr_constraint: bool = True
        self.new_signal: int = C.OPUS_SIGNAL_MUSIC
        self.new_application: int = C.OPUS_APPLICATION_AUDIO

        # Currently-running encoder (mirrors of g_current*).
        self.current_bitrate: int = C.OPUS_BITRATE
        self.current_vbr: bool = True
        self.current_vbr_constraint: bool = True
        self.current_signal: int = C.OPUS_SIGNAL_MUSIC
        self.current_application: int = C.OPUS_APPLICATION_AUDIO
        self.current_complexity: int = 8   # EVDState.h default currentOpusComplexity
        self.pending_complexity: Optional[int] = None   # NEW: one-shot override
                                                        # (mirrors g_pendingComplexity;
                                                        #  consumed by encoder thread)

        self.opus_sample_rate: int = C.OPUS_SAMPLE_RATE_DEFAULT
        self.opus_frame_samples: int = C.OPUS_SAMPLE_RATE_DEFAULT // (1000 // C.OPUS_FRAME_MS)

        # ---- Session identity -----------------------------------------
        # [epoch-random-per-boot] EnkelvoudServer/EVDMain.h:237 does
        #     serverEpoch = esp_random();
        # It is an OPAQUE per-boot session id, not a timestamp: players
        # compare it against the epoch they last saw and, on a change,
        # discard their cached clock offset and audio sequence baseline
        # (EnkelvoudESPPlayer/EVDPNet.h handleServerEpoch). Unix-ms also
        # changes per boot so detection worked, but it makes the value
        # meaningful to anything that reads it, which it must not be.
        self.server_epoch: int = secrets.randbits(32) or 1

        # ---- Stats (mirrors of EVDState.h counters we actually use) --
        self.stat_udp_packets_sent: int = 0
        self.stat_udp_bytes_sent: int = 0
        self.stat_udp_errors: int = 0
        self.stat_opus_encoded: int = 0
        self.stat_opus_dropped: int = 0
        self.stat_sync_requests_served: int = 0
        self.stat_sync_request_errors: int = 0
        self.stat_sync_requests_throttled: int = 0
        self.stat_audio_incompatible_skips: int = 0
        self.stat_audio_provisional_skips: int = 0
        self.stat_audio_sync_pending: int = 0
        self.stat_latency_sum_ms: int = 0
        self.stat_latency_count: int = 0

        # udpSendTask live counters (exposed in /api/stats).
        self.udp_send_loop_count: int = 0
        self.udp_send_stall_resets: int = 0
        self.udp_pacer_ahead_reset: int = 0    # legacy, never incremented
        self.udp_pacer_behind_reset: int = 0
        self.udp_pacer_drift_reset: int = 0
        self.udp_stage_after_recv: int = 0
        self.udp_stage_after_store: int = 0

        # Window stats (reset every 15 s by the meter thread).
        self.stat_opus_dropped_last_5s: int = 0
        self.stat_opus_encoded_last_5s: int = 0
        self.stat_udp_packets_last_5s: int = 0
        self.stat_udp_bytes_last_5s: int = 0

    # ---- Accessors for parity with C++ helpers ---------------------

    def playback_mode_cstr(self) -> str:
        return C.MODE_NAMES[self.playback_mode_idx if 0 <= self.playback_mode_idx < 3 else 0]

    # ---- Subscriber table ops (EVDHelpers.h translations) ------------

    def prune_stale_subscribers_locked(self, now: int) -> None:
        """Caller must hold subs_lock."""
        kept = []
        for s in self.subscribers:
            if (now - s.last_seen_ms) <= C.PLAYER_STALE_TIMEOUT_MS:
                kept.append(s)
            else:
                self.log("W", "Subscriber expired: %s (%s) - no data for %d s"
                         % (s.name, s.ip, (now - s.last_seen_ms) // 1000))
        self.subscribers = kept
        self.subscriber_count = len(kept)

    def find_subscriber_locked(self, ip: str) -> int:
        """Caller must hold subs_lock."""
        for i, s in enumerate(self.subscribers):
            if s.ip == ip:
                return i
        return -1

    def find_or_create_subscriber_locked(
        self,
        ip: str,
        name: str,
        declared_proto: int,
        negotiated_proto: int,
        compatible: bool,
    ) -> int:
        """Caller must hold subs_lock. Mirrors findOrCreateSubscriberLocked."""
        slot = self.find_subscriber_locked(ip)
        if slot >= 0:
            s = self.subscribers[slot]
            s.name = name
            s.last_seen_ms = now_ms()
            s.consecutive_send_failures = 0
            s.declared_audio_protocol_version = declared_proto
            s.negotiated_audio_protocol_version = negotiated_proto
            s.audio_compatible = compatible
            # Do NOT reset has_completed_clock_sync
            return slot

        if len(self.subscribers) >= C.MAX_UDP_SUBSCRIBERS:
            return -1

        self.subscribers.append(Subscriber(
            ip=ip, name=name, last_seen_ms=now_ms(),
            consecutive_send_failures=0,
            declared_audio_protocol_version=declared_proto,
            negotiated_audio_protocol_version=negotiated_proto,
            audio_compatible=compatible,
            has_completed_clock_sync=False,
            provisional=False,
        ))
        self.subscriber_count = len(self.subscribers)
        return len(self.subscribers) - 1

    def snapshot_subscribers(self, now: int) -> List[SubscriberSnap]:
        with self.subs_lock:
            self.prune_stale_subscribers_locked(now)
            return [
                SubscriberSnap(
                    ip=s.ip, name=s.name, last_seen_ms=s.last_seen_ms,
                    consecutive_send_failures=s.consecutive_send_failures,
                    declared_audio_protocol_version=s.declared_audio_protocol_version,
                    negotiated_audio_protocol_version=s.negotiated_audio_protocol_version,
                    audio_compatible=s.audio_compatible,
                    has_completed_clock_sync=s.has_completed_clock_sync,
                    provisional=s.provisional,
                )
                for s in self.subscribers
            ]

    # ---- Player table ops --------------------------------------------

    def prune_stale_players_locked(self, now: int) -> None:
        kept = []
        for p in self.player_nodes:
            if (now - p.last_seen_ms) <= C.PLAYER_STALE_TIMEOUT_MS:
                kept.append(p)
            else:
                self.log("W", "Player expired: %s (%s) - no heartbeat for %d s"
                         % (p.name, p.ip, (now - p.last_seen_ms) // 1000))
        self.player_nodes = kept

    def snapshot_players(self, now: int) -> List[PlayerNode]:
        with self.players_lock:
            self.prune_stale_players_locked(now)
            return list(self.player_nodes)

    # ---- Zone snapshot (EVDHelpers.h) --------------------------------

    def snapshot_zones(self) -> List[Zone]:
        with self.zones_lock:
            # Deep-copy so callers can't mutate the canonical list.
            return [Zone(id=z.id, name=z.name, volume=z.volume,
                         muted=z.muted, node_ips=list(z.node_ips))
                    for z in self.zones]

    def assigned_node_ips(self) -> set:
        """IPs placed in a zone. Every other node is Discovered and muted."""
        with self.zones_lock:
            ips = set()
            for z in self.zones:
                ips.update(z.node_ips)
            return ips

    # ---- Node perf ops (EVDHelpers.h) --------------------------------

    def prune_stale_perf_locked(self, now: int) -> None:
        self.node_perf = [
            p for p in self.node_perf
            if p.updated_ms != 0
               and (now - p.updated_ms) <= C.NODE_PERF_STALE_TIMEOUT_MS
        ]

    def update_node_perf(
        self, ip: str, latency_ms: int, queue_depth: int,
        target: int, rebuffers: int, now: int,
    ) -> None:
        with self.perf_lock:
            for p in self.node_perf:
                if p.ip == ip:
                    p.latency_ms = latency_ms
                    p.queue_depth = queue_depth
                    p.target = target
                    p.rebuffers = rebuffers
                    p.updated_ms = now
                    p.has_data = True
                    return
            self.prune_stale_perf_locked(now)
            if len(self.node_perf) >= C.NODE_PERF_MAX_ENTRIES:
                return
            self.node_perf.append(NodePerf(
                ip=ip, latency_ms=latency_ms, queue_depth=queue_depth,
                target=target, rebuffers=rebuffers, updated_ms=now,
            ))

    def remove_node_perf(self, ip: str) -> None:
        with self.perf_lock:
            self.node_perf = [p for p in self.node_perf if p.ip != ip]

    def snapshot_perf(self) -> List[NodePerf]:
        with self.perf_lock:
            self.prune_stale_perf_locked(now_ms())
            return list(self.node_perf)

    # ---- Logging helper ---------------------------------------------

    def log(self, level: str, text: str) -> None:
        """Log level is 'I', 'W', or 'E'. Feeds the ring + stdout."""
        # The ring lives on the module-level `log_ring` instance (see
        # log_ring.py). We import lazily to avoid a circular import.
        from . import log_ring as _lr
        _lr.log_ring.append(level=level, text=text)


# Singleton
runtime = Runtime()


# ---- Compatibility helpers (EVDProtocolCompat.h) --------------------------

def evaluate_audio_compatibility(
    declared: int, server_version: int = C.AUDIO_PROTO_VERSION
) -> Tuple[bool, int, str]:
    """Returns (compatible, negotiated_protocol_version, reason)."""
    if declared < 0:
        return False, 0, "no_protocol_declared"
    if declared < server_version:
        return False, 0, "player_protocol_too_old"
    if declared > server_version:
        return True, server_version, "negotiated_down"
    return True, server_version, "ok"