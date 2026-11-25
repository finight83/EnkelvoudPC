"""
enkelvoud.ctrl - UDP :4203 unicast sender + all host_audio_sync and
                   time_sync payload variants.

Every payload format is copied VERBATIM from EVDPlayers.h / EVDUDP.h.
Three distinct host_audio_sync shapes exist in the C++ source and each is
a separate function here so one cannot silently drift into another.

  send_host_audio_sync_first_sight   <- handlePlayerRegistration() isNew path
  send_host_audio_sync_broadcast     <- broadcastNodeConfig()
  send_zone_config_to_ip             <- sendZoneConfigToIp()

Two time_sync shapes exist:

  send_time_sync_first_sight         <- handlePlayerRegistration() isNew path
  send_time_sync_authoritative       <- sendAuthoritativeTimeSync() (30 s tick)

The distinction is the presence of "authoritative":true on the 30 s tick only.
"""

import ipaddress
import socket
import threading
from typing import Tuple

from . import constants as C
from .state import runtime, now_ms


# ---- Single outbound UDP socket for ctrl packets --------------------------
# C++: ctrlUdp.begin(0) -> ephemeral source port. We mirror that.
_ctrl_socket: socket.socket | None = None
_ctrl_socket_lock = threading.Lock()


def _get_socket() -> socket.socket:
    global _ctrl_socket
    with _ctrl_socket_lock:
        if _ctrl_socket is None:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("0.0.0.0", 0))
            _ctrl_socket = s
        return _ctrl_socket


def _fmt_vol(pct: int) -> str:
    """Mirror of String((float)pct / 100.0f, 3)."""
    return f"{pct / 100.0:.3f}"


def _enc_signal_str(signal: int) -> str:
    if signal == C.OPUS_SIGNAL_MUSIC:
        return "music"
    if signal == C.OPUS_SIGNAL_VOICE:
        return "voice"
    if signal == C.OPUS_AUTO:
        return "auto"
    return "unknown"


def _enc_application_str(app: int) -> str:
    if app == C.OPUS_APPLICATION_AUDIO:
        return "audio"
    if app == C.OPUS_APPLICATION_VOIP:
        return "voip"
    if app == C.OPUS_APPLICATION_RESTRICTED_LOWDELAY:
        return "lowdelay"
    return "unknown"


# ---- Core send helper (EVDUDP.h sendCtrlToSubscriber) ---------------------

def send_ctrl_to_subscriber(ip: str, json_body: str) -> bool:
    """Send one ctrl packet to ip:CTRL_UDP_PORT.

    Mirrors EVDUDP.h sendCtrlToSubscriber():
      - If ip is a known subscriber with consecutiveSendFailures >=
        SUB_FAILURE_SKIP_COUNT (== 2), skip (audio path uses 10, ctrl uses 2).
      - Validate IP; on failure increment statUdpErrors and return False.
      - Send as a single datagram; on failure increment statUdpErrors.
    """
    # Back-off check (subsMutex equivalent).
    with runtime.subs_lock:
        slot = runtime.find_subscriber_locked(ip)
        if slot >= 0:
            if runtime.subscribers[slot].consecutive_send_failures >= C.SUB_FAILURE_SKIP_COUNT:
                return False

    # Validate IP.
    try:
        ipaddress.IPv4Address(ip)
    except ValueError:
        runtime.stat_udp_errors += 1
        return False

    data = json_body.encode("utf-8")
    try:
        sock = _get_socket()
        sock.sendto(data, (ip, C.CTRL_UDP_PORT))
        return True
    except OSError:
        runtime.stat_udp_errors += 1
        return False


# ---- time_sync ------------------------------------------------------------

def _time_sync_authoritative_json() -> str:
    # EVDUDP.h sendAuthoritativeTimeSync() -- source of truth.
    server_ms = now_ms() & 0xFFFFFFFF
    return (
        f'{{"type":"time_sync","serverMs":{server_ms},'
        f'"epoch":{runtime.server_epoch & 0xFFFFFFFF},'
        f'"protoVersion":{C.AUDIO_PROTO_VERSION},"authoritative":true}}'
    )


