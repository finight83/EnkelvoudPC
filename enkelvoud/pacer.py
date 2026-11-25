"""
enkelvoud.pacer - verbatim translation of EVDPacer.h.

The pacer keeps an ABSOLUTE 20 ms schedule. Ordinary lateness never moves
the origin; only `start()` (idle reanchor) and the drain-stale path do.
"""

from typing import TYPE_CHECKING

from . import constants as C


class AudioFramePacer:
    def __init__(self) -> None:
        self.initialized: bool = False
        self.deadline_ms: int = 0
        self.draining_stale_backlog: bool = False

    def start(self, now_ms: int) -> None:
        self.deadline_ms = now_ms
        self.initialized = True
        self.draining_stale_backlog = False

    def until_ms(self, now_ms: int) -> int:
        """Signed: positive means deadline is in the future."""
        return self.deadline_ms - now_ms

    def lateness_ms(self, now_ms: int) -> int:
        """Signed: positive means deadline is in the past."""
        return now_ms - self.deadline_ms

    def should_reanchor_after_idle(self, packet_gap: bool, queued_frames: int) -> bool:
        return packet_gap and queued_frames == 0

    def prepare_frame(
        self, now_ms: int, frame_ms: int, queued_frames: int, packet_gap: bool,
    ) -> bool:
        """
        Return True to send this frame, False to drop it.

        Always advances the absolute deadline by one slot, whether the frame
        is sent or dropped. This is the key invariant: the pacer drains
        stale backlog rather than transmitting a catch-up burst.
        """
        if self.should_reanchor_after_idle(packet_gap, queued_frames):
            self.start(now_ms)

        late = self.lateness_ms(now_ms)
        if late > C.AUDIO_PACER_MAX_LAG_MS:
            self.draining_stale_backlog = True

        if self.draining_stale_backlog and late > 0:
            if queued_frames == 0:
                # C++ : start(nowMs); advance(frameMs); return true;
                self.start(now_ms)
                self.advance(frame_ms)
                return True
            # Drain one slot; drop.
            self.advance(frame_ms)
            return False

        self.draining_stale_backlog = False
        self.advance(frame_ms)
        return True

    def advance(self, frame_ms: int) -> None:
        self.deadline_ms += frame_ms


def audio_sample_timestamp_ms(start_ms: int, frames: int, sample_rate: int) -> int:
    """EVDPacer.h inline audioSampleTimestampMs(). Unused in v1 sender but kept
    for parity — the ESP32 side may call it from future nodes."""
    return start_ms + (frames * 1000) // sample_rate