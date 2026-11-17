"""
enkelvoud.log_ring - log capture ring, matching EVDState.h's gLogRing.

Semantics that must not change (the ESP32 dashboard depends on them):

  - Seq starts at 1 on the first append, monotonic.
  - The ring holds the most recent EVD_LOG_RING_LEN entries.
  - /api/log/tail?since=N returns every entry with seq > N, oldest first.
  - nextSeq in the response is the highest seq currently in the ring.
  - text has NO prefix; the [INFO ] / [WARN ] / [ERROR] prefix is added
    only in the stdout / Serial formatter.
"""

import sys
import threading
import time
from typing import List, Tuple

from . import constants as C


# Every level character written through this module has this stdout prefix,
# matching evdLogPrintf()'s "[%s] %8lu ms | %s\n" format.
_LEVEL_STDOUT = {"I": "INFO ", "W": "WARN ", "E": "ERROR"}


class LogRing:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: List[dict] = [
            {"seq": 0, "ms": 0, "level": "I", "text": ""}
            for _ in range(C.EVD_LOG_RING_LEN)
        ]
        self._write_idx = 0
        self._seq = 0

    def append(self, level: str, text: str) -> None:
        """Append a line. `level` is 'I', 'W', or 'E'."""
        if level not in ("I", "W", "E"):
            level = "I"
        if len(text) > C.EVD_LOG_LINE_MAX - 1:
            text = text[: C.EVD_LOG_LINE_MAX - 1]

        ms = int(time.monotonic() * 1000) & 0xFFFFFFFF

        with self._lock:
            self._seq += 1
            e = self._entries[self._write_idx]
            e["seq"] = self._seq
            e["ms"] = ms
            e["level"] = level
            e["text"] = text
            self._write_idx = (self._write_idx + 1) % C.EVD_LOG_RING_LEN

        # Serial-equivalent stdout output.
        prefix = _LEVEL_STDOUT.get(level, "INFO ")
        try:
            sys.stdout.write(f"[{prefix}] {ms:8d} ms | {text}\n")
            sys.stdout.flush()
        except Exception:
            pass

    def tail(self, since_seq: int) -> Tuple[List[dict], int]:
        """Return (entries_with_seq_gt_since, next_seq)."""
        with self._lock:
            end_seq = self._seq
            if end_seq == 0:
                return [], 0
            oldest_seq = max(1, end_seq - C.EVD_LOG_RING_LEN + 1)
            start_seq = max(oldest_seq, since_seq + 1)

            out: List[dict] = []
            for s in range(start_seq, end_seq + 1):
                idx = (s - 1) % C.EVD_LOG_RING_LEN
                e = self._entries[idx]
                if e["seq"] != s:
                    continue
                out.append({
                    "seq": e["seq"],
                    "ms": e["ms"],
                    "level": e["level"],
                    "text": e["text"],
                })
            return out, end_seq


# Singleton
log_ring = LogRing()


def log_info(fmt: str, *args) -> None:
    log_ring.append("I", fmt % args if args else fmt)


def log_warn(fmt: str, *args) -> None:
    log_ring.append("W", fmt % args if args else fmt)


def log_error(fmt: str, *args) -> None:
    log_ring.append("E", fmt % args if args else fmt)