"""Master recording: session flow against a fake node HTTP endpoint.

The node firmware is not built here; these tests stand a small HTTP server in
for it so the server-side session, ack path, report fetch and the /api/logs
poll fallback are all exercised.
"""

import json
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from enkelvoud import recorder
from enkelvoud.state import runtime


class _FakeNode:
    """Minimal /api/logs + /api/rec_report endpoint."""

    def __init__(self, report_lines, log_snapshots):
        self.report_lines = report_lines
        self.log_snapshots = log_snapshots      # list of snapshots, consumed per poll
        self.log_calls = 0
        self.report_calls = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                if u.path == "/api/logs":
                    outer.log_calls += 1
                    idx = min(outer.log_calls - 1, len(outer.log_snapshots) - 1)
                    body = json.dumps(outer.log_snapshots[idx]).encode()
                elif u.path == "/api/rec_report":
                    if outer.report_lines is None:      # unpatched firmware
                        self.send_response(404)
                        self.end_headers()
                        return
                    outer.report_calls += 1
                    q = urllib.parse.parse_qs(u.query)
                    rid = (q.get("recId") or [""])[0]
                    body = json.dumps({
                        "recId": rid,
                        "nodeId": "aa:bb",
                        "name": "fake",
                        "startedMs": 1,
                        "endedMs": 2,
                        "dropped": 0,
                        "lines": outer.report_lines,
                    }).encode()
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class RecorderTest(unittest.TestCase):
    def setUp(self):
        self._orig_url = recorder._node_url
        self._orig = (recorder.SAMPLE_MS, recorder.POLL_MS,
                      recorder.ACK_WAIT_MS, recorder.REPORT_WAIT_MS)
        recorder.SAMPLE_MS = 50
        recorder.POLL_MS = 50
        recorder.ACK_WAIT_MS = 100
        recorder.REPORT_WAIT_MS = 1500
        self._saved_zones = runtime.zones
        runtime.zones = []

    def tearDown(self):
        recorder._node_url = self._orig_url
        (recorder.SAMPLE_MS, recorder.POLL_MS,
         recorder.ACK_WAIT_MS, recorder.REPORT_WAIT_MS) = self._orig
        runtime.zones = self._saved_zones

    def _point_at(self, node):
        recorder._node_url = lambda ip, path, p=node.port: "http://127.0.0.1:%d%s" % (p, path)

    def test_unpatched_node_falls_back_to_logs_poll(self):
        node = _FakeNode(None, [["a", "b"], ["a", "b", "c"]])
        self._point_at(node)
        try:
            status, body = recorder.start("rt-poll", ["127.0.0.1"], False)
            self.assertEqual(status, 202, body)
            # No ack: the poller must pick /api/logs up after ACK_WAIT_MS.
            threading.Event().wait(0.45)
            status, payload = recorder.stop("rt-poll")
        finally:
            node.close()
        self.assertEqual(status, 200, payload)
        self.assertGreaterEqual(node.log_calls, 1)
        node_rep = payload["nodes"][0]
        self.assertEqual(node_rep["source"], "poll")
        self.assertEqual(node_rep["lines"], ["a", "b", "c"])

    def test_patched_node_report_is_preferred(self):
        node = _FakeNode(["line-1", "line-2"], [["polled"]])
        self._point_at(node)
        try:
            status, _ = recorder.start("rt-node", ["127.0.0.1"], False)
            self.assertEqual(status, 202)
            recorder.ack("rt-node", "127.0.0.1")
            status, payload = recorder.stop("rt-node")
        finally:
            node.close()
        self.assertEqual(status, 200)
        node_rep = payload["nodes"][0]
        self.assertEqual(node_rep["source"], "node")
        self.assertEqual(node_rep["lines"], ["line-1", "line-2"])
        self.assertEqual(node.report_calls, 1)

    def test_server_section_samples_and_logs(self):
        node = _FakeNode([], [])
        self._point_at(node)
        try:
            status, _ = recorder.start("rt-srv", [], True)
            self.assertEqual(status, 202)
            threading.Event().wait(0.30)
            status, payload = recorder.stop("rt-srv")
        finally:
            node.close()
        self.assertEqual(status, 200)
        server = payload["server"]
        self.assertTrue(server["samples"], "no server samples captured")
        self.assertIn("mode", server["samples"][0])

    def test_bad_recid_and_duplicate_session(self):
        self.assertEqual(recorder.start("bad id!", [], True)[0], 400)
        status, _ = recorder.start("rt-dup", [], False)
        self.assertEqual(status, 202)
        self.assertEqual(recorder.start("rt-other", [], False)[0], 409)
        recorder.stop("rt-dup")


if __name__ == "__main__":
    unittest.main()
