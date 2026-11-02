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
import threading
import time
from collections.abc import Callable
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


class BeaconWatcher:
    """Watches the camera's once-a-second announcement for the whole session.

    The beacon is the only signal that comes from the camera whether or not it is
    sending video, which makes it a way to tell similar-looking failures apart.
    It has one large catch, learned the hard way: **the camera stops beaconing
    while a client is connected to it**. Silence on its own therefore means
    nothing. Only silence *while no video is arriving either* says the camera has
    actually gone off the air.

    So the watcher is given a way to ask how many frames have arrived, and reads
    the two signals together:

    * beacon silent, frames still arriving: normal. The camera suppresses its
      announcement while it is serving us. Reported once, as information.
    * beacon silent and frames stopped, then the beacon returns a few seconds
      later: the camera lost power and rebooted, which on a battery-powered tool
      means its cells, their contacts or its switch.
    * beacon silent and frames stopped, and the beacon never returns: the camera
      is hung. Its firmware stopped answering anything, which is what this
      hardware does when its encoder jams. Only pulling the batteries clears it.

    Runs on its own thread and only reports when something changes, so a healthy
    session produces one line at most.
    """

    def __init__(
        self,
        port: int = BEACON_PORT,
        gap: float = 3.0,
        frames_fn: Callable[[], int] | None = None,
    ) -> None:
        self.port = port
        self.gap = gap
        self.frames_fn = frames_fn
        self.last_seen: float | None = None
        self.first_beacon: Beacon | None = None
        self.count = 0
        self.outages = 0
        self.longest_outage = 0.0
        self.suppressions = 0
        """Times the beacon went quiet while video kept arriving. Harmless."""
        self.recoveries = 0
        """Times the camera went off the air and then announced itself again."""
        self.encoder_stalls = 0
        """Times the picture stopped while the camera was still announcing itself."""
        self.video_was_flowing = False
        """Whether video ever arrived at all, for honest reporting at the end."""
        self._missing_since: float | None = None
        self._suppressed = False
        self._stalled = False
        self._frames = 0
        self._frames_moved_at: float | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, timeout: float = 2.0) -> None:
        """Start watching, and do not return until the socket is listening.

        Waiting matters: the caller goes straight on to wait for the first
        beacon, and a beacon that arrives before the socket is bound is simply
        dropped by the operating system. That would cost a whole second, or the
        entire discovery window on an unlucky run.
        """
        self._thread = threading.Thread(target=self._run, name="beacon-watch", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)

    def stop(self) -> None:
        self._stop.set()

    def wait_for_first(self, timeout: float) -> Beacon | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            if self.first_beacon is not None:
                return self.first_beacon
            time.sleep(0.1)
        return self.first_beacon

    def _video_is_flowing(self, now: float) -> bool:
        """Has a frame arrived recently enough to say the camera is still talking?"""
        if self.frames_fn is None:
            return False
        try:
            frames = self.frames_fn()
        except Exception:  # pragma: no cover - defensive
            return False
        if frames != self._frames:
            self._frames = frames
            self._frames_moved_at = now
            self.video_was_flowing = True
            self._stalled = False
        if self._frames_moved_at is None:
            return False
        return now - self._frames_moved_at <= self.gap

    def summary(self) -> str:
        """One line, and it must not claim a fault the evidence does not support."""
        outages = self.outages
        longest = self.longest_outage
        if self._missing_since is not None:
            # An outage still running when the session ended still counts.
            longest = max(longest, time.monotonic() - self._missing_since)
        if self.count == 0:
            return "no beacon was ever seen"
        parts = [f"{self.count} beacon(s)"]
        if outages:
            parts.append(f"{outages} time(s) off the air, longest {longest:.1f}s")
        else:
            parts.append("never off the air")
        if self.suppressions:
            parts.append(
                f"{self.suppressions} quiet spell(s) while video was flowing, which is normal"
            )
        if self.encoder_stalls:
            parts.append(f"{self.encoder_stalls} time(s) on the air with no picture")
        return ", ".join(parts)

    def _run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", self.port))
        except OSError as exc:
            log.error("cannot watch the beacon on UDP %d: %s", self.port, exc)
            self._ready.set()
            return
        sock.settimeout(0.5)
        self._ready.set()
        log.info(
            "watching the camera's beacon. It goes quiet while the camera is serving us, "
            "so only silence with no video arriving means it has gone off the air"
        )
        try:
            while not self._stop.is_set():
                try:
                    data, addr = sock.recvfrom(1024)
                    beacon = parse_beacon(data, addr[0])
                except (TimeoutError, OSError):
                    beacon = None
                now = time.monotonic()
                streaming = self._video_is_flowing(now)

                if beacon is not None:
                    if self.first_beacon is None:
                        self.first_beacon = beacon
                        log.info("beacon: %s", beacon.describe())
                    self.count += 1
                    if self._missing_since is not None:
                        outage = now - self._missing_since
                        self.longest_outage = max(self.longest_outage, outage)
                        self.recoveries += 1
                        log.warning(
                            "the camera is announcing itself again after %.1fs off the air. "
                            "A gap of a few seconds like this is the camera rebooting, "
                            "which on batteries points at the cells, their contacts or the switch.",
                            outage,
                        )
                        self._missing_since = None
                    elif self._suppressed:
                        log.info("the camera is announcing itself again")
                    self._suppressed = False
                    self.last_seen = now

                on_the_air = self.last_seen is not None and now - self.last_seen <= self.gap

                # Checked on every pass, including passes where a beacon arrived:
                # a camera that is answering while sending no picture is the whole
                # point of watching, and an earlier version could never see it
                # because a received beacon skipped the rest of the loop.
                if on_the_air and self.video_was_flowing and not streaming and not self._stalled:
                    self._stalled = True
                    self.encoder_stalls += 1
                    log.error(
                        "the picture has stopped but the camera is still announcing itself. "
                        "It is alive and has stopped encoding, so this is not a power fault."
                    )

                if self.last_seen is None or on_the_air:
                    continue

                if streaming:
                    # The camera suppresses its announcement while it has a client.
                    # Video is still arriving, so nothing is wrong.
                    if not self._suppressed:
                        self._suppressed = True
                        self.suppressions += 1
                        log.info(
                            "the camera has stopped announcing itself, which it does while it "
                            "is sending to us. Video is still arriving, so this is expected"
                        )
                    continue

                if self._missing_since is None:
                    self._missing_since = self.last_seen
                    self.outages += 1
                    self._suppressed = False
                    log.error(
                        "the camera has gone off the air (%.1fs with neither an announcement "
                        "nor a frame). It is not merely refusing to send video: it has stopped "
                        "transmitting altogether, so it has either lost power or hung.",
                        now - self.last_seen,
                    )
        finally:
            sock.close()


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


def local_ipv4_addresses() -> list[str]:
    """Every IPv4 address this machine answers on, best effort.

    Used to tell the user which URL to type into a phone. The camera's own
    network is in here too, which is why the caller labels it.
    """
    found: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if addr not in found and not addr.startswith("127."):
                found.append(addr)
    except OSError:
        pass
    # The hostname lookup misses some adapters, so add the default route's own
    # address as well.
    primary = local_ip_for("8.8.8.8")
    if primary and primary not in found and not primary.startswith("127."):
        found.append(primary)
    return found


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
