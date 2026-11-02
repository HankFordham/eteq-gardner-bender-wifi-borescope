"""The beacon watcher has to tell three similar silences apart.

The trap this guards against: the camera stops announcing itself while it is
streaming to a client, so beacon silence on its own is normal and an earlier
version of this watcher reported a healthy camera as having gone off the air.
"""

from __future__ import annotations

import socket
import struct
import time

import pytest

from eteq.discovery import BeaconWatcher, parse_beacon
from eteq.protocol import BEACON_MAGIC

PORT = 23457
GAP = 0.3


def beacon_bytes(ip: str = "192.168.2.103", name: str = "WIFICAM") -> bytes:
    return (
        BEACON_MAGIC
        + bytes(int(part) for part in ip.split("."))
        + name.encode("ascii").ljust(16, b"\0")
        + struct.pack(">H", 0)
        + b"\0\0"
        + struct.pack(">HH", 0, 0)
    )


def send_beacon(port: int) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(beacon_bytes(), ("127.0.0.1", port))
    finally:
        sock.close()


def wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def watcher(request):
    """A watcher on its own port, stopped however the test ends."""
    frames = {"n": 0}
    port = PORT + (abs(hash(request.node.name)) % 200)
    w = BeaconWatcher(port=port, gap=GAP, frames_fn=lambda: frames["n"])
    w.start()
    yield w, frames, port
    w.stop()


def test_beacon_is_decoded(watcher):
    w, _frames, port = watcher
    assert wait_until(lambda: w.count == 0 or True)
    send_beacon(port)
    assert wait_until(lambda: w.first_beacon is not None)
    assert w.first_beacon.ip == "192.168.2.103"
    assert w.first_beacon.name == "WIFICAM"


def test_silence_while_streaming_is_not_an_outage(watcher):
    """The camera stops announcing itself while it is sending us video."""
    w, frames, port = watcher
    send_beacon(port)
    assert wait_until(lambda: w.count >= 1)

    # Frames keep arriving; the beacon does not.
    deadline = time.monotonic() + GAP * 6
    while time.monotonic() < deadline:
        frames["n"] += 1
        time.sleep(0.05)

    assert w.outages == 0, "a streaming camera must never be called off the air"
    assert w.suppressions >= 1
    assert "never off the air" in w.summary()


def test_silence_with_no_video_is_an_outage(watcher):
    """Nothing at all from the camera is a real fault."""
    w, _frames, port = watcher
    send_beacon(port)
    assert wait_until(lambda: w.count >= 1)
    assert wait_until(lambda: w.outages == 1, timeout=GAP * 10)
    assert w.recoveries == 0
    # An outage still running when the run ends is still reported.
    assert "off the air" in w.summary()
    assert "longest 0.0s" not in w.summary()


def test_a_returning_beacon_is_a_reboot(watcher):
    """Off the air and then back is the camera restarting."""
    w, _frames, port = watcher
    send_beacon(port)
    assert wait_until(lambda: w.outages == 1, timeout=GAP * 10)
    send_beacon(port)
    assert wait_until(lambda: w.recoveries == 1)
    assert w.longest_outage > 0


def test_video_flowing_is_remembered(watcher):
    w, frames, port = watcher
    send_beacon(port)
    assert wait_until(lambda: w.count >= 1)
    assert not w.video_was_flowing
    frames["n"] = 5
    assert wait_until(lambda: w.video_was_flowing)


def test_a_short_datagram_is_not_a_beacon():
    assert parse_beacon(b"8713") is None
    assert parse_beacon(b"x" * 32) is None
    assert parse_beacon(beacon_bytes()) is not None


def test_picture_stopping_while_on_air_is_an_encoder_jam(watcher):
    """The camera answering while sending nothing is a jam, not a power fault."""
    w, frames, port = watcher
    send_beacon(port)
    assert wait_until(lambda: w.count >= 1)
    frames["n"] = 10
    assert wait_until(lambda: w.video_was_flowing)

    # Keep the camera on the air while the picture stops dead.
    deadline = time.monotonic() + GAP * 6
    while time.monotonic() < deadline:
        send_beacon(port)
        time.sleep(0.05)

    assert w.encoder_stalls >= 1
    assert w.outages == 0, "a camera that is still answering has not lost power"
    assert "no picture" in w.summary()
