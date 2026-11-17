# INFERRED (choices the files did not fully pin down)

These are the only deviations from a strict translation of your source.
Each item lists what was chosen, why, and what you'd need to change to
make it match your implementation exactly.

## 1. `serverEpoch` seed value

**Chosen:** `int(time.time() * 1000) & 0xFFFFFFFF`, forced ≥ 1.

**Why:** `EVDMain.h` (not provided) seeds `serverEpoch`. From the files
we can see:
- `EVDZones.h` uses `serverEpoch` in zone-ID generation.
- `EVDSTS.h` echoes it in `/api/state` and `/api/stats`.
- `EVDUDP.h` echoes it in `sync_resp` and `time_sync`.
- The stated intent in `EVDZones.h` is "makes the seed reboot-unique".

Unix-ms satisfies all four constraints.

**To match your source exactly:** send `EVDMain.h`'s
`serverEpoch = ...` line and I'll swap the seed computation.

## 2. Default `bootSourceIdx = 2` (USB)

**Chosen:** `boot_source_idx = 2` (USB).

**Why:** No PC source board means BT/AUX have no meaning. `EVDState.h`
has `bootSourceIdx = 0` (BT) as the ESP32 default. On PC, USB (the
capture device) is the natural default.

**To match:** change `state.py` line `self.boot_source_idx: int = 2`
to `0`.

## 3. Queue overflow drops OLDEST frame

**Chosen:** `enkelvoud/queues.py::DropOldestQueue` uses `deque(maxlen=N)`
which drops the oldest item.

**Why:** The ESP32's FreeRTOS `xQueueSend(..., 0)` drops the NEW frame
when the queue is full. That causes the "audio stalls for 200 ms after
WiFi burst" behaviour. On PC we own both sides and can do better. This
is strictly more robust for realtime audio.

**To match:** replace `DropOldestQueue.put` with
`if len(self._deque) < self._maxlen: self._deque.append(item)
else: self.dropped += 1`.

## 4. Encoder recreation is synchronous

**Chosen:** On `application` or `sample_rate` change, the encoder thread
recreates its libopus instance on the next frame (`_recreate()` in
`encoder.py`).

**Why:** The ESP32 uses a pending/drain handshake (`g_pendingOpusSampleRate`
+ `g_encoderSettingsDirty`) because `opus_encode()` runs on a different
task and libopus isn't thread-safe per instance. On PC we own the encoder
thread, so recreation is safe and instant.

**Observable behaviour is identical:** nodes see the same `opusSampleRate`
field on the next `host_audio_sync` broadcast. A ~40 ms audio pause on
rate change is the maximum deviation.

## 5. WASAPI loopback via `pyaudiowpatch`

**Chosen:** `pyaudiowpatch` (Windows-only maintained fork of `pyaudio`
that exposes WASAPI loopback). Fallback to `pyaudio` microphone capture
on non-Windows.

**Why:** `soundcard` is the alternative library that exposes loopback
on Windows. `pyaudiowpatch` has a cleaner re-sample path via `scipy` and
is battle-tested. Neither library is bit-exact to what the ESP32's
`ResamplerToOpusRate` does, but only the wire format matters for node
compatibility.

## 6. Resampling algorithm

**Chosen:** `scipy.signal.resample_poly`.

**Why:** The ESP32's `ResamplerToOpusRate` in `EVDAudio.h` uses a
Catmull-Rom cubic with a ratio-1 fast path. `resample_poly` is a
polyphase FIR that is technically higher quality and has comparable
latency. **The wire format is identical** — only the PCM content pre-Opus
differs slightly. Node firmware does not care.

## 7. mDNS restart on `serverName` change

**Chosen:** `/api/config` with `serverName` calls
`discovery.restart_mdns(new_name)` which tears down and re-registers
the zeroconf services.

**Why:** `EVDPlayers.h handleApiConfig` does the same with
`MDNS.end() / MDNS.begin()`. The observable effect (new `.local` hostname
resolves, old one doesn't) is the same.

## 8. Log ring stdout format

**Chosen:** `[{INFO }|{WARN }|{ERROR}] <ms> ms | <text>`

**Why:** Verbatim from `EVDState.h evdLogPrintf`:
```c
Serial.printf("[%s] %8lu ms | %s\n",
              level == 'E' ? "ERROR" :
              level == 'W' ? "WARN " : "INFO ", ...);