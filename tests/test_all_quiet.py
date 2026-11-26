"""Global Quiet Mode: the action-bar toggle puts every assigned room on the
filtered stream, while per-room flags keep working underneath."""

import unittest

from enkelvoud.state import Zone, runtime


class GlobalQuietTest(unittest.TestCase):
    def setUp(self):
        with runtime.zones_lock:
            self._zones = list(runtime.zones)
        self._quiet_mode = runtime.quiet_mode

    def tearDown(self):
        with runtime.zones_lock:
            runtime.zones[:] = self._zones
        runtime.quiet_mode = self._quiet_mode

    def test_global_quiet_covers_every_room(self):
        with runtime.zones_lock:
            runtime.zones[:] = [
                Zone(id="a", name="A", node_ips=["10.0.0.1"], quiet=False),
                Zone(id="b", name="B", node_ips=["10.0.0.2"], quiet=True),
            ]

        runtime.quiet_mode = False
        self.assertEqual(runtime.quiet_node_ips(), {"10.0.0.2"},
                         msg="off: only the per-room Quiet flag counts")

        runtime.quiet_mode = True
        self.assertEqual(runtime.quiet_node_ips(), {"10.0.0.1", "10.0.0.2"},
                         msg="on: every assigned room gets the filtered stream")

    def test_global_quiet_ignores_rooms_without_nodes(self):
        with runtime.zones_lock:
            runtime.zones[:] = [
                Zone(id="a", name="A", node_ips=["10.0.0.1"]),
                Zone(id="b", name="B", node_ips=[]),
            ]
        runtime.quiet_mode = True
        self.assertEqual(runtime.quiet_node_ips(), {"10.0.0.1"})


if __name__ == "__main__":
    unittest.main()
