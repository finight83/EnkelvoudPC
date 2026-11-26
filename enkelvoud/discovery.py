"""
enkelvoud.discovery - UDP :4201 responder + mDNS advertisement.

Wire formats:
  request  (node -> server):  b"ENKELVOUDP/1 DISCOVER"  (whitespace-trimmed)
  response (server -> node):  b"ENKELVOUDP/1 SERVER <serverName> <VERSION> <localIp>"

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
        self._http_port = 80
        # [servers-decide] Only the elected leader answers node discovery and
        # owns the mDNS name. See peers.py.
        self._is_leader = True

    def start(self, http_port: int = 80) -> None:
        self._http_port = http_port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # No SO_REUSEADDR: two instances on one PC should fail loudly.
        self._sock.bind(("0.0.0.0", C.DISCOVERY_PORT))
        self._sock.settimeout(0.5)

        self._thread = threading.Thread(
            target=self._run, name="discovery", daemon=True,
        )
        self._thread.start()

        log_info("UDP discovery responder listening on port %d", C.DISCOVERY_PORT)
        if self._is_leader:
            self._setup_mdns()

    def set_leader(self, is_leader: bool) -> None:
        """Switch between leader (answers nodes) and standby (silent).

        The leader owns mDNS 'Enkelvoud.local' and the UDP discovery reply; a
        standby owns neither, so nodes converge on the leader. Called by the
        peer election whenever the role changes.
        """
        if is_leader == self._is_leader:
            return
        self._is_leader = is_leader
        if is_leader:
            log_info("Discovery: LEADER - answering nodes, advertising mDNS")
            self._setup_mdns()
        else:
            log_info("Discovery: STANDBY - not answering nodes")
            self._teardown_mdns()

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

            # [servers-decide] Standby servers stay silent: only the elected
            # leader answers node discovery, so every node lands on it.
            if not self._is_leader:
                continue

            local_ip = get_local_ip()
            reply = (
                f"{C.DISCOVERY_MAGIC_RPY} "
                f"{runtime.server_name} "
                f"{C.VERSION} "
                f"{local_ip}"
            )
            try:
                self._sock.sendto(reply.encode("utf-8"), addr)
            except OSError as e:
                log_warn("Discovery: sendto %s failed: %s", addr, e)
                continue
            log_info("Discovery: replied to %s -> %s", addr[0], local_ip)

    # ---- mDNS -----------------------------------------------------------

    # [mdns-annotation-fix] These used the zeroconf type directly in a class
    # annotation. When zeroconf is not installed the import block leaves the
    # name undefined, and a bare annotation is still EVALUATED at class-body
    # time -- so the module raised NameError on import and took the whole
    # server down, instead of logging "mDNS disabled" as intended.
    _zc: Optional[object] = None
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

            for svc_type, proto, _default_port in C.MDNS_SERVICES:
                full_type = f"{svc_type}.{proto}.local."
                instance = f"{name}.{full_type}"
                info = ServiceInfo(
                    type_=full_type,
                    name=instance,
                    addresses=[ip_bytes],
                    port=self._http_port,
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
        if self._is_leader:
            self._setup_mdns()


# ---- Competing-server detection -------------------------------------------
# Player nodes look for "Enkelvoud.local" (mDNS) and fall back to a UDP
# DISCOVER broadcast, then attach to whichever server answers. So TWO servers
# on one LAN (e.g. the PC and an ESP32-S3 that is still powered) split the
# fleet: the loser looks half-populated even though it is running, and every
# node drifts back to the survivor once the other one stops. This check makes
# that situation obvious at startup instead of as a mystery "3 of 5 nodes".

def _broadcast_ip() -> Optional[str]:
    """Directed broadcast address of the interface carrying the LAN IP."""
    target = get_local_ip()
    try:
        import ipaddress
        import psutil
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family == socket.AF_INET and a.address == target and a.netmask:
                    net = ipaddress.IPv4Network(f"{target}/{a.netmask}",
                                                strict=False)
                    return str(net.broadcast_address)
    except Exception:
        pass
    return None


def probe_other_servers(timeout_ms: int = 1200) -> list:
    """Return any *other* Enkelvoud servers answering UDP DISCOVER on this LAN.

    Each entry is {"ip": str, "reply": str}. The local responder's own reply is
    filtered out by source IP. Sends only the discovery request; it cannot
    disturb nodes (they never listen on :4201) or the other server.
    """
    self_ip = get_local_ip()
    req = C.DISCOVERY_MAGIC_REQ.encode("ascii")
    targets = []
    directed = _broadcast_ip()
    if directed:
        targets.append(directed)
    targets.append("255.255.255.255")

    found: dict = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.2)
        for t in targets:
            try:
                sock.sendto(req, (t, C.DISCOVERY_PORT))
            except OSError:
                pass
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            ip = addr[0]
            if ip == self_ip:
                continue
            body = data.decode("utf-8", "replace").strip()
            if body.startswith(C.DISCOVERY_MAGIC_RPY):
                found[ip] = body
    finally:
        sock.close()
    return [{"ip": ip, "reply": body} for ip, body in found.items()]


# Singleton
discovery = DiscoveryResponder()