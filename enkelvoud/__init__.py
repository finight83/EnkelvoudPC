"""
enkelvoud - PC server for the Enkelvoud audio system.

Speaks the same wire protocols as the ESP32-S3 server firmware
(EnkelvoudServer / EVDCTR) so existing nodes need no changes:

  UDP 4201  discovery   request "ENKELVOUDP/1 DISCOVER"
  UDP 4202  audio       Opus, 12-byte header, 20 ms frames
  UDP 4203  ctrl        host_audio_sync / time_sync JSON
  UDP 4204  clock sync  sync_req / sync_resp RTT exchange
  TCP 80    HTTP        the same /api/* surface as EVDRoutes.h

Entry point:  python -m enkelvoud [--port 8080] [--name Enkelvoud]
"""

__version__ = "3.9.1.2"