def _time_sync_first_sight_json() -> str:
    # EVDPlayers.h handlePlayerRegistration() first-sight push.
    # NOTE: NO "authoritative" field on this one.
    server_ms = now_ms() & 0xFFFFFFFF
    return (
        f'{{"type":"time_sync","serverMs":{server_ms},'
        f'"epoch":{runtime.server_epoch & 0xFFFFFFFF},'
        f'"protoVersion":{C.AUDIO_PROTO_VERSION}}}'
    )


def send_time_sync_first_sight(ip: str) -> bool:
    return send_ctrl_to_subscriber(ip, _time_sync_first_sight_json())


def send_time_sync_authoritative(ip: str) -> bool:
    return send_ctrl_to_subscriber(ip, _time_sync_authoritative_json())


def broadcast_authoritative_time_sync() -> int:
    """Called by clock_sync every 30 s. Returns number of packets attempted."""
    body = _time_sync_authoritative_json()
    subs = runtime.snapshot_subscribers(now_ms())
    sent = 0
    for s in subs:
        if send_ctrl_to_subscriber(s.ip, body):
            sent += 1
    return sent


# ---- host_audio_sync: first-sight form ------------------------------------
# EVDPlayers.h handlePlayerRegistration() isNew path.
#
# Fields IN this form:  target, type, volume, muted, zoneVolume, zoneMuted,
#                       bufferMs, sourceSampleRate, opusSampleRate, mode,
#                       audioProtocolVersion, negotiatedAudioProtocolVersion,
#                       audioCompatible, audioCompatReason, epoch, clockSyncPort
# Fields NOT in this form: encBitrate, encComplexity, encVbr, encVbrConstraint,
#                          encSignal, encApplication
# ---------------------------------------------------------------------------

def send_host_audio_sync_first_sight(
    ip: str,
    decision: Tuple[bool, int, str],     # (compatible, negotiated_ver, reason)
    zvol: int,
    zmute: bool,
) -> bool:
    compatible, negotiated_ver, reason = decision
    body = (
        f'{{"target":"{ip}","type":"host_audio_sync",'
        f'"volume":{_fmt_vol(runtime.master_volume)},'
        f'"muted":{"true" if runtime.master_muted else "false"},'
        f'"zoneVolume":{_fmt_vol(zvol)},'
        f'"zoneMuted":{"true" if zmute else "false"},'
        f'"bufferMs":{runtime.audio_buffer_ms},'
        f'"sourceSampleRate":{C.SOURCE_SAMPLE_RATE},'
        f'"opusSampleRate":{runtime.opus_sample_rate},'
        f'"mode":"{runtime.playback_mode_cstr()}",'
        f'"audioProtocolVersion":{C.AUDIO_PROTO_VERSION},'
        f'"negotiatedAudioProtocolVersion":{negotiated_ver},'
        f'"audioCompatible":{"true" if compatible else "false"},'
        f'"audioCompatReason":"{reason}",'
        f'"epoch":{runtime.server_epoch & 0xFFFFFFFF},'
        f'"clockSyncPort":{C.CLOCK_SYNC_UDP_PORT}}}'
    )
    return send_ctrl_to_subscriber(ip, body)


# ---- host_audio_sync: broadcast form --------------------------------------
# EVDPlayers.h broadcastNodeConfig().
#
# Fields IN this form:  target, type, volume, muted, zoneVolume, zoneMuted,
#                       bufferMs, sourceSampleRate, opusSampleRate, mode,
#                       audioProtocolVersion, negotiatedAudioProtocolVersion,
#                       audioCompatible, epoch, clockSyncPort,
#                       encBitrate, encComplexity, encVbr, encVbrConstraint,
#                       encSignal, encApplication
# Fields NOT in this form: audioCompatReason
#
# Note: encSignal in broadcast uses g_currentSignal (APPLIED), while
# handlePlayerRegistration's response body uses g_newSignal (INTENDED).
# We mirror both, one per function.
# ---------------------------------------------------------------------------

