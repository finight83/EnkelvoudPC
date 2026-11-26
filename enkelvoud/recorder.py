"""
enkelvoud.recorder - one recording session across the server and its nodes.

Why this exists
---------------
The dashboard's Analyze button recorded only whatever the browser could poll,
node by node, from each node's own page. This module is the master control:
start one session, tell every selected node to start capturing, stop it once,
and get every report back in one payload.

Protocol (see README "Master recording"):

  server -> node   UDP :4203 {"target":ip,"type":"rec_start","recId":"..."}
                   and the same with "rec_stop"
  node -> server   POST /api/player/rec_ack   {"recId":...}      (patched fw)
  server -> node   GET  /api/rec_report?recId=...                (patched fw)
  unpatched node   no ack -> the session polls /api/logs at 1 Hz,
                   so the older firmware still produces a report.

The node buffers into PSRAM on its logging path (no polling, no flash writes);
this side only talks HTTP at start and stop, plus the 1 Hz fallback poller for
nodes that never acked.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

from . import ctrl
from .log_ring import log_error, log_info, log_warn, log_ring
from .state import now_ms, runtime

SAMPLE_MS = 1000            # server-side sample cadence
POLL_MS = 1000              # fallback /api/logs poll cadence for unpatched nodes
ACK_WAIT_MS = 2500          # how long "no ack" means "unpatched / not recording"
REPORT_WAIT_MS = 20000      # wait for node reports after rec_stop
HTTP_TIMEOUT_S = 5.0
MAX_NODE_LINES = 20000

_REC_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_lock = threading.Lock()
_sessions: Dict[str, "_Session"] = {}


def _node_url(ip: str, path: str) -> str:
    """Node HTTP root. Tests monkeypatch this to a local server."""
    return "http://%s%s" % (ip, path)


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
        return r.read()


def _http_get_json(url: str):
    return json.loads(_http_get(url).decode("utf-8", "replace"))


def _merge_poll(existing: List[str], snapshot: List[str]) -> List[str]:
    """Append only the lines after the longest existing-suffix/snapshot-prefix."""
    if not snapshot:
        return existing
    best = 0
    for n in range(1, min(len(existing), len(snapshot)) + 1):
        if existing[-n:] == snapshot[:n]:
            best = n
    return existing + snapshot[best:]


class _Session:
    def __init__(self, rec_id: str, node_ips: List[str], with_server: bool) -> None:
        self.rec_id = rec_id
        self.node_ips = node_ips
        self.with_server = with_server
        self.started_ms = now_ms()
        self.started_wall = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.acked: set = set()
        self.names: Dict[str, str] = {}
        self.polls: Dict[str, List[str]] = {}
        self.reports: Dict[str, dict] = {}
        self.samples: List[dict] = []
        self.logs: List[dict] = []
        self.log_seq = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- background sampling / fallback polling ------------------------

    def start_thread(self) -> None:
        self._thread = threading.Thread(target=self._run, name="recorder",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        last_poll = 0
        while not self._stop.is_set():
            if self.with_server:
                try:
                    self._sample_server()
                except Exception as e:
                    log_warn("Recorder: server sample failed: %s", e)
            if (now_ms() - self.started_ms) > ACK_WAIT_MS:
                if (now_ms() - last_poll) >= POLL_MS:
                    last_poll = now_ms()
                    for ip in self.node_ips:
                        if ip in self.acked:
                            continue
                        self._poll_node(ip)
            self._stop.wait(SAMPLE_MS / 1000.0)

    def _sample_server(self) -> None:
        from . import web_api                      # lazy: avoids an import cycle
        stats = web_api._build_stats_response()
        stream = stats.get("streaming") or {}
        t = (now_ms() - self.started_ms) / 1000.0
        wall = time.strftime("%H:%M:%S", time.gmtime())
        self.samples.append({
            "t": round(t, 2),
            "wall": wall,
            "cpuPct": stats.get("cpuPct"),
            "sysCpuPct": stats.get("sysCpuPct"),
            "ramPct": stats.get("heapUsedPct"),
            "qd": stream.get("rawQueueDepth"),
            "qq": stream.get("opusQueueDepth"),
            "latencyMs": stream.get("avgLatencyMs"),
            "rebuffers": stream.get("rebuffers"),
            "mode": runtime.playback_mode_cstr(),
            "bufferMs": runtime.audio_buffer_ms,
            "clients": len(runtime.snapshot_subscribers(now_ms())),
        })
        entries, next_seq = log_ring.tail(self.log_seq)
        for e in entries:
            self.logs.append({"t": round(t, 2), "level": e["level"],
                              "text": e["text"]})
        self.log_seq = next_seq

    def _poll_node(self, ip: str) -> None:
        try:
            snap = _http_get_json(_node_url(ip, "/api/logs"))
        except (OSError, ValueError, json.JSONDecodeError):
            return
        if not isinstance(snap, list):
            return
        lines = [x for x in snap if isinstance(x, str)]
        merged = _merge_poll(self.polls.get(ip, []), lines)
        if len(merged) > MAX_NODE_LINES:
            merged = merged[-MAX_NODE_LINES:]
        self.polls[ip] = merged

    # ---- stop ----------------------------------------------------------

    def fetch_report(self, ip: str) -> bool:
        """GET /api/rec_report from a patched node. True if stored."""
        try:
            body = _http_get(_node_url(
                ip, "/api/rec_report?recId=" + urllib.parse.quote(self.rec_id)))
            data = json.loads(body.decode("utf-8", "replace"))
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(data, dict) or not isinstance(data.get("lines"), list):
            return False
        got = data.get("recId")
        if got is not None and got != self.rec_id:
            return False
        self.reports[ip] = {
            "source": "node",
            "lines": [x for x in data["lines"] if isinstance(x, str)][:MAX_NODE_LINES],
            "dropped": int(data.get("dropped") or 0),
            "startedMs": data.get("startedMs"),
            "endedMs": data.get("endedMs"),
        }
        return True

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


# ---- Public API ----------------------------------------------------------

def start(rec_id: str, node_ips: List[str], with_server: bool) -> Tuple[int, dict]:
    if not isinstance(rec_id, str) or not _REC_ID_RE.match(rec_id):
        return 400, {"ok": False, "error": "bad recId"}
    ips: List[str] = []
    for ip in node_ips if isinstance(node_ips, list) else []:
        if isinstance(ip, str) and ip not in ips:
            ips.append(ip)
    with _lock:
        if rec_id in _sessions:
            return 409, {"ok": False, "error": "session already running"}
        if _sessions:
            return 409, {"ok": False, "error": "another recording is running"}

    session = _Session(rec_id, ips, bool(with_server))
    with _lock:
        _sessions[rec_id] = session

    for ip in ips:
        ctrl.send_rec_ctrl(ip, rec_id, "rec_start")
    session.start_thread()
    log_info("Recorder: session %s started (%d node(s), server=%s)",
             rec_id, len(ips), bool(with_server))
    return 202, {"ok": True, "recId": rec_id, "nodes": ips,
                 "server": bool(with_server)}


def ack(rec_id: str, ip: str) -> int:
    with _lock:
        session = _sessions.get(rec_id)
    if session is None:
        return 404
    if isinstance(ip, str) and ip:
        session.acked.add(ip)
    return 200


def stop(rec_id: str) -> Tuple[int, dict]:
    with _lock:
        session = _sessions.get(rec_id)
    if session is None:
        return 404, {"ok": False, "error": "unknown session"}

    session.shutdown()
    for ip in session.node_ips:
        ctrl.send_rec_ctrl(ip, rec_id, "rec_stop")

    # Wait for patched nodes to answer /api/rec_report. Unpatched nodes never
    # do; their 1 Hz /api/logs poll is the fallback.
    deadline = now_ms() + REPORT_WAIT_MS
    pending = [ip for ip in session.node_ips if ip not in session.reports]
    while pending and now_ms() < deadline:
        still = []
        for ip in pending:
            if not session.fetch_report(ip):
                still.append(ip)
        pending = still
        if pending:
            time.sleep(0.5)

    nodes = []
    for ip in session.node_ips:
        rep = session.reports.get(ip)
        if rep is None:
            rep = {"source": "poll",
                   "lines": session.polls.get(ip, []),
                   "dropped": 0}
        nodes.append({
            "ip": ip,
            "name": session.names.get(ip, ip),
            "source": rep["source"],
            "lines": rep["lines"],
            "count": len(rep["lines"]),
            "dropped": rep.get("dropped", 0),
        })

    payload = {
        "ok": True,
        "recId": rec_id,
        "startedWall": session.started_wall,
        "durationMs": now_ms() - session.started_ms,
        "server": ({"samples": session.samples, "logs": session.logs}
                   if session.with_server else None),
        "nodes": nodes,
    }
    with _lock:
        _sessions.pop(rec_id, None)
    log_info("Recorder: session %s stopped (%d node report(s))",
             rec_id, len(nodes))
    return 200, payload
