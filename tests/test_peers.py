"""
Unit tests for the P3 "servers decide" election (peers.py).

Guards:
  * the leader is the alive server with the lowest (priority, id);
  * a higher-priority peer demotes this server to standby;
  * a dead leader promotes the next server (failover);
  * priority persists to peers.json and a drag-order applies it.

Run from EnkelvoudPC/:

    runtime\\python\\python.exe tests\\test_peers.py -v
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from enkelvoud import constants as C
from enkelvoud.peers import PeerRegistry, elect
from enkelvoud.state import now_ms, runtime


class ElectTest(unittest.TestCase):
    def test_lowest_priority_wins(self):
        me = {"id": "pc-1", "priority": 2}
        peers = [{"id": "esp-1", "priority": 1}]
        self.assertEqual(elect(peers + [me])["id"], "esp-1")

    def test_tie_break_by_id(self):
        me = {"id": "b", "priority": 1}
        peers = [{"id": "a", "priority": 1}]
        self.assertEqual(elect(peers + [me])["id"], "a")

    def test_longer_running_leader_keeps_leadership_on_tie(self):
        # Regression: the ESP's persisted priority could tie the PC's at 1,
        # and the id tie-break ("esp-" < "pc-") then handed the fleet to a
        # newcomer that streamed nothing. The incumbent (higher uptime) wins.
        me = {"id": "pc-1", "priority": 1, "uptimeMs": 500000}
        peers = [{"id": "esp-1", "priority": 1, "uptimeMs": 3000}]
        self.assertEqual(elect(peers + [me])["id"], "pc-1")
        # Equal uptime still falls back to the id.
        peers[0]["uptimeMs"] = 500000
        self.assertEqual(elect(peers + [me])["id"], "esp-1")

    def test_invalid_priority_ranks_worst(self):
        me = {"id": "pc-1", "priority": 1}
        peers = [{"id": "x", "priority": None}]
        self.assertEqual(elect(peers + [me])["id"], "pc-1")


class RegistryRoleTest(unittest.TestCase):
    def setUp(self):
        runtime.is_leader = True

    def tearDown(self):
        runtime.is_leader = True

    def _mk(self) -> PeerRegistry:
        r = PeerRegistry()
        r.id = "pc-test"
        r.priority = 1
        return r

    def _beacon(self, r: PeerRegistry, pid: str, prio: int) -> None:
        msg = {"e": C.PEER_PROTOCOL, "id": pid, "name": pid.upper(),
               "priority": prio, "leader": False, "httpPort": 80,
               "uptimeMs": 1000, "version": "x"}
        r._on_beacon(json.dumps(msg).encode(), ("192.168.100.50", C.PEER_PORT))

    def test_single_server_is_leader(self):
        r = self._mk()
        r._evaluate()
        self.assertEqual(r.role, "leader")
        self.assertTrue(runtime.is_leader)

    def test_higher_priority_peer_makes_standby(self):
        r = self._mk()
        self._beacon(r, "esp-1", 1)   # same priority, id tie-break -> peer wins
        r._evaluate()
        self.assertEqual(r.role, "standby")
        self.assertFalse(runtime.is_leader)

    def test_lower_priority_peer_keeps_leader(self):
        r = self._mk()
        self._beacon(r, "esp-2", 5)
        r._evaluate()
        self.assertEqual(r.role, "leader")

    def test_dead_leader_restores_leadership(self):
        r = self._mk()
        self._beacon(r, "esp-1", 1)
        r._evaluate()
        self.assertEqual(r.role, "standby")
        # Age the peer past the dead timeout: failover promotes this server.
        with r.lock:
            r.peers["esp-1"]["lastSeenMs"] = now_ms() - (C.PEER_DEAD_MS + 1000)
        r._evaluate()
        self.assertEqual(r.role, "leader")
        self.assertTrue(runtime.is_leader)

    def test_priority_persists(self):
        # A plain file in TEMP (not a mkdtemp subdir: the confined test
        # sandbox denies creating nested temp directories).
        path = os.path.join(os.environ.get("TEMP", "."), "evd_peers_test.json")
        try:
            r = self._mk()
            r._data_path = path
            r.set_priority(3)
            self.assertEqual(r.priority, 3)
            r2 = PeerRegistry()
            r2.id = "pc-test"
            r2._data_path = path
            r2._load_priority()
            self.assertEqual(r2.priority, 3)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def test_apply_order_sets_self_priority(self):
        r = self._mk()
        res = r.apply_order(["esp-1", "pc-test"])
        self.assertEqual(r.priority, 2)
        self.assertEqual(res["pc-test"], "self")

    def test_role_change_callback_fires(self):
        seen = []
        r = self._mk()
        r.configure(None, 80, on_role_change=seen.append)
        self._beacon(r, "esp-1", 1)
        r._evaluate()
        self.assertEqual(seen, ["standby"])


if __name__ == "__main__":
    unittest.main()