def send_host_audio_sync_broadcast(
    ip: str,
    negotiated_ver: int,
    compatible: bool,
    zvol: int,
    zmute: bool,
) -> bool:
    body = (
        f'{{"target":"{ip}","type":"host_audio_sync",'
        f'"volume":{_fmt_vol(runtime.master_volume)},'
        f'"muted":{"true" if runtime.master_muted else "false"},'
        f'"zoneVolume":{_fmt_vol(zvol)},'
        f'"zoneMuted":{"true" if zmute else "false"},'
        f'"bufferMs":{runtime.audio_buffer_ms},'
        f'"sourceSampleRate":{C.SOURCE_SAMPLE_RATE},'
        f'"opusSampleRate":{runtime.opus_sample_rate},'
        f'"mode":"{runtime.playback_mode_cstr()}",'
        f'"audioProtocolVersion":{C.AUDIO_PROTO_VERSION},'
        f'"negotiatedAudioProtocolVersion":{negotiated_ver},'
        f'"audioCompatible":{"true" if compatible else "false"},'
        f'"epoch":{runtime.server_epoch & 0xFFFFFFFF},'
        f'"clockSyncPort":{C.CLOCK_SYNC_UDP_PORT},'
        f'"encBitrate":{runtime.current_bitrate},'
        f'"encComplexity":{runtime.current_complexity},'
        f'"encVbr":{"true" if runtime.current_vbr else "false"},'
        f'"encVbrConstraint":{"true" if runtime.current_vbr_constraint else "false"},'
        f'"encSignal":"{_enc_signal_str(runtime.current_signal)}",'
        f'"encApplication":"{_enc_application_str(runtime.current_application)}"}}'
    )
    return send_ctrl_to_subscriber(ip, body)


# ---- host_audio_sync: partial form (zone push) ---------------------------
# EVDPlayers.h sendZoneConfigToIp().
#
# Fields IN this form:  target, type, zoneVolume, zoneMuted,
#                       sourceSampleRate, opusSampleRate, bufferMs, mode,
#                       epoch, clockSyncPort
# Optional:             audioProtocolVersion, negotiatedAudioProtocolVersion,
#                       audioCompatible, audioCompatReason
#                       (only if the node has ever registered via /api/player/register)
# Fields NOT in this form: volume, muted, enc* (all deliberately omitted so
#                          applyCtrlJson() on the node leaves master vol/mute
#                          untouched and only the zone state changes)
# ---------------------------------------------------------------------------

def send_zone_config_to_ip(ip: str, zvol: int, zmute: bool) -> bool:
    # Look up the node's last-known compat state.
    have_compat = False
    compatible = False
    negotiated_ver = 0
    compat_reason = ""
    for p in runtime.snapshot_players(now_ms()):
        if p.ip == ip:
            have_compat = True
            compatible = p.audio_compatible
            negotiated_ver = p.negotiated_audio_protocol_version
            compat_reason = p.audio_compat_reason
            break

    body = (
        f'{{"target":"{ip}","type":"host_audio_sync"'
        f',"zoneVolume":{_fmt_vol(zvol)}'
        f',"zoneMuted":{"true" if zmute else "false"}'
        f',"sourceSampleRate":{C.SOURCE_SAMPLE_RATE}'
        f',"opusSampleRate":{runtime.opus_sample_rate}'
        f',"bufferMs":{runtime.audio_buffer_ms}'
        f',"mode":"{runtime.playback_mode_cstr()}"'
        f',"epoch":{runtime.server_epoch & 0xFFFFFFFF}'
        f',"clockSyncPort":{C.CLOCK_SYNC_UDP_PORT}'
    )
    if have_compat:
        body += (
            f',"audioProtocolVersion":{C.AUDIO_PROTO_VERSION}'
            f',"negotiatedAudioProtocolVersion":{negotiated_ver}'
            f',"audioCompatible":{"true" if compatible else "false"}'
            f',"audioCompatReason":"{compat_reason}"'
        )
    body += "}"
    return send_ctrl_to_subscriber(ip, body)