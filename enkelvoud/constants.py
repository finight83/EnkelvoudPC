"""
enkelvoud.constants - every #define and const from the ESP32 source, verbatim.

Verified against:
  EnkelvoudServer.ino  (EVDConfig.h merged in)
  EVDUDP.h
  EVDPacer.h
  EVDState.h
  EVDHelpers.h
  EVDWiFi.h
  EVDPlayers.h
  EVDSTS.h
  EVDZones.h
  EVDRoutes.h
  EVDCTRL.h

Anything marked INFERRED is a PC-side design choice, not taken from your files.
"""

# ---- Server identity -------------------------------------------------------
# The PC server reports a plain "version": there is no microcontroller
# firmware here. The string is still sent in the UDP discovery reply, where
# the ESP32 player nodes expect the same field layout as the ESP32 server.
# 3.9.2.0: P3 - multi-server peer beacons + "servers decide" election.
VERSION = "3.9.2.0"

# ---- Audio wire protocol ---------------------------------------------------
# EnkelvoudServer.ino
AUDIO_PROTO_MAGIC   = 0xE1D0
AUDIO_PROTO_VERSION = 2
AUDIO_HEADER_BYTES  = 12
# flags byte is hardcoded to 0 in EVDUDP.h udpSendTask()
# [wire-flag-unicast] Byte 3 of the audio header. The firmware sets
# AUDIO_FLAG_UNICAST (0x01) on EVERY frame it sends (EVDUDP.h:262) and a
# current player drops any non-zero flag byte that lacks that bit
# (EnkelvoudESPPlayer/EVDPNet.h, [wire-flag-unicast]). Sending 0 relies on
# the "build that predates the flag" branch, which exists for old servers
# and is not what a current server does. Audio here is unicast per
# subscriber, so 0x01 is always correct.
AUDIO_HEADER_FLAGS  = 0x01

# ---- Opus format -----------------------------------------------------------
OPUS_SAMPLE_RATE_48K     = 48000
OPUS_SAMPLE_RATE_24K     = 24000
OPUS_SAMPLE_RATE_DEFAULT = OPUS_SAMPLE_RATE_48K
OPUS_FRAME_MS            = 20
CHANNELS                 = 2
OPUS_BITRATE             = 96000
OPUS_MAX_PACKET_BYTES    = 1024

# Source pipeline reference rate (pre-resample). SOURCE_SAMPLE_RATE in EVDConfig.h.
SOURCE_SAMPLE_RATE = 48000

# ---- libopus enum values (must match opus_defines.h) -----------------------
OPUS_APPLICATION_VOIP                = 2048
OPUS_APPLICATION_AUDIO               = 2049
OPUS_APPLICATION_RESTRICTED_LOWDELAY = 2051

OPUS_SIGNAL_VOICE = 3001
OPUS_SIGNAL_MUSIC = 3002
OPUS_AUTO         = -1000

# ---- Queues ----------------------------------------------------------------
RAW_QUEUE_LEN    = 16
OPUS_QUEUE_LEN   = 16
ENCODE_QUEUE_LEN = 4

# ---- UDP ports -------------------------------------------------------------
DISCOVERY_PORT      = 4201
AUDIO_UDP_PORT      = 4202
CTRL_UDP_PORT       = 4203
CLOCK_SYNC_UDP_PORT = 4204

# ---- Multi-server peer beacons (P3: servers decide) ------------------------
# Every Enkelvoud server broadcasts a JSON beacon on PEER_PORT once a second
# and elects the alive server with the lowest (priority, id) as the leader.
# Only the leader answers node discovery / accepts registrations.
PEER_PORT               = 4205
PEER_PROTOCOL           = "enkelvoud-peer/1"
PEER_BEACON_INTERVAL_MS = 1000
PEER_DEAD_MS            = 5000    # alive while a beacon is younger than this
PEER_PRUNE_MS           = 60000   # drop dead peers from the dashboard table
DEFAULT_SERVER_PRIORITY = 1       # 1 = highest; this server is preferred
MAX_SERVER_PRIORITY     = 99

# ---- Discovery strings -----------------------------------------------------
DISCOVERY_MAGIC_REQ = "ENKELVOUDP/1 DISCOVER"
DISCOVERY_MAGIC_RPY = "ENKELVOUDP/1 SERVER"

# ---- Subscriber table ------------------------------------------------------
MAX_UDP_SUBSCRIBERS           = 16
MAX_SEND_FAILURES_BEFORE_SKIP = 10
SUB_DEAD_LASTSEEN_MS          = 7000
SUB_FAILURE_SKIP_COUNT        = 2
PLAYER_STALE_TIMEOUT_MS       = 70000

# ---- Clock sync ------------------------------------------------------------
SYNC_RATE_TABLE_SIZE            = 16
SYNC_MIN_INTERVAL_MS            = 20
SYNC_POLL_MS                    = 5          # vTaskDelay(5) in clockSyncTask
TIME_SYNC_BROADCAST_INTERVAL_MS = 30000

# ---- Pacer -----------------------------------------------------------------
AUDIO_PACER_MAX_LAG_MS = 100

# ---- Modes -----------------------------------------------------------------
MODE_OFF   = 0
MODE_MUSIC = 1
MODE_MOVIE = 2
MODE_COUNT = 3
MODE_NAMES = ["off", "music", "movie"]

# ---- Audio buffer bounds ---------------------------------------------------
AUDIO_BUFFER_MIN_MS = 20
AUDIO_BUFFER_MAX_MS = 400

# ---- Zones -----------------------------------------------------------------
MAIN_ZONE_ID = "main"

# ---- Log ring --------------------------------------------------------------
EVD_LOG_RING_LEN = 32
EVD_LOG_LINE_MAX = 128

# ---- Node perf table -------------------------------------------------------
NODE_PERF_STALE_TIMEOUT_MS = 20000
NODE_PERF_MAX_ENTRIES      = 32

# ---- Defaults --------------------------------------------------------------
DEFAULT_SERVER_NAME   = "Enkelvoud"
DEFAULT_MASTER_VOLUME = 100
DEFAULT_MASTER_MUTED  = False

# ---- Per-mode encoder presets ---------------------------------------------
# EVDState.h g_modeSettings[] initializer. Field order matches ModeEncSettings:
#   bitrate, complexity, vbr, vbrConstraint, signal, application, sampleRate, bufferMs
MODE_ENCODE_DEFAULTS = [
    {  # MODE_OFF
        "bitrate": 96000, "complexity": 4, "vbr": True, "vbr_constraint": True,
        "signal": OPUS_SIGNAL_MUSIC, "application": OPUS_APPLICATION_AUDIO,
        "sample_rate": 48000, "buffer_ms": 120,
    },
    {  # MODE_MUSIC
        "bitrate": 96000, "complexity": 4, "vbr": True, "vbr_constraint": True,
        "signal": OPUS_SIGNAL_MUSIC, "application": OPUS_APPLICATION_AUDIO,
        "sample_rate": 48000, "buffer_ms": 150,
    },
    {  # MODE_MOVIE
        "bitrate": 96000, "complexity": 4, "vbr": True, "vbr_constraint": True,
        "signal": OPUS_SIGNAL_MUSIC, "application": OPUS_APPLICATION_AUDIO,
        "sample_rate": 48000, "buffer_ms": 80,
    },
]

# ---- mDNS services ---------------------------------------------------------
# EVDWiFi.h MDNS.addService calls
MDNS_SERVICES = [("_http", "_tcp", 80), ("_enkelsrv", "_tcp", 80)]

# ---- Discovery reply target -----------------------------------------------
# INFERRED: reply is sent to the request's source port (per EVDWiFi.h)
DISCOVERY_REPLY_TO_SOURCE_PORT = True

# ---- Clock sync poll interval (wall clock) --------------------------------
CLOCK_SYNC_POLL_INTERVAL_MS = SYNC_POLL_MS