"""Quiet Mode: band-pass/limiter behaviour and the second Opus stream.

The PC has no audio device in CI, so these tests feed PCM straight into the
encoder queue: that exercises the same code path capture would, and proves the
filtered stream is produced only for rooms with Quiet Mode on.
"""

import unittest

import numpy as np

from enkelvoud import constants as C
from enkelvoud.encoder import OpusEncoderThread
from enkelvoud.queues import (
    EncodeFrame, encode_queue, opus_queue, quiet_opus_queue,
)
from enkelvoud.quiet import QuietFilter
from enkelvoud.state import Zone, runtime


def _tone(hz: float, amp: float = 0.4, frames: int = 960, fs: int = 48000) -> bytes:
    t = np.arange(frames) / fs
    x = (amp * 32767.0 * np.sin(2.0 * np.pi * hz * t)).astype(np.int16)
    return np.stack([x, x], axis=1).tobytes()


def _peak(pcm: bytes) -> int:
    return int(np.abs(np.frombuffer(pcm, dtype=np.int16)).max())


def _drain(q) -> None:
    while q.get(timeout=0.01) is not None:
        pass


class QuietFilterTest(unittest.TestCase):
    def test_bandpass_passes_midrange(self):
        f = QuietFilter()
        for _ in range(4):                      # settle the biquad history
            f.process(_tone(1000.0), 48000)
        out = f.process(_tone(1000.0), 48000)
        # Butterworth band-pass is ~unity at 1 kHz (limiter is below 0.85 FS).
        self.assertGreater(_peak(out), 0.6 * _peak(_tone(1000.0)))

    def test_bandpass_cuts_bass_and_treble(self):
        for hz in (100.0, 15000.0):
            f = QuietFilter()
            for _ in range(4):
                f.process(_tone(hz), 48000)
            out = _peak(f.process(_tone(hz), 48000))
            ref = _peak(_tone(hz))
            self.assertLess(out, 0.5 * ref, f"{hz:.0f} Hz not attenuated ({out}/{ref})")

    def test_limiter_caps_peaks(self):
        f = QuietFilter()
        loud = _tone(1000.0, amp=0.99)
        for _ in range(6):
            out = f.process(loud, 48000)
        # Threshold is 0.85 FS; allow a little for the 1 ms attack.
        self.assertLessEqual(_peak(out), int(0.90 * 32767))

    def test_coefficients_follow_sample_rate(self):
        f48 = QuietFilter()
        f48.configure(48000)
        hp48, lp48 = f48._hp, f48._lp
        f24 = QuietFilter()
        f24.configure(24000)
        self.assertEqual(f24._fs, 24000)
        # Same corner frequencies, different coefficients per rate (the
        # firmware's configureQuietFilters() does the same).
        self.assertNotEqual(hp48, f24._hp)
        self.assertNotEqual(lp48, f24._lp)
        # 6 kHz sits inside the band at 24 kHz and must survive.
        for _ in range(4):
            f24.process(_tone(6000.0, fs=24000), 24000)
        out = _peak(f24.process(_tone(6000.0, fs=24000), 24000))
        self.assertGreater(out, 0.5 * _peak(_tone(6000.0, fs=24000)))


class QuietStreamTest(unittest.TestCase):
    """Encoder must emit the filtered stream only for quiet rooms."""

    def setUp(self):
        self._saved = runtime.zones
        runtime.zones = [Zone(id="main", name="Main", volume=100,
                              muted=False, quiet=False, node_ips=["10.9.9.9"])]
        _drain(encode_queue)
        _drain(opus_queue)
        _drain(quiet_opus_queue)
        self._enc = OpusEncoderThread()
        self.assertTrue(self._enc.start(), "Opus encoder unavailable")

    def tearDown(self):
        self._enc.stop()
        runtime.zones = self._saved
        _drain(encode_queue)

    def _frame(self, seq: int) -> EncodeFrame:
        return EncodeFrame(pcm=_tone(1000.0), captured_ms=seq * 20,
                           seq=seq, samples=960)

    def test_no_quiet_stream_when_no_room_is_quiet(self):
        encode_queue.put(self._frame(1))
        self.assertIsNotNone(opus_queue.get(timeout=3))
        self.assertIsNone(quiet_opus_queue.get(timeout=0.3))

    def test_quiet_room_gets_a_second_filtered_stream(self):
        with runtime.zones_lock:
            runtime.zones[0].quiet = True
        encode_queue.put(self._frame(2))
        normal = opus_queue.get(timeout=3)
        quiet = quiet_opus_queue.get(timeout=3)
        self.assertIsNotNone(normal)
        self.assertIsNotNone(quiet)
        # Same frame, separate encoders: timestamps and seq must line up.
        self.assertEqual(normal.captured_ms, quiet.captured_ms)
        self.assertEqual(normal.seq, quiet.seq)
        # The filtered encode of a 1 kHz tone at the same bitrate is a
        # different bitstream, not a copy of the normal packet.
        self.assertNotEqual(normal.opus, quiet.opus)
        self.assertEqual(runtime.quiet_node_ips(), {"10.9.9.9"})


class SenderRoutingTest(unittest.TestCase):
    def test_senders_read_disjoint_queues(self):
        from enkelvoud.sender import quiet_sender, sender
        self.assertIs(sender._queue, opus_queue)
        self.assertIs(quiet_sender._queue, quiet_opus_queue)
        self.assertFalse(sender._quiet_stream)
        self.assertTrue(quiet_sender._quiet_stream)


if __name__ == "__main__":
    unittest.main()
