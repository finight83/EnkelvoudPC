"""
enkelvoud.clock_sync - UDP :4204 sync_req/sync_resp responder +
                       30 s authoritative time_sync broadcast.

Wire formats (EVDUDP.h handleClockSyncRequests / sendAuthoritativeTimeSync):

  Request  (node -> server, JSON):
    {"type":"sync_req","clientTxMs":<uint32>}

  Response (server -> node, JSON, echoed to request's source IP:port):
    {"type":"sync_resp","protoVersion":2,"epoch":<uint32>,
     "clientTxMs":<uint32>,"serverRxMs":<uint32>,"serverTxMs":<uint32>}

  serverRxMs is timestamped BEFORE JSON parsing.
  serverTxMs is timestamped just before the datagram is handed to the socket.
  clientTxMs is echoed verbatim from the request.

Rate limit:
  16-entry table, keyed by source IPv4 address (as uint32), max 20 ms
  between served requests per IP.  LRU eviction: prefer a free slot,
  else evict the entry with the smallest lastMs.

Promotion rule:
  A subscriber slot only gains hasCompletedClockSync == True after a
  sync_resp is actually sent (sendto success).  Failed sends do NOT
  promote.  A newly created slot is marked provisional=True until the
  first /api/player/register heartbeat clears it.

Broadcast:
  Every 30 s the clock-sync task pushes time_sync with
  "authoritative":true to every subscriber (ctrl :4203).
"""

import json
import select
import socket
import threading
import time
from typing import List, Optional

from . import constants as C
from . import ctrl
from .state import runtime, now_ms
from .log_ring import log_info, log_warn, log_error


class _RateEntry:
    __slots__ = ("occupied", "ip_int", "last_ms")
    def __init__(self) -> None:
        self.occupied = False
        self.ip_int = 0
        self.last_ms = 0


class ClockSyncResponder:
    def __init__(self) -> None:
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._rate_table: List[_RateEntry] = [
            _RateEntry() for _ in range(C.SYNC_RATE_TABLE_SIZE)
        ]
        self._last_auth_sync_ms = 0

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("0.0.0.0", C.CLOCK_SYNC_UDP_PORT))
        self._sock.setblocking(False)

        self._thread = threading.Thread(
            target=self._run, name="clock-sync", daemon=True,
        )
        self._thread.start()

        log_info("Clock sync responder listening on port %d",
                 C.CLOCK_SYNC_UDP_PORT)

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    # ---- Poll loop ------------------------------------------------------

    def _run(self) -> None:
        assert self._sock is not None
        poll_timeout = C.SYNC_POLL_MS / 1000.0
        while not self._stop.is_set():
            # Wait up to 5 ms for a datagram.
            try:
                r, _, _ = select.select([self._sock], [], [], poll_timeout)
            except (OSError, ValueError):
                break
            if r:
                try:
                    data, addr = self._sock.recvfrom(2048)
                except OSError:
                    data, addr = b"", None
                if addr is not None:
                    self._handle_request(data, addr)

            # Check the 30 s authoritative broadcast deadline.
            now = now_ms()
            if now - self._last_auth_sync_ms >= C.TIME_SYNC_BROADCAST_INTERVAL_MS:
                self._last_auth_sync_ms = now
                n = ctrl.broadcast_authoritative_time_sync()
                if n > 0:
                    log_info("Time sync broadcast to %d subscribers", n)

    # ---- Request handler ------------------------------------------------

    def _handle_request(self, data: bytes, addr) -> None:
        # [serverRxMs before parse] - EVDUDP.h
        server_rx_ms = now_ms() & 0xFFFFFFFF

        try:
            ip_int = int.from_bytes(socket.inet_aton(addr[0]), "big")
        except OSError:
            runtime.stat_sync_request_errors += 1
            return

        # Rate limit.
        if not self._rate_allow(ip_int, now_ms()):
            runtime.stat_sync_requests_throttled += 1
            return

        # Parse (only type and clientTxMs are used).
        try:
            doc = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            runtime.stat_sync_request_errors += 1
            return
        if not isinstance(doc, dict) or doc.get("type") != "sync_req":
            runtime.stat_sync_request_errors += 1
            return
        try:
            client_tx_ms = int(doc.get("clientTxMs", 0)) & 0xFFFFFFFF
        except (TypeError, ValueError):
            runtime.stat_sync_request_errors += 1
            return

        # [serverTxMs just before send] - EVDUDP.h
        server_tx_ms = now_ms() & 0xFFFFFFFF

        reply = (
            f'{{"type":"sync_resp",'
            f'"protoVersion":{C.AUDIO_PROTO_VERSION},'
            f'"epoch":{runtime.server_epoch & 0xFFFFFFFF},'
            f'"clientTxMs":{client_tx_ms},'
            f'"serverRxMs":{server_rx_ms},'
            f'"serverTxMs":{server_tx_ms}}}'
        )

        try:
            self._sock.sendto(reply.encode("utf-8"), addr)  # type: ignore[union-attr]
        except OSError:
            runtime.stat_sync_request_errors += 1
            return

        runtime.stat_sync_requests_served += 1

        # [sync-promote-after-send] - EVDUDP.h
        # Promote only after sendto succeeded.
        remote_str = addr[0]
        promoted = False
        with runtime.subs_lock:
            slot = runtime.find_subscriber_locked(remote_str)
            if slot < 0:
                # Provisional slot - see [sync-before-heartbeat-provisional-slot]
                slot = runtime.find_or_create_subscriber_locked(
                    remote_str, remote_str,
                    C.AUDIO_PROTO_VERSION, C.AUDIO_PROTO_VERSION, True,
                )
                if slot >= 0:
                    runtime.subscribers[slot].provisional = True
            if slot >= 0:
                runtime.subscribers[slot].last_seen_ms = now_ms()
                runtime.subscribers[slot].consecutive_send_failures = 0
                if not runtime.subscribers[slot].has_completed_clock_sync:
                    runtime.subscribers[slot].has_completed_clock_sync = True
                    promoted = True

        if promoted:
            log_info("Clock sync: %s promoted to audio-eligible (first RTT sync)",
                     remote_str)

    # ---- Rate limit (syncRateAllow equivalent) --------------------------

    def _rate_allow(self, ip_int: int, now: int) -> bool:
        free_slot = -1
        oldest_slot = 0
        for i, entry in enumerate(self._rate_table):
            if entry.occupied and entry.ip_int == ip_int:
                if (now - entry.last_ms) < C.SYNC_MIN_INTERVAL_MS:
                    return False
                entry.last_ms = now
                return True
            if not entry.occupied and free_slot < 0:
                free_slot = i
            if entry.last_ms < self._rate_table[oldest_slot].last_ms:
                oldest_slot = i
        slot = free_slot if free_slot >= 0 else oldest_slot
        self._rate_table[slot].occupied = True
        self._rate_table[slot].ip_int = ip_int
        self._rate_table[slot].last_ms = now
        return True


# Singleton
clock_sync = ClockSyncResponder()