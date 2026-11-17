# Enkelvoud PC Server

A Windows-first PC replacement for the ESP32-S3 "Enkelvoud" audio server.
Nodes keep running unchanged: this process speaks the same UDP protocols
(discovery :4201, audio :4202, ctrl :4203, clock sync :4204), serves the
same HTTP API, and serves the same dashboard on port 80.

## What it does

- **Discovery** — answers `ENKELVOUDP/1 DISCOVER` on UDP :4201 with
  `ENKELVOUDP/1 SERVER <name> 3.9.1.2 <ip>`, and advertises itself via
  mDNS (`_http._tcp` and `_enkelsrv._tcp` on port 80).
- **Node management** — implements `/api/player/register`,
  `/api/node_stats`, `/api/player/ota_result`, zone CRUD, and pushes
  `host_audio_sync` + `time_sync` over UDP :4203. Per-node volume, mute,
  and zone state are mirrored from the ESP32 behaviour.
- **Clock sync** — the exact `sync_req` / `sync_resp` JSON exchange on
  UDP :4204, with the 20 ms per-IP rate limit, the 5 ms poll, and the
  30 s authoritative `time_sync` broadcast. Nodes are only marked
  audio-eligible after a `sync_resp` is successfully sent.
- **Audio** — captures the default Windows output device via WASAPI
  loopback (`pyaudiowpatch`), resamples to 48 kHz stereo int16, encodes
  Opus 20 ms frames, and sends the 12-byte-header datagrams to each
  subscribed node at one frame per 20 ms with a stale-frame dropper that
  matches `EVDPacer.h` verbatim.
- **Dashboard** — serves the same un-gzipped dashboard HTML as the ESP32.

## What it doesn't do (PC stubs)

- **OTA** — every `/api/ota/*` route returns
  `503 {"ok":false,"error":"OTA not supported on PC server"}` except
  `/api/ota/plan` and `/api/ota/status`, which return the correct shapes
  with `lastError` set to the same string.
- **BT / AUX / USB source board** — `/api/bt/*` return empty, `/api/cmd`
  returns `503 source board not present on PC`, `/api/source` accepts the
  request but does not switch capture device.
- **DLNA/UPnP** — `/api/dlna` reflects a `--dlna on|off` flag but no
  SSDP/renderer is implemented.
- **Wi-Fi settings** — `/api/network/state`, `/save`, `/api/wifi/scan/*`
  return the correct shapes but do not modify network configuration.
- **Server restart** — `/api/restart` returns `{"ok":true}`; pass
  `--allow-restart` to make it actually exit the process.

## Requirements

- Windows 10/11 (WASAPI loopback capture via `pyaudiowpatch`).
- Python 3.10 or newer.
- A `libopus` shared library (see below).
- Administrator for port 80, or use `--port 8080`.

## Opus setup

`opuslib` needs `libopus` at runtime. Two easy options on Windows:

**Option 1 — `opuslib-tools` wheel (recommended):**

    pip install opuslib-tools

This installs a bundled `opus.dll` next to the venv. If Python still
cannot find it, copy `opus.dll` into the venv's `Scripts/` directory.

**Option 2 — system-wide `libopus`:**

Download a prebuilt `opus.dll` from https://opus-codec.org/downloads/
and place it in `C:\Windows\System32\` or next to `python.exe`.

Verify with:

    python -c "import opuslib; e=opuslib.Encoder(48000,2,'audio'); print('ok', e.bitrate)"

## Install

    git clone <this repo> enkelvoud-pc
    cd enkelvoud-pc
    py -3 -m venv .venv
    .venv\Scripts\activate
    python -m pip install --upgrade pip
    pip install -r requirements.txt
    pip install opuslib-tools

Then extract the dashboard:

    python tools/extract_dashboard.py path\to\EVDCTRL.h web\dashboard.html

## Run

    run.bat                    # normal run, HTTP on :80 (needs admin)
    run.bat --port 8080        # non-admin run
    run.bat --name MyHome --dlna on

Or invoke directly:

    python -m enkelvoud --name Enkelvoud --port 8080

Open `http://localhost:8080/` in a browser. Point a node at this machine's
IP on the LAN (the node discovers via UDP :4201 or the dashboard lists it
via `/api/discover`).

## Test plan

Start the server, then in a second terminal:

    pip install requests
    python tests/mock_node.py --server <PC-IP>

The test harness verifies:

1. Discovery reply format.
2. `sync_req` / `sync_resp` field set, echo of `clientTxMs`, `serverRxMs ≤
   serverTxMs`.
3. `/api/player/register` response field set, `audioCompatible == true`.
4. First-sight ctrl packets on :4203 (`host_audio_sync` and `time_sync`
   without `authoritative`). **This test is a soft pass on a
   single-machine setup**: the server sends to the registering node's IP
   and the mock can only bind to that IP on a second host. It logs a
   WARN and does not fail in that case.
5. **Audio header on :4202** — this is the primary compatibility check.
   Verifies 12-byte header, magic `0xE1D0`, version 2, flags 0, seq
   strictly +1 per packet, `captured_ms` monotonic and ~20 ms apart,
   payload ≤ 1024 bytes.
6. `/api/node_stats` reflected in `/api/stats`.

If all six report `PASS`, node firmware will see the PC exactly as it
sees the ESP32 for every code path exercised.

### Interpreting test 4

On the PC itself with a mock at `127.0.0.1`, the ctrl packets go to
`<registering-ip>:4203` which will be `127.0.0.1`. To actually observe
them, run the mock on a different host on the LAN, or run the mock with
`--ctrl-port 4203` after temporarily stopping nothing else from binding
to that port. In practice the audio header test (5) is the load-bearing
one.

## Persistent state

- `data/zones.json` — zone definitions.
- `data/encoders.json` — per-mode encoder presets (`off`, `music`,
  `movie`), plus the currently selected mode.

Neither file is required at first run; missing files trigger defaults.

## Logging

All `log_info` / `log_warn` / `log_error` messages go to stdout AND are
buffered in a 32-entry ring exposed at `/api/log/tail?since=N`. The
dashboard's Advanced → Status tab polls this endpoint. Verbosity is
controlled via `/api/log/level` (POST `{"level":0..3}`). Log line format
matches the ESP32: `[INFO ] <ms> ms | <text>`.

## Known limitations

See `docs/INFERRED.md` and `docs/STUBBED.md`. In short:

- `serverEpoch` seed is inferred (Unix-ms & 0xFFFFFFFF, forced ≥ 1). The
  C++ uses `esp_timer_get_time()/1000`. The only constraint from the files
  is "reboot-unique seed for zone IDs" and "monotonic epoch echoed to
  nodes". Both hold.
- `capture.py` resamples to 48 kHz using `scipy.signal.resample_poly`.
  The ESP32's `ResamplerToOpusRate` (Catmull-Rom with a rate-1 fast path)
  is not reproduced bit-exactly. Bit-exact capture resampling is not
  required for node compatibility, only the wire format is.
- The dashboard is not embedded; it is served from `web/dashboard.html`.
  Regenerate after editing `EVDCTRL.h` with `tools/extract_dashboard.py`.

## Verification status

`docs/VERIFIED.md` lists every behaviour with its source file and line
class. `docs/INFERRED.md` lists every place I had to choose something the
files do not pin down. `docs/STUBBED.md` lists every route that returns
a non-functional but shape-correct response.