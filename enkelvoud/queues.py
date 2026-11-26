"""
enkelvoud.queues - drop-oldest bounded queues + payload types.

The C++ code uses FreeRTOS queues with `xQueueSend(..., 0)` on the producer
side, which drops the NEW frame when the queue is full. That is exactly the
behaviour that causes "stuttering after a WiFi stall": every producer stall
silently discards audio instead of applying backpressure.

On the PC we invert that to *drop-oldest*. Producers never block, consumers
always see the newest audio, and any backlog causes the OLDEST frames to be
discarded first. This is strictly better for real-time audio and cannot make
the node's view of `captured_ms` go backwards.

Queue lengths are the same as the C++:

  encode_queue  ENCODE_QUEUE_LEN = 4   (80 ms)
  opus_queue    OPUS_QUEUE_LEN   = 16  (320 ms)
"""

import threading
from collections import deque
from dataclasses import dataclass
from typing import Generic, Optional, TypeVar

from . import constants as C

T = TypeVar("T")


class DropOldestQueue(Generic[T]):
    def __init__(self, maxlen: int) -> None:
        self._maxlen = maxlen
        self._deque: deque[T] = deque(maxlen=maxlen)
        self._cond = threading.Condition(threading.Lock())
        self.dropped = 0

    def put(self, item: T) -> None:
        with self._cond:
            if len(self._deque) == self._maxlen:
                self.dropped += 1
            self._deque.append(item)     # deque with maxlen drops the oldest
            self._cond.notify()

    def get(self, timeout: Optional[float] = None) -> Optional[T]:
        with self._cond:
            if not self._deque:
                self._cond.wait_for(lambda: bool(self._deque), timeout=timeout)
            if not self._deque:
                return None
            return self._deque.popleft()

    def qsize(self) -> int:
        with self._cond:
            return len(self._deque)


# ---- Payload types --------------------------------------------------------

@dataclass
class EncodeFrame:
    """PCM int16 stereo, 960 samples (20 ms @ 48 kHz). Produced by capture,
    consumed by encoder."""
    pcm: bytes              # 960 * 2 ch * 2 bytes = 3840 B
    captured_ms: int        # 32-bit, matches millis() semantics
    seq: int                # monotonically increasing, starts at 1
    samples: int            # always 960 in v1


@dataclass
class OpusPacket:
    """One encoded Opus frame. Produced by encoder, consumed by sender."""
    opus: bytes
    captured_ms: int
    seq: int


# ---- Singletons (module-level) --------------------------------------------

encode_queue: DropOldestQueue[EncodeFrame] = DropOldestQueue(C.ENCODE_QUEUE_LEN)
opus_queue:   DropOldestQueue[OpusPacket]  = DropOldestQueue(C.OPUS_QUEUE_LEN)

# [quiet-stream] Second Opus stream, produced from the filtered copy of each
# frame for rooms with Quiet Mode on. Same length/latency budget as opus_queue;
# sender.quiet_sender drains it, sender.sender ignores those rooms.
quiet_opus_queue: DropOldestQueue[OpusPacket] = DropOldestQueue(C.OPUS_QUEUE_LEN)