"""
enkelvoud.peers - multi-server discovery + "servers decide" leader election.

Every Enkelvoud server broadcasts a small JSON beacon on UDP :4205 once a
second:

    {"e": "enkelvoud-peer/1", "id": "pc-...", "name": "Enkelvoud",
     "priority": 1, "leader": true, "httpPort": 80, "uptimeMs": 1234,
     "version": "3.9.1.2"}

Election is deterministic -- no consensus round is needed:

  * rank every ALIVE server by (priority, id) ascending (1 = highest);
  * the lowest rank is the LEADER;
  * a peer is alive while its beacon is younger than PEER_DEAD_MS.

Only the leader answers node discovery and accepts node registrations. A
standby stops advertising mDNS "Enkelvoud.local" and stops sending audio. When
the leader disappears the next server becomes leader on its own (failover);
when a higher-priority server starts it takes over and the old leader steps
down (takeover).

Priority is persisted in <data-dir>/peers.json. The dashboard's draggable
Servers list sets the whole order through /api/servers/order.
"""

import json
import os
import socket
import threading
import time
import urllib.request
import uuid
from typing import Callable, Dict, List, Optional

from . import constants as C
from .discovery import _broadcast_ip, get_local_ip
from .log_ring import log_error, log_info, log_warn
from .state import now_ms, runtime


def _server_id() -> str:
    """Stable per-machine id: the primary MAC address, prefixed 'pc-'."""
    try:
        mac = uuid.getnode()
        return "pc-%012x" % mac
    except Exception:
        return "pc-unknown"


def elect(candidates: List[dict]) -> dict:
    """Pure election: the alive server with the lowest (priority, -uptime, id).

    Separate from the socket code so it can be unit-tested: the caller passes
    the alive peers plus itself. A missing/odd priority ranks as worst.

    Ties are broken in favour of the server that has been up the longest.
    Without that, two servers at the SAME priority (e.g. the ESP's persisted
    priority left at 1 after a takeover test) were decided purely by machine
    id, and the newcomer could displace a healthy running leader -- which then
    stepped down and stopped streaming.
    """
    def key(p: dict):
        prio = p.get("priority")
        if not isinstance(prio, int) or prio < 1:
            prio = C.MAX_SERVER_PRIORITY
        up = p.get("uptimeMs")
        if not isinstance(up, int) or up < 0:
            up = 0
        return (prio, -up, str(p.get("id", "")))

    return min(candidates, key=key)


def _broadcast_targets() -> List[str]:
    targets = ["255.255.255.255"]
    directed = _broadcast_ip()
    if directed and directed not in targets:
        targets.insert(0, directed)
    return targets


class PeerRegistry:
    def __init__(self) -> None:
        self.id = _server_id()
        self.priority = C.DEFAULT_SERVER_PRIORITY
        self.peers: Dict[str, dict] = {}
        self.lock = threading.Lock()
        self.role = "leader"           # single-server installs are leaders
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._http_port = 80
        self._data_path: Optional[str] = None
        self._on_role_change: Optional[Callable[[str], None]] = None
        self._started = time.monotonic()

    # ---- lifecycle ------------------------------------------------------

    def configure(self, data_path: Optional[str], http_port: int,
                  on_role_change: Optional[Callable[[str], None]] = None) -> None:
        self._data_path = data_path
        self._http_port = http_port
        self._on_role_change = on_role_change
        self._load_priority()

    def start(self) -> None:
        self._stop.clear()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("0.0.0.0", C.PEER_PORT))
            sock.settimeout(0.2)
            self._sock = sock
        except OSError as e:
            # A single server is still a valid (leader) configuration; the
            # election simply never sees peers.
            log_warn("Peer beacon: cannot bind UDP %d (%s) - running as a "
                     "single server", C.PEER_PORT, e)
            self._sock = None
            return
        self._thread = threading.Thread(target=self._run, name="peers",
                                        daemon=True)
        self._thread.start()
        log_info("Peer beacon: UDP %d, id %s, priority %d",
                 C.PEER_PORT, self.id, self.priority)

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # ---- election -------------------------------------------------------

    def is_leader(self) -> bool:
        return self.role == "leader"

    def _uptime_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)

    def _alive_peers(self, now: int) -> List[dict]:
        with self.lock:
            return [p for p in self.peers.values()
                    if now - p["lastSeenMs"] <= C.PEER_DEAD_MS]

    def leader_descriptor(self) -> dict:
        now = now_ms()
        me = {"id": self.id, "name": runtime.server_name,
              "ip": get_local_ip(), "priority": self.priority,
              "uptimeMs": self._uptime_ms(), "self": True}
        peers = [dict(p, self=False) for p in self._alive_peers(now)]
        return elect(peers + [me])

    def _evaluate(self) -> None:
        now = now_ms()
        with self.lock:
            dead = [pid for pid, p in self.peers.items()
                    if now - p["lastSeenMs"] > C.PEER_PRUNE_MS]
            for pid in dead:
                del self.peers[pid]
            alive = [p for p in self.peers.values()
                     if now - p["lastSeenMs"] <= C.PEER_DEAD_MS]
        me = {"id": self.id, "priority": self.priority,
              "uptimeMs": self._uptime_ms()}
        best = elect(alive + [me])
        new_role = "leader" if best["id"] == self.id else "standby"
        if new_role == self.role:
            return
        old = self.role
        self.role = new_role
        runtime.is_leader = (new_role == "leader")
        runtime.peer_servers = self.snapshot()["servers"]
        if new_role == "leader":
            log_info("Peer election: leader now %s (this server took over; "
                     "%d peer(s) alive)", self.id, len(alive))
        else:
            log_warn("Peer election: this server is now STANDBY - %s (%s) "
                     "outranks it (priority %s vs %d); nodes will move there",
                     best.get("name", best["id"]), best.get("ip", "?"),
                     best.get("priority"), self.priority)
        if self._on_role_change is not None:
            try:
                self._on_role_change(new_role)
            except Exception as e:
                log_warn("Peer election: role callback failed: %s", e)

    # ---- priority -------------------------------------------------------

    def set_priority(self, value: int) -> int:
        try:
            v = int(value)
        except (TypeError, ValueError):
            v = C.DEFAULT_SERVER_PRIORITY
        v = max(1, min(C.MAX_SERVER_PRIORITY, v))
        with self.lock:
            self.priority = v
        self._save_priority()
        self._evaluate()
        return v

    def apply_order(self, order: List[str]) -> dict:
        """Apply a preferred-order list of server ids (1 = highest priority).

        The local priority is set directly; each peer gets a POST to its
        /api/servers/priority (best effort -- older servers 404 it).
        """
        results: Dict[str, str] = {}
        for idx, sid in enumerate(order):
            pr = min(idx + 1, C.MAX_SERVER_PRIORITY)
            if not isinstance(sid, str):
                continue
            if sid == self.id:
                self.set_priority(pr)
                results[sid] = "self"
                continue
            with self.lock:
                peer = self.peers.get(sid)
            if not peer:
                results[sid] = "unknown"
                continue
            url = "http://%s:%d/api/servers/priority" % (
                peer.get("ip", ""), int(peer.get("httpPort", 80) or 80))
            try:
                body = json.dumps({"priority": pr}).encode("utf-8")
                req = urllib.request.Request(
                    url, data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST")
                with urllib.request.urlopen(req, timeout=2) as r:
                    r.read(512)
                results[sid] = "ok"
            except Exception as e:
                results[sid] = "failed: %s" % e
        self._evaluate()
        runtime.peer_servers = self.snapshot()["servers"]
        return results

    def _load_priority(self) -> None:
        if not self._data_path:
            return
        try:
            with open(self._data_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            p = data.get("priority")
            if isinstance(p, int) and 1 <= p <= C.MAX_SERVER_PRIORITY:
                self.priority = p
        except (OSError, ValueError):
            pass

    def _save_priority(self) -> None:
        if not self._data_path:
            return
        try:
            tmp = self._data_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"priority": self.priority}, f)
            os.replace(tmp, self._data_path)
        except OSError as e:
            log_warn("Peer beacon: could not persist priority: %s", e)

    # ---- wire -----------------------------------------------------------

    def _run(self) -> None:
        assert self._sock is not None
        last_beacon = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                while True:
                    data, addr = self._sock.recvfrom(2048)
                    self._on_beacon(data, addr)
            except socket.timeout:
                pass
            except OSError:
                break
            if now - last_beacon >= C.PEER_BEACON_INTERVAL_MS / 1000.0:
                last_beacon = now
                self._send_beacon()
                self._evaluate()

    def _send_beacon(self) -> None:
        msg = {
            "e": C.PEER_PROTOCOL,
            "id": self.id,
            "name": runtime.server_name,
            "priority": self.priority,
            "leader": self.is_leader(),
            "httpPort": self._http_port,
            "uptimeMs": int((time.monotonic() - self._started) * 1000),
            "version": C.VERSION,
        }
        data = json.dumps(msg).encode("utf-8")
        targets = _broadcast_targets()
        for target in targets:
            try:
                self._sock.sendto(data, (target, C.PEER_PORT))
            except OSError:
                pass

    def _on_beacon(self, data: bytes, addr) -> None:
        try:
            msg = json.loads(data.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(msg, dict) or msg.get("e") != C.PEER_PROTOCOL:
            return
        pid = msg.get("id")
        if not isinstance(pid, str) or not pid or pid == self.id:
            return
        prio = msg.get("priority")
        if not isinstance(prio, int):
            return
        http_port = msg.get("httpPort")
        if not isinstance(http_port, int) or not (0 < http_port < 65536):
            http_port = 80
        uptime = msg.get("uptimeMs")
        if not isinstance(uptime, int):
            uptime = 0
        name = msg.get("name")
        if not isinstance(name, str):
            name = pid
        peer = {
            "id": pid,
            "name": name[:32],
            "ip": addr[0],
            "priority": max(1, min(C.MAX_SERVER_PRIORITY, prio)),
            "leader": bool(msg.get("leader")),
            "httpPort": http_port,
            "version": str(msg.get("version", ""))[:16],
            "uptimeMs": uptime,
            "lastSeenMs": now_ms(),
        }
        with self.lock:
            prev = self.peers.get(pid)
            peer["seenCount"] = (prev or {}).get("seenCount", 0) + 1
            self.peers[pid] = peer

    # ---- dashboard ------------------------------------------------------

    def snapshot(self) -> dict:
        now = now_ms()
        with self.lock:
            peers = []
            for p in self.peers.values():
                q = dict(p)
                q["alive"] = (now - p["lastSeenMs"]) <= C.PEER_DEAD_MS
                q["ageMs"] = now - p["lastSeenMs"]
                q["self"] = False
                peers.append(q)
        me = {
            "id": self.id, "name": runtime.server_name,
            "ip": get_local_ip(), "priority": self.priority,
            "role": self.role, "self": True, "alive": True,
            "version": C.VERSION, "httpPort": self._http_port,
            "uptimeMs": self._uptime_ms(),
        }
        servers = peers + [me]
        servers.sort(key=lambda s: (s["priority"], s["id"]))
        leader = self.leader_descriptor()
        return {
            "ok": True,
            "self": me,
            "servers": servers,
            "role": self.role,
            "leaderId": leader["id"],
            "leaderIp": leader.get("ip", ""),
            "protocol": C.PEER_PROTOCOL,
        }


# Singleton
peers = PeerRegistry()
