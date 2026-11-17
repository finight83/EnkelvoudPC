"""
enkelvoud.discovery - UDP :4201 responder + mDNS advertisement.

Wire formats:
  request  (node -> server):  b"ENKELVOUDP/1 DISCOVER"  (whitespace-trimmed)
  response (server -> node):  b"ENKELVOUDP/1 SERVER <serverName> <FW_VERSION> <localIp>"

Response is sent to the REQUEST'S SOURCE IP AND PORT (not to 4201).
mDNS services registered: _http._tcp and _enkelsrv._tcp, both on port 80.

Local IP is the primary interface address (UDP route trick: connect() to a
non-routable destination just resolves the local IP without sending a packet).
"""

import socket
import threading
import time
from typing import Optional

from . import constants as C
from .state import runtime
from .log_ring import log_info, log_warn, log_error

try:
    from zeroconf import ServiceInfo, Zeroconf
    _ZEROCONF_AVAILABLE = True
except ImportError:
    _ZEROCONF_AVAILABLE = False


# ---- Local IP discovery ---------------------------------------------------

_local_ip: Optional[str] = None
_local_ip_lock = threading.Lock()


def get_local_ip() -> str:
    """Return the primary local IPv4 address. Cached after first call."""
    global _local_ip
    with _local_ip_lock:
        if _local_ip is None:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    s.connect(("192.0.2.1", 9))   # TEST-NET-1, no packet sent
                    _local_ip = s.getsockname()[0]
                finally:
                    s.close()
            except OSError:
                _local_ip = "127.0.0.1"
        return _local_ip


def refresh_local_ip() -> str:
    """Force re-detection (use after network change)."""
    global _local_ip
    with _local_ip_lock:
        _local_ip = None
    return get_local_ip()


# ---- Discovery responder -------------------------------------------------

class DiscoveryResponder:
    def __init__(self) -> None:
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # No SO_REUSEADDR: two instances on one PC should fail loudly.
        self._sock.bind(("0.0.0.0", C.DISCOVERY_PORT))
        self._sock.settimeout(0.5)

        self._thread = threading.Thread(
            target=self._run, name="discovery", daemon=True,
        )
        self._thread.start()

        log_info("UDP discovery responder listening on port %d", C.DISCOVERY_PORT)
        self._setup_mdns()

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._teardown_mdns()

    # ---- UDP loop -------------------------------------------------------

    def _run(self) -> None:
        assert self._sock is not None
        request_bytes = C.DISCOVERY_MAGIC_REQ.encode("ascii")
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break

            # Trim trailing whitespace, then compare.
            trimmed = data.rstrip(b"\r\n \t")
            if trimmed != request_bytes:
                continue

            local_ip = get_local_ip()
            reply = (
                f"{C.DISCOVERY_MAGIC_RPY} "
                f"{runtime.server_name} "
                f"{C.FW_VERSION} "
                f"{local_ip}"
            )
            try:
                self._sock.sendto(reply.encode("utf-8"), addr)
            except OSError as e:
                log_warn("Discovery: sendto %s failed: %s", addr, e)
                continue
            log_info("Discovery: replied to %s -> %s", addr[0], local_ip)

    # ---- mDNS -----------------------------------------------------------

    _zc: Optional[Zeroconf] = None
    _service_infos: list = []

    def _setup_mdns(self) -> None:
        if not _ZEROCONF_AVAILABLE:
            log_warn("mDNS disabled: zeroconf library not installed "
                     "(pip install zeroconf)")
            return
        try:
            local_ip = get_local_ip()
            ip_bytes = socket.inet_aton(local_ip)
            name = runtime.server_name

            self._zc = Zeroconf()
            self._service_infos = []

            for svc_type, proto, port in C.MDNS_SERVICES:
                full_type = f"{svc_type}.{proto}.local."
                instance = f"{name}.{full_type}"
                info = ServiceInfo(
                    type_=full_type,
                    name=instance,
                    addresses=[ip_bytes],
                    port=port,
                    properties={},
                    server=f"{name}.local.",
                    host_ttl=120,
                    other_ttl=4500,
                )
                self._zc.register_service(info)
                self._service_infos.append(info)

            log_info("mDNS registered: %s.local on _http._tcp and _enkelsrv._tcp",
                     name)
        except Exception as e:
            log_error("mDNS setup failed: %s", e)

    def _teardown_mdns(self) -> None:
        if self._zc is not None:
            try:
                for info in self._service_infos:
                    try:
                        self._zc.unregister_service(info)
                    except Exception:
                        pass
                self._zc.close()
            except Exception:
                pass
            self._zc = None
            self._service_infos = []

    def restart_mdns(self, new_name: str) -> None:
        """Called by /api/config when serverName changes."""
        runtime.server_name = new_name
        self._teardown_mdns()
        self._setup_mdns()


# Singleton
discovery = DiscoveryResponder()