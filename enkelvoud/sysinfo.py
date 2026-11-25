"""
enkelvoud.sysinfo - stable CPU sampling for the PC dashboard.

`psutil.cpu_percent(interval=None)` and `psutil.Process.cpu_percent(interval=None)`
report the usage *since the previous call*: every call resets the baseline.
Calling them directly from two HTTP handlers therefore made the readings
meaningless whenever the dashboard polled /api/state and /api/stats close
together — one request got the real window, the other got a few milliseconds
and reported ~0 %. That is the "CPU 0 % ... 100 % ... 0 %" flapping seen in the
first diagnostic recording, and the reason the timeline was mostly zeroes.

One sampler here, sampled at most once per MIN_INTERVAL_S, gives every caller the
same stable numbers:

  proc - the Enkelvoud server process, percent of one CPU core (can exceed 100
         when several threads keep cores busy; the analog of the ESP32 core %)
  sys  - the whole PC, 0..100

Both are -1 when psutil is unavailable.
"""

import threading
import time

MIN_INTERVAL_S = 1.0

_lock = threading.Lock()
_proc = None
_last_ts = 0.0
_cpu_pct = -1
_sys_cpu_pct = -1


def _prime() -> None:
    """Establish the psutil baselines (the first call always returns 0.0)."""
    global _proc
    try:
        import psutil
        _proc = psutil.Process()
        _proc.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None)
    except Exception:
        _proc = None


def read() -> dict:
    """Return {'proc': int, 'sys': int}, sampled at most once per MIN_INTERVAL_S."""
    global _last_ts, _cpu_pct, _sys_cpu_pct
    with _lock:
        if _proc is not None:
            now = time.monotonic()
            if now - _last_ts >= MIN_INTERVAL_S:
                try:
                    import psutil
                    _cpu_pct = int(round(_proc.cpu_percent(interval=None)))
                    _sys_cpu_pct = int(round(psutil.cpu_percent(interval=None)))
                except Exception:
                    pass
                _last_ts = now
        return {"proc": _cpu_pct, "sys": _sys_cpu_pct}


_prime()
