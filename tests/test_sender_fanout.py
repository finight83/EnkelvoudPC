"""
Regression test for AudioSender._fanout()'s per-subscriber gates.

Guards the crash seen on 2026-11-25:

    AttributeError: 'SubscriberSnap' object has no attribute
    'consecutive_send_failures'

The send-failure gate (b) reads the counter off the snapshot returned by
Runtime.snapshot_subscribers(), so that snapshot has to carry it.

Run from EnkelvoudPC/:

    runtime\\python\\python.exe tests\\test_sender_fanout.py -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from enkelvoud import constants as C
from enkelvoud import sender as sender_mod
from enkelvoud.queues import OpusPacket
from enkelvoud.state import Zone, now_ms, runtime


class _FakeSocket:
    def __init__(self, fail: bool = False) -> None:
        self.sent = []
        self.fail = fail

    def sendto(self, data, addr):
        if self.fail:
            raise OSError("unreachable")
        self.sent.append((data, addr))


class FanoutGateTest(unittest.TestCase):
    NODE_IP = "192.168.100.216"

    def setUp(self) -> None:
        runtime.subscribers.clear()
        # [discovered-nodes] Audio only flows to nodes assigned to a room.
        # Put the test node in Main Room so the pre-existing gates are the
        # thing under test here; the muted/Discovered case has its own test.
        with runtime.zones_lock:
            runtime.zones = [Zone(id=C.MAIN_ZONE_ID, name="Main Room",
                                  volume=100, muted=False,
                                  node_ips=[self.NODE_IP])]
        runtime.find_or_create_subscriber_locked(
            self.NODE_IP, "node", C.AUDIO_PROTO_VERSION,
            C.AUDIO_PROTO_VERSION, True,
        )
        with runtime.subs_lock:
            sub = runtime.subscribers[0]
            sub.last_seen_ms = now_ms()
            sub.has_completed_clock_sync = True
            sub.audio_compatible = True
            sub.provisional = False
            sub.consecutive_send_failures = 0
        self.pkt = OpusPacket(opus=b"\x11" * 40, captured_ms=now_ms(), seq=1)
        self.datagram = b"\x00" * 12 + self.pkt.opus
        self.sender = sender_mod.sender

    def _fanout(self, fail: bool = False) -> _FakeSocket:
        sock = _FakeSocket(fail=fail)
        self.sender._sock = sock
        self.sender._fanout(self.datagram, now_ms(), self.pkt)
        return sock

    def test_snapshot_carries_send_failure_counter(self) -> None:
        snaps = runtime.snapshot_subscribers(now_ms())
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0].consecutive_send_failures, 0)

    def test_healthy_subscriber_receives_audio(self) -> None:
        sock = self._fanout()
        self.assertEqual(len(sock.sent), 1)
        self.assertEqual(sock.sent[0][1], (self.NODE_IP, C.AUDIO_UDP_PORT))

    def test_unassigned_node_is_muted(self) -> None:
        with runtime.zones_lock:
            runtime.zones = [Zone(id=C.MAIN_ZONE_ID, name="Main Room",
                                  volume=100, muted=False, node_ips=[])]
        sock = self._fanout()
        self.assertEqual(sock.sent, [])

    def test_send_error_increments_counter(self) -> None:
        self._fanout(fail=True)
        self.assertEqual(runtime.subscribers[0].consecutive_send_failures, 1)

    def test_failure_gate_skips_at_threshold(self) -> None:
        with runtime.subs_lock:
            runtime.subscribers[0].consecutive_send_failures = (
                C.MAX_SEND_FAILURES_BEFORE_SKIP
            )
        sock = self._fanout()
        self.assertEqual(sock.sent, [])

    def test_failure_gate_allows_below_threshold(self) -> None:
        with runtime.subs_lock:
            runtime.subscribers[0].consecutive_send_failures = (
                C.MAX_SEND_FAILURES_BEFORE_SKIP - 1
            )
        sock = self._fanout()
        self.assertEqual(len(sock.sent), 1)

    def test_clock_sync_gate_holds_audio(self) -> None:
        with runtime.subs_lock:
            runtime.subscribers[0].has_completed_clock_sync = False
        before = runtime.stat_audio_sync_pending
        sock = self._fanout()
        self.assertEqual(sock.sent, [])
        self.assertEqual(runtime.stat_audio_sync_pending, before + 1)


if __name__ == "__main__":
    unittest.main()
