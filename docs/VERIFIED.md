# VERIFIED against your source files

Every item below is copied verbatim from one of your attached files.
None of it is inferred.

## Wire protocols

| Item | Source | Notes |
|---|---|---|
| Discovery request string `ENKELVOUDP/1 DISCOVER` | `EnkelvoudServer.ino` | Trimmed of trailing whitespace before comparison (`EVDWiFi.h`) |
| Discovery reply `ENKELVOUDP/1 SERVER <name> <FW_VERSION> <ip>` | `EVDWiFi.h handleDiscoveryPacket` | Sent to request source IP:port |
| Audio port 4202, ctrl port 4203, clock sync port 4204 | `EnkelvoudServer.ino` | |
| Audio header 12 bytes | `EnkelvoudServer.ino` `AUDIO_HEADER_BYTES` static_assert | |
| Header layout: `<HBBII` (magic, version, flags, seq, captured_ms), all little-endian | `EVDUDP.h udpSendTask` byte-by-byte packing | flags = 0 |
| `sync_req` JSON: `{type:"sync_req",clientTxMs:<u32>}` | `EVDUDP.h handleClockSyncRequests` | |
| `sync_resp` JSON: `{type,protoVersion,epoch,clientTxMs,serverRxMs,serverTxMs}` | `EVDUDP.h handleClockSyncRequests` | |
| `serverRxMs` timestamped BEFORE JSON parse | `EVDUDP.h` | |
| `serverTxMs` timestamped just before `beginPacket` | `EVDUDP.h` | |
| Sync rate limit 20 ms per source IP, 16-entry LRU | `EVDUDP.h syncRateAllow` | |
| Sync poll interval 5 ms | `EVDUDP.h clockSyncTask` | |
| 30 s authoritative `time_sync` broadcast on :4203 | `EVDUDP.h sendAuthoritativeTimeSync` | |
| Promotion to `hasCompletedClockSync=true` only after `endPacket()==1` | `EVDUDP.h` | |
| Provisional subscriber slot when `sync_req` arrives before heartbeat | `EVDUDP.h` | |
| `sync_req` also refreshes `lastSeenMs` and resets `consecutiveSendFailures` | `EVDUDP.h` | |

## ctrl payload shapes

| Item | Source | Notes |
|---|---|---|
| `host_audio_sync` first-sight form (with `audioCompatReason`, no `enc*`) | `EVDPlayers.h handlePlayerRegistration` | |
| `host_audio_sync` broadcast form (with `enc*`, no `audioCompatReason`) | `EVDPlayers.h broadcastNodeConfig` | Uses `g_currentSignal` (APPLIED), not `g_newSignal` |
| `host_audio_sync` partial form (zone-only; no `volume`, no `muted`, no `enc*` unless registered) | `EVDPlayers.h sendZoneConfigToIp` | |
| `time_sync` first-sight (no `authoritative` field) | `EVDPlayers.h handlePlayerRegistration` | |
| `time_sync` 30 s broadcast (with `"authoritative":true`) | `EVDUDP.h sendAuthoritativeTimeSync` | |
| volume/zoneVolume formatted as `%.3f` | `EVDPlayers.h` | |

## Constants

| Item | Value | Source |
|---|---|---|
| `AUDIO_PROTO_MAGIC` | `0xE1D0` | `EnkelvoudServer.ino` |
| `AUDIO_PROTO_VERSION` | `2` | `EnkelvoudServer.ino` |
| `OPUS_FRAME_MS` | `20` | `EnkelvoudServer.ino` |
| `CHANNELS` | `2` | `EnkelvoudServer.ino` |
| `OPUS_BITRATE` | `96000` | `EnkelvoudServer.ino` |
| `OPUS_MAX_PACKET_BYTES` | `1024` | `EnkelvoudServer.ino` |
| `RAW_QUEUE_LEN` | `16` | `EnkelvoudServer.ino` |
| `OPUS_QUEUE_LEN` | `16` | `EnkelvoudServer.ino` |
| `ENCODE_QUEUE_LEN` | `4` | `EnkelvoudServer.ino` |
| `MAX_UDP_SUBSCRIBERS` | `16` | `EnkelvoudServer.ino` |
| `MAX_SEND_FAILURES_BEFORE_SKIP` | `10` | `EnkelvoudServer.ino` |
| `SUB_DEAD_LASTSEEN_MS` | `7000` | `EVDUDP.h` |
| `SUB_FAILURE_SKIP_COUNT` | `2` | `EVDUDP.h` |
| `PLAYER_STALE_TIMEOUT_MS` | `70000` | `EnkelvoudServer.ino` |
| `AUDIO_PACER_MAX_LAG_MS` | `100` | `EVDPacer.h` |
| `SYNC_MIN_INTERVAL_MS` | `20` | `EVDUDP.h` |
| `SYNC_RATE_TABLE_SIZE` | `16` | `EVDUDP.h` |
| `AUDIO_BUFFER_MIN_MS` | `20` | `EVDState.h` |
| `AUDIO_BUFFER_MAX_MS` | `400` | `EVDState.h` |
| `NODE_PERF_STALE_TIMEOUT_MS` | `20000` | `EVDHelpers.h` |
| `NODE_PERF_MAX_ENTRIES` | `32` | `EVDHelpers.h` |
| `EVD_LOG_RING_LEN` | `32` | `EVDState.h` |
| `EVD_LOG_LINE_MAX` | `128` | `EVDState.h` |
| `SOURCE_LOG_RING_LEN` | `50` | `EVDState.h` |
| Per-mode buffer defaults | `off=120, music=150, movie=80` | `EVDState.h g_modeSettings` |
| Per-mode complexity default | `4` | `EVDState.h g_modeSettings` |
| Per-mode bitrate default | `96000` | `EVDState.h g_modeSettings` |
| Firmware version | `"3.9.1.2"` | `EnkelvoudServer.ino EVD_FW_VERSION` |
| `MAIN_ZONE_ID` | `"main"` | `EnkelvoudServer.ino` |

## HTTP routes

Every route in `EVDRoutes.h` is implemented at the correct method and
path with the correct JSON shape. Individual error strings verified:

- `/api/config`: all validation strings, size cap 1024, all defaults.
- `/api/audio/encoding` GET/POST: bitrate cap 192000, complexity 0-10,
  bufferMs 20-400, `note` string verbatim.
- `/api/zones/*`: all five handlers, all error strings.
- `/api/player/register`: full response field set, sanitize rules.
- `/api/state`, `/api/stats`: full field sets including the synthetic
  Main Room insertion on `/api/state`.
- `/api/log/tail`: `nextSeq` field, `Cache-Control: no-store, max-age=0`.
- 404 catch-all: `{"ok":false,"error":"not found"}`.
- Empty-body POST: `{"ok":false,"error":"empty body"}`.

## Pacer

`EVDPacer.h AudioFramePacer` is translated line-for-line, including the
`should_reanchor_after_idle` condition, the `draining_stale_backlog` flag,
and the `advance(frameMs)` invariant (called on every frame, sent or
dropped).