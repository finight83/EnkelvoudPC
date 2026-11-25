"""
enkelvoud.sender - UDP :4202 audio fan-out, paced at 20 ms per frame.

Per-frame flow (mirrors EVDUDP.h udpSendTask):

  1. Pull OpusPacket from opus_queue (blocks up to 100 ms).
  2. If master_muted, drop the frame without sending.
  3. Compute packetGap = (now - last_packet_ms) > AUDIO_PACER_MAX_LAG_MS (100 ms).
  4. If pacer not initialized, start it.
  5. Sleep until pacer.until_ms(now) is <= 0 (capped at 100 ms per sleep).
  6. pacer.prepare_frame(post_delay_ms, 20, opus_qsize, packet_gap):
       returns False -> drop stale frame, continue.
       returns True  -> pack header + payload, send.
  7. Snapshot subscribers, apply per-subscriber gates, sendto each.

Gates (all four must pass, in this order):
  a. last_seen_ms is fresher than SUB_DEAD_LASTSEEN_MS (7000 ms)
  b. consecutive_send_failures < MAX_SEND_FAILURES_BEFORE_SKIP (10)
  c. has_completed_clock_sync == True
  d. audio_compatible == True

The failure counter resets on successful send and increments on OSError.
The C++ has two additional nuanced counters (statAudioSyncPending,
statAudioProvisionalSkips, statAudioIncompatibleSkips) which we
reproduce verbatim in the gate path.

Wire header (12 bytes, little-endian):
  [0..1]  magic       = 0xE1D0
  [2]     version     = 2
  [3]     flags       = 0
  [4..7]  seq         (uint32 LE)
  [8..11] captured_ms (uint32 LE)
"""

import socket
import struct
import threading
import time

from . import constants as C
from .pacer import AudioFramePacer
from .queues import opus_queue
from .state import runtime, now_ms
from .log_ring import log_info, log_warn, log_error


_HEADER_FMT = "<HBBII"   # 12 bytes


def _pack_header(seq: int, captured_ms: int) -> bytes:
    return struct.pack(
        _HEADER_FMT,
        C.AUDIO_PROTO_MAGIC,
        C.AUDIO_PROTO_VERSION,
        C.AUDIO_HEADER_FLAGS,
        seq & 0xFFFFFFFF,
        captured_ms & 0xFFFFFFFF,
    )


class AudioSender:
    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("0.0.0.0", 0))
        self._sock.setblocking(True)

        self._thread = threading.Thread(
            target=self._run, name="sender", daemon=True,
        )
        self._thread.start()
        log_info("Sender: thread started")

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    # ---- Thread -------------------------------------------------------------

    def _run(self) -> None:
        pacer = AudioFramePacer()
        last_packet_ms = 0
        has_last = False

        while not self._stop.is_set():
            pkt = opus_queue.get(timeout=0.1)
            arrival_ms = now_ms()
            runtime.udp_send_loop_count += 1

            if pkt is None:
                continue

            runtime.udp_stage_after_recv += 1

            if runtime.master_muted:
                # Skip without advancing the pacer schedule — the C++ does
                # this too: the packet is consumed but no send happens.
                continue

            runtime.udp_stage_after_store += 1

            packet_gap = (
                has_last
                and (arrival_ms - last_packet_ms) > C.AUDIO_PACER_MAX_LAG_MS
            )
            last_packet_ms = arrival_ms
            has_last = True

            if not pacer.initialized:
                pacer.start(arrival_ms)

            until = pacer.until_ms(arrival_ms)
            if until > 0:
                time.sleep(min(until, 100) / 1000.0)

            post_ms = now_ms()
            queued = opus_queue.qsize()

            if not pacer.prepare_frame(post_ms, C.OPUS_FRAME_MS, queued, packet_gap):
                runtime.udp_send_stall_resets += 1
                runtime.udp_pacer_behind_reset += 1
                runtime.udp_pacer_drift_reset += 1
                runtime.stat_opus_dropped += 1
                continue

            header = _pack_header(pkt.seq, pkt.captured_ms)
            datagram = header + pkt.opus
            self._fanout(datagram, post_ms, pkt)

    # ---- Fanout -------------------------------------------------------------

    def _fanout(self, datagram: bytes, snapshot_ms: int, pkt) -> None:
        subs = runtime.snapshot_subscribers(snapshot_ms)
        if not subs:
            return

        sent_ips: list[tuple[str, bool]] = []

        for s in subs:
            # a. Freshness gate.
            if (snapshot_ms - s.last_seen_ms) > C.SUB_DEAD_LASTSEEN_MS:
                continue
            # b. Send-failure gate.
            if s.consecutive_send_failures >= C.MAX_SEND_FAILURES_BEFORE_SKIP:
                continue
            # c. Clock-sync gate.
            if not s.has_completed_clock_sync:
                runtime.stat_audio_sync_pending += 1
                continue
            # d. Compatibility gate.
            if not s.audio_compatible:
                if s.provisional:
                    runtime.stat_audio_provisional_skips += 1
                else:
                    runtime.stat_audio_incompatible_skips += 1
                continue

            try:
                self._sock.sendto(datagram, (s.ip, C.AUDIO_UDP_PORT))
                sent_ips.append((s.ip, True))
            except OSError:
                runtime.stat_udp_errors += 1
                sent_ips.append((s.ip, False))

        # ---- Update failure counters -----------------------------------
        if sent_ips:
            with runtime.subs_lock:
                for ip, ok in sent_ips:
                    slot = runtime.find_subscriber_locked(ip)
                    if slot < 0:
                        continue
                    if ok:
                        runtime.subscribers[slot].consecutive_send_failures = 0
                    else:
                        if runtime.subscribers[slot].consecutive_send_failures < 255:
                            runtime.subscribers[slot].consecutive_send_failures += 1

        # ---- Stats -----------------------------------------------------
        n_ok = sum(1 for _, ok in sent_ips if ok)
        if n_ok > 0:
            latency_ms = (now_ms() - pkt.captured_ms) & 0xFFFFFFFF
            runtime.stat_udp_packets_sent += n_ok
            runtime.stat_udp_bytes_sent += len(datagram) * n_ok
            runtime.stat_latency_sum_ms += latency_ms
            runtime.stat_latency_count += 1


# Singleton
sender = AudioSender()