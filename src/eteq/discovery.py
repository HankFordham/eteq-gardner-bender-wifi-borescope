"""Finding the camera on its own hotspot.

The camera broadcasts a 32-byte beacon from its port 1000 to ``255.255.255.255:2000``
once per second. The phone app listens for it but does not actually need it: it
hardcodes ``192.168.2.103``. We prefer the beacon because other units of the same
family hand out different addresses, and fall back to the default gateway, which
on these hotspots is the camera itself.
"""

from __future__ import annotations

import logging
import re
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass

from .protocol import BEACON_MAGIC, BEACON_PORT, BEACON_SIZE, hexdump

log = logging.getLogger(__name__)

DEFAULT_IP = "192.168.2.103"
"""What the vendor app hardcodes, and a reasonable last resort."""


@dataclass
class Beacon:
    """A decoded discovery beacon."""

    ip: str
    name: str
    avol: int
    width: int
    height: int
    source: str

    def describe(self) -> str:
        size = f"{self.width}x{self.height}" if self.width and self.height else "not advertised"
        return f"{self.name} at {self.ip} (audio level {self.avol}, picture size {size})"


def parse_beacon(data: bytes, source_ip: str = "") -> Beacon | None:
    """Decode a beacon datagram, or return None if it is not one.

    Layout: magic ``8713``, 4-byte IPv4 address, 16-byte NUL-padded name,
    big-endian audio level, two spare bytes, then width and height. Real hardware
    sends zeros for the size, in which case the app assumes 640x240.
    """
    if len(data) != BEACON_SIZE or data[:4] != BEACON_MAGIC:
        return None
    ip = ".".join(str(b) for b in data[4:8])
    name = data[8:24].split(b"\0")[0].decode("ascii", errors="replace")
    (avol,) = struct.unpack(">H", data[24:26])
    width, height = struct.unpack(">HH", data[28:32])
    return Beacon(ip=ip, name=name, avol=avol, width=width, height=height, source=source_ip)


def listen_beacon(timeout: float = 4.0, port: int = BEACON_PORT, dump: bool = True) -> Beacon | None:
    """Wait for one beacon. Returns None on timeout or if the port cannot be bound.

    A bind failure almost always means something else already holds UDP 2000, and
    a silent wait almost always means the Windows firewall is dropping the
    broadcast. Both are reported rather than raised, because neither is fatal:
    the caller can still talk to the camera at a known address.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", port))
    except OSError as exc:
        log.error("cannot listen on UDP %d for the beacon: %s", port, exc)
        sock.close()
        return None

    log.info("listening for the camera's beacon on UDP %d for up to %.0fs", port, timeout)
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(1024)
            except TimeoutError:
                break
            except OSError:
                continue
            beacon = parse_beacon(data, addr[0])
            if beacon is None:
                if dump:
                    log.info("ignoring a %d byte datagram from %s that is not a beacon", len(data), addr[0])
                continue
            if dump:
                log.info("beacon from %s:%d\n%s", addr[0], addr[1], hexdump(data))
            log.info("found %s", beacon.describe())
            return beacon
    finally:
        sock.close()

    log.warning(
        "no beacon on UDP %d. Either you are not on the camera's WiFi, or the firewall "
        "is dropping it (run: eteq --install-firewall-rule)",
        port,
    )
    return None


def listen_beacons(timeout: float = 4.0, port: int = BEACON_PORT) -> list[Beacon]:
    """Collect every distinct camera that beacons during the window."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    found: list[Beacon] = []
    try:
        sock.bind(("", port))
    except OSError as exc:
        log.error("cannot listen on UDP %d: %s", port, exc)
        sock.close()
        return found

    deadline = time.monotonic() + timeout
    seen = set()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(1024)
            except (TimeoutError, OSError):
                break
            beacon = parse_beacon(data, addr[0])
            if beacon and beacon.ip not in seen:
                seen.add(beacon.ip)
                found.append(beacon)
    finally:
        sock.close()
    return found


def guess_gateway() -> str | None:
    """Best-effort default gateway, which on these hotspots is the camera.

    Used only when the beacon does not arrive. Any failure returns None.
    """
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["route", "print", "-4"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
                    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", parts[2]):
                        return parts[2]
        else:
            out = subprocess.run(["ip", "route"], capture_output=True, text=True, timeout=10).stdout
            match = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out)
            if match:
                return match.group(1)
    except Exception as exc:  # pragma: no cover - platform dependent
        log.debug("gateway lookup failed: %s", exc)
    return None


def local_ip_for(peer_ip: str) -> str | None:
    """Which of our addresses would be used to reach ``peer_ip``."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((peer_ip, 9))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def resolve_camera(
    explicit_ip: str | None = None,
    discover: bool = True,
    timeout: float = 4.0,
) -> tuple[str, Beacon | None]:
    """Decide which address to talk to.

    An explicitly given address always wins and skips the wait. Otherwise we
    listen for a beacon, then try the default gateway, then fall back to the
    address the vendor app hardcodes.
    """
    if explicit_ip:
        return explicit_ip, None

    if discover:
        beacon = listen_beacon(timeout)
        if beacon:
            return beacon.ip, beacon

    gateway = guess_gateway()
    if gateway:
        log.info("no beacon; trying the default gateway %s, which is usually the camera", gateway)
        return gateway, None

    log.info("no beacon and no gateway; falling back to %s", DEFAULT_IP)
    return DEFAULT_IP, None
