# Enkelvoud PC Server

A Windows-first PC replacement for the ESP32-S3 "Enkelvoud" audio server.

The package is `enkelvoud/`; run it with `python -m enkelvoud` from this
directory (or via `run.bat`). `tests/` and `requirements.txt` sit beside it,
not inside it.

Double-click `run.bat` on a PC with no Python at all: the first run installs a
private Python into `runtime\python` (see "No Python to install" below).

Nodes keep running unchanged: this process speaks the same UDP protocols
(discovery :4201, audio :4202, ctrl :4203, clock sync :4204), serves the
same HTTP API, and serves the same dashboard on port 80.

## What it does

- **Discovery** — answers `ENKELVOUDP/1 DISCOVER` on UDP :4201 with
  `ENKELVOUDP/1 SERVER <name> 3.9.1.2 <ip>`, and advertises itself via
  mDNS (`_http._tcp` and `_enkelsrv._tcp` on port 80).
- **Node management** — implements `/api/player/register`, `/api/node_stats`,
  zone CRUD, and pushes `host_audio_sync` + `time_sync` over UDP :4203.
  Per-node volume, mute, and zone state are mirrored from the ESP32 behaviour.
- **Clock sync** — the exact `sync_req` / `sync_resp` JSON exchange on
  UDP :4204, with the 20 ms per-IP rate limit, the 5 ms poll, and the
  30 s authoritative `time_sync` broadcast. Nodes are only marked
  audio-eligible after a `sync_resp` is successfully sent.
- **Audio** — captures the default Windows output device via WASAPI
  loopback (`pyaudiowpatch`), resamples to 48 kHz stereo int16, encodes
  Opus 20 ms frames, and sends the 12-byte-header datagrams to each
  subscribed node at one frame per 20 ms with a stale-frame dropper that
  matches `EVDPacer.h` verbatim.
- **Dashboard** — a PC-native web UI (Zones, Advanced, Update) served from
  `web/dashboard.html`. Master volume is controlled by Windows; the PC itself
  is the audio source (always captures all PC audio).

## What it doesn't do

- **Source switching** — there is no BT/AUX/USB selector; the PC captures all
  of its own audio (WASAPI loopback) and is itself the source.
- **Network configuration** — the PC is assumed to be on an already-configured
  network; there is no Wi-Fi setup UI or route.
- **Server self-update** — the PC app is updated by replacing its files; there
  is no over-the-air path for the server itself. Player nodes *are* updated
  from the Update tab (see "Firmware updates").
- **Master volume/mute** — controlled by Windows, not the server.
- **Per-room Quiet** — implemented as a second Opus stream: rooms with Quiet on
  get the band-passed encode (400 Hz–9.5 kHz + peak limiter, ported from
  `EVDAudio.h`), every other room gets the plain one. Costs a second encoder
  while at least one room has it on.
- **DLNA/UPnP** — `/api/dlna` reflects a `--dlna on|off` flag but no
  SSDP/renderer is implemented.
- **Server restart** — `/api/restart` returns `{"ok":true}`; pass
  `--allow-restart` to make it actually exit the process.

## Requirements

- Windows 10/11 (WASAPI loopback capture via `pyaudiowpatch`).
- Nothing else. No Python install: `run.bat` / `install.bat` fetch a private
  Python into `runtime\python` on first use (see "No Python to install").
- Administrator for port 80, or use `--port 8080`.

Nothing else. The Opus library comes in the Python package (see below).

## Opus setup — nothing to install by hand

`requirements.txt` installs `opuslib-next-bundled`, a wheel that carries the
native library itself (`opuslib_next/_native/opus.dll` on Windows, the
`.so`/`.dylib` elsewhere). There is no `libopus` to install on the system and
no binary to download from a website.

If it is missing anyway (an old venv, or someone installed only `opuslib`),
the server fixes it at startup: `opus_runtime.ensure_opus()` checks for a
working encoder and runs `pip install opuslib-next-bundled` itself. The
startup log says which way it went:

    I Encoder: Opus ready (opuslib_next)
    E Encoder: no Opus library available ... pip install opuslib-next-bundled

To check or provision it without starting the server:

    python opus_runtime.py --check      # exit 0 if a working encoder exists
    python opus_runtime.py --install    # install it now if it is missing

A system `libopus` is still honoured: `opuslib` from Debian/Ubuntu
(`apt install libopus0`), Fedora (`dnf install opus`) or macOS
(`brew install opus`) is used when `opuslib` is importable, and then nothing
is downloaded.

## No Python to install — the launcher brings its own

`run.bat` used to open with *"No Python 3.10 or newer found"* on any PC that
had no Python, and told you to go and install one. It no longer does that.

On the first run it downloads the official Windows **embeddable** Python from
python.org (an 11 MB zip — not an installer), unpacks it into
`runtime\python`, enables `site-packages` in its `pythonXY._pth`, bootstraps
pip with `get-pip.py`, and installs `requirements.txt` into it. That is about
60 MB of downloads and roughly 250 MB on disk, once; every later run is
offline and starts immediately.

Nothing is installed system-wide: no installer, no registry writes, no PATH
change, no service, no admin rights needed for the Python part. Delete the
`runtime\` folder and the PC is exactly as it was.

That also makes the folder portable: once `runtime\` has been built, copy the
whole project folder to any other 64-bit Windows 10/11 PC and `run.bat` there
starts with no install and no network access at all.

Do it explicitly (nicer than having it happen inside a server launch, because
a download problem is then its own visible step):

    install.bat                              # or double-click it
    install.bat -Force                       # wipe runtime\python, download again
    install.bat -PythonVersion 3.12.16       # pin a different release

The launchers then pick an interpreter in this order:

1. `runtime\python\python.exe` — the bundled runtime above.
2. `.venv\Scripts\python.exe` — only if you made one on purpose.
3. `py -3` / `python` / `python3` on PATH, or the usual
   `%LocalAppData%\Programs\Python\Python31x` folders — a system Python 3.10+,
   used only if the bundled one is not there.

Which one won is printed on the second line: `[run.bat] Python: bundled
runtime\python`. `ENKELVOUD_NO_BOOTSTRAP=1` skips step 1's install entirely
(no network access, no `runtime\` folder created).

### Offline / air-gapped PCs

The first run needs internet unless you carry the files over. Put these next
to `run.bat` on the offline PC:

    vendor\python-3.13.15-embed-amd64.zip    # python.org/ftp/python/3.13.15/
    vendor\get-pip.py                        # bootstrap.pypa.io/get-pip.py
    vendor\wheels\*.whl                      # pip download -r requirements.txt -d vendor\wheels

`install.bat` uses the zip instead of downloading it, takes `get-pip.py` from
`vendor\` if present, and installs from `vendor\wheels` with `--no-index` when
that folder has wheels. On Windows on ARM take the `-embed-arm64.zip` variant
(the script picks the right one when it downloads). The pinned version list is
at the top of `tools\setup_runtime.ps1`; `-PythonVersion` and
`ENKELVOUD_PY_VERSION` override it.

## Run

Double-click `run.bat`, or from a terminal:

    run.bat                    # normal run, HTTP on :80 (needs admin)
    run.bat --port 8080        # non-admin run
    run.bat --name MyHome --dlna on

It keeps the console open on exit and prints why it stopped, so a window
that closes is never silent. It runs on the bundled Python described
above, so there is nothing to install first -- no Python, no `libopus`, no
DLL (see "Opus setup").

Or invoke an interpreter directly:

    runtime\python\python.exe -m enkelvoud --name Enkelvoud --port 8080

`--port` above 65535 is not rejected: Python masks it, so `--port 99999`
silently means `99999 & 0xFFFF`. Use a port you mean, between 1 and 65535.

Open `http://localhost:8080/` in a browser. Point a node at this machine's
IP on the LAN (the node discovers via UDP :4201 or the dashboard lists it
via `/api/discover`).

### Manual venv (optional, e.g. a Linux dev box)

    py -3 -m venv .venv
    .venv\Scripts\activate              # POSIX: . .venv/bin/activate
    python -m pip install -r requirements.txt

`run.bat` prefers `runtime\python` over `.venv`, so delete `runtime\` (or set
`ENKELVOUD_NO_BOOTSTRAP=1`) if you want a hand-made `.venv` to be used.

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

## Firmware updates (player nodes)

Update tab: tick the nodes, pick a `node_<version>.bin`, press **Upload**. The
server stores the file in `data/ota_spool/`, then pushes it to each ticked node
one at a time over plain HTTP — `POST http://<node>:80/api/ota`, multipart body
with one `fw` part, 512-byte writes, 3 attempts, 8 s reply wait. That is the
same request the ESP32 server sends, so node firmware needs no change.

`GET /api/ota/plan` (spool files + nodes), `GET /api/ota/status` (progress,
completed/failed), `POST /api/ota/apply`
(`{"nodes":["<ip>"],"nodeFile":"node_<version>.bin"}`), `POST /api/ota/upload`
(multipart), `POST /api/ota/delete`. There is no server self-flash: a PC app is
updated by replacing its files.

## Master recording (server + nodes)

**Analyze** in the top bar opens a picker: tick *This server* and/or any player
nodes, press **Start**, and later **Stop & build report**. The server samples
its own CPU/RAM/queues/logs at 1 Hz and merges one report per node into a
single text report (copy or download).

Node side: `rec_start` / `rec_stop` ctrl datagrams (UDP :4203, with a `recId`
the node echoes back). Patched firmware acknowledges with
`POST /api/player/rec_ack` and serves `GET /api/rec_report?recId=...`; the
capture lives in the node's otherwise-unused PSRAM (12000 × 160 B ≈ 1.9 MB),
is written from the existing log path (normal logging unchanged) and is
streamed back in ~1 KB chunks. Nodes without that firmware never ack, so the
server falls back to polling their `/api/logs` ring at 1 Hz — 50-line window,
so keep the node log level at 1–2 for those.

## 10-band equalizer

**EQ** in the top bar opens ten vertical sliders — ISO octave centres
31.5 Hz…16 kHz, ±12 dB in 0.5 dB steps — an **enable/disable** toggle and a
**Flat** reset. The filter runs on the PCM frame *before* Opus encoding, so
both the plain and the Quiet stream carry it (EQ → band-pass → limiter). Bands
above the Opus Nyquist are skipped, so the 16 kHz slider does nothing at the
24 kHz sample rate. State persists in `data/eq.json`; when every band is at
0 dB the filter is bypassed and the frame passes through bit-exact.

## Persistent state

- `data/zones.json` — zone definitions (volume, mute and per-room quiet).
- `data/encoders.json` — per-mode encoder presets (`music`, `movie`; music is
  the default), plus the currently selected mode.
- `data/ota_spool/` — uploaded player firmware images (`node_<version>.bin`).
- `data/eq.json` — equalizer state (enabled flag + ten band gains in dB).

Neither file is required at first run; missing files trigger defaults.

## Logging

All `log_info` / `log_warn` / `log_error` messages go to stdout AND are
buffered in a 32-entry ring exposed at `/api/log/tail?since=N`. The
dashboard's Advanced → Status tab polls this endpoint. Verbosity is
controlled via `/api/log/level` (POST `{"level":0..3}`). Log line format
matches the ESP32: `[INFO ] <ms> ms | <text>`.

## Known limitations

- `capture.py` resamples to 48 kHz using `scipy.signal.resample_poly`.
  Bit-exact capture resampling is not required for node compatibility, only
  the wire format is.
- The dashboard is served from `web/dashboard.html` (no embedded copy or
  `EVDCTRL.h` header).