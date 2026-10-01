"""A fake camera, so the tool can be developed and tested without hardware.

It speaks the same protocol as the real thing: the discovery beacon, the reliable
UDP transport, the text commands, and ``0011`` chunks carrying ``Ret``, ``Type``,
``Info`` and ``Data`` items in the order real hardware uses. The video it replays
is a small H.264 file, by default the one in ``tests/data``, so nothing external
is needed.

Run it with ``python -m eteq.simulator`` and point the tool at it with
``eteq --ip 127.0.0.1 --no-discover``.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import struct
import sys
import threading
import time

from . import protocol as P
from .mp4 import H264Framer

log = logging.getLogger("eteq.simulator")

CHUNK = 929
"""Payload size real hardware uses for all but the last chunk of a frame."""


def _default_clip() -> str | None:
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        os.path.join(here, "..", "..", "tests", "data", "sample.h264"),
        os.path.join(here, "data", "sample.h264"),
    ):
        path = os.path.normpath(candidate)
        if os.path.exists(path):
            return path
    return None


class SimulatedCamera:
    def __init__(
        self,
        bind: str = "127.0.0.1",
        port: int = P.CAM_PORT_DEFAULT,
        beacon_to: tuple[str, int] = ("127.0.0.1", P.BEACON_PORT),
        clip: str | None = None,
        fps: float = 20.0,
        loss: int = 0,
        name: str = "WIFICAM",
        refuse_settings: bool = False,
        ignore_start: bool = False,
    ) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind((bind, port))
        self.sock.settimeout(0.01)
        self.bind_ip = bind
        self.beacon_to = beacon_to
        self.fps = fps
        self.loss = loss
        self.name = name

        self.client: tuple[str, int] | None = None
        self.lock = threading.Lock()
        self.send_seq = 0
        self.send_base = 0
        self.unacked: dict[int, list] = {}
        self.recv_expected = 0
        self.streaming = False
        self.params: dict[str, str] = {}
        self.sent_count = 0
        self.running = True
        # Two quirks of the reference hardware, off by default so the simulator
        # stays easy to work against, but available for tests.
        self.refuse_settings = refuse_settings
        """Answer ``Ret=0`` to a SET that arrives while already streaming."""
        self.ignore_start = ignore_start
        """Acknowledge ``Video=1`` but never actually encode anything."""

        path = clip or _default_clip()
        if not path:
            raise SystemExit(
                "No H.264 clip to replay. Pass --clip with a raw .h264 file "
                "(tests/data/sample.h264 is the usual one)."
            )
        with open(path, "rb") as fh:
            data = fh.read()
        # One framer: an access unit is only complete when the next start code
        # arrives, so the final frame only appears when the same instance flushes.
        framer = H264Framer()
        self.frames = framer.push(data) + framer.flush()
        if not self.frames:
            raise SystemExit(f"{path} did not contain any H.264 frames")
        log.info("replaying %d frames from %s", len(self.frames), path)

    # -- transport -----------------------------------------------------------

    def _raw_send(self, pkt: bytes) -> None:
        if self.client is None:
            return
        self.sent_count += 1
        if self.loss and self.sent_count % self.loss == 0:
            log.debug("dropping %s on purpose", pkt[:4].hex())
            return
        try:
            self.sock.sendto(pkt, self.client)
        except OSError:
            pass

    def send_data(self, payload: bytes) -> None:
        with self.lock:
            seq = self.send_seq
            pkt = bytes([P.PKT_DATA, seq, self.recv_expected, P.PKT_MAGIC]) + payload
            self.unacked[seq] = [pkt, time.monotonic(), 0]
            self.send_seq = (seq + 1) & 0xFF
            self._raw_send(pkt)

    def retransmit_tick(self) -> None:
        with self.lock:
            if not self.unacked:
                return
            entry = self.unacked.get(self.send_base)
            if entry is None:
                self.send_base = min(self.unacked, key=lambda s: (s - self.send_base) & 0xFF)
                entry = self.unacked[self.send_base]
            if time.monotonic() - entry[1] <= 0.05:
                return
            entry[1] = time.monotonic()
            entry[2] += 1
            if entry[2] > 100:
                log.warning("the client stopped acknowledging; stopping the stream")
                self.streaming = False
                self.unacked.clear()
                return
            # Real hardware zeroes the message prefix when it retransmits.
            pkt = bytearray(entry[0])
            if len(pkt) > 8 and bytes(pkt[4:8]) == P.MSG_PREFIX:
                pkt[4:8] = P.ZERO_PREFIX
            self._raw_send(bytes(pkt))

    def handle(self, pkt: bytes, addr: tuple[str, int]) -> None:
        if len(pkt) < 4 or pkt[3] != P.PKT_MAGIC:
            return
        if self.client != addr:
            log.info("client %s:%d connected", *addr)
            with self.lock:
                self.client = addr
                self.send_seq = self.send_base = 0
                self.unacked.clear()
                self.recv_expected = 0
                self.streaming = False

        ptype, seq, ack = pkt[0], pkt[1], pkt[2]
        with self.lock:
            if ptype in (P.PKT_DATA, P.PKT_ACK):
                steps = 0
                while self.unacked and self.send_base != ack and steps < 256:
                    self.unacked.pop(self.send_base, None)
                    self.send_base = (self.send_base + 1) & 0xFF
                    steps += 1
            if ptype == P.PKT_NACK:
                entry = self.unacked.get(seq)
                if entry:
                    self._raw_send(entry[0])
                return

        if ptype != P.PKT_DATA:
            return
        if seq != self.recv_expected:
            self._raw_send(bytes([P.PKT_ACK, seq, self.recv_expected, P.PKT_MAGIC]))
            return
        self.recv_expected = (self.recv_expected + 1) & 0xFF
        self._raw_send(bytes([P.PKT_ACK, seq, self.recv_expected, P.PKT_MAGIC]))
        self.on_message(pkt[4:])

    # -- messages ------------------------------------------------------------

    def on_message(self, payload: bytes) -> None:
        msg = P.parse_message(payload)
        if msg is None:
            log.warning("unparseable message %r", payload[:24])
            return

        if msg.code == P.CODE_GET:
            log.info("GET %s", [(k.decode(), v.decode(errors='replace')) for k, v in msg.items])
            self.send_data(P.message(P.CODE_GET_ACK, P.item("AllInfo", b"\0" * 0x2BC)))

        elif msg.code == P.CODE_SET:
            pairs = [(k.decode(), v.decode(errors="replace")) for k, v in msg.items]
            starting = any(k == "Video" for k, _ in pairs)
            if self.refuse_settings and self.streaming and not starting:
                log.info("SET %s -> refused (Ret=0)", pairs)
                self.send_data(P.message(P.CODE_SET_ACK, P.item("Ret", "0")))
                return
            log.info("SET %s", pairs)
            self.params.update(pairs)
            self.send_data(P.message(P.CODE_SET_ACK, P.item("Ret", "1")))
            if self.params.get("Video") == "1" and not self.streaming:
                if self.ignore_start:
                    log.info("pretending the encoder is dead: acknowledged but not streaming")
                else:
                    log.info("starting the stream")
                    self.streaming = True
            elif self.params.get("Video") == "0" and self.streaming:
                log.info("stopping the stream")
                self.streaming = False

        elif msg.code == P.CODE_USR:
            reply = b"05000001Video0"
            if msg.body == P.HEARTBEAT_UDC and int(time.monotonic()) % 11 == 0:
                reply = P.SNAPSHOT_PRESSED  # pretend the button was pressed now and then
            self.send_data(P.message(P.CODE_USR_ACK, reply))

        else:
            log.info("unhandled code %s", msg.code)

    # -- threads -------------------------------------------------------------

    def beacon_loop(self) -> None:
        ip_bytes = bytes(int(x) for x in self.bind_ip.split("."))
        beacon = P.BEACON_MAGIC + ip_bytes + self.name.encode()[:16].ljust(16, b"\0") + b"\0" * 8
        while self.running:
            try:
                self.sock.sendto(beacon, self.beacon_to)
            except OSError:
                pass
            time.sleep(1.0)

    def stream_loop(self) -> None:
        index = 0
        timestamp = 0
        period = 1.0 / self.fps
        while self.running:
            if not self.streaming:
                time.sleep(0.05)
                continue
            frame = self.frames[index % len(self.frames)]
            index += 1
            timestamp += int(round(period * 1000))
            payload = frame.data
            chunks = [payload[i : i + CHUNK] for i in range(0, len(payload), CHUNK)] or [b""]
            for position, piece in enumerate(chunks):
                info = struct.pack(
                    ">7I",
                    P.STREAM_TYPE_KEYFRAME if frame.is_keyframe else P.STREAM_TYPE_INTER,
                    len(payload) if position == 0 else 0,
                    index & 0xFFFF,
                    position,
                    0,
                    timestamp,
                    0,
                )
                body = (
                    P.item("Ret", "1")
                    + P.item("Type", "Video")
                    + P.item("Info", info)
                    + P.item("Data", piece)
                )
                self.send_data(P.message(P.CODE_STREAM, body))
                waited = 0.0
                while len(self.unacked) > 24 and self.streaming and waited < 2.0:
                    time.sleep(0.002)
                    waited += 0.002
            time.sleep(period)

    def run(self) -> None:
        threading.Thread(target=self.beacon_loop, name="beacon", daemon=True).start()
        threading.Thread(target=self.stream_loop, name="stream", daemon=True).start()
        log.info("simulated camera listening on %s:%d", *self.sock.getsockname())
        try:
            while self.running:
                try:
                    pkt, addr = self.sock.recvfrom(4096)
                    self.handle(pkt, addr)
                except TimeoutError:
                    pass
                except (ConnectionResetError, OSError):
                    pass
                self.retransmit_tick()
        except KeyboardInterrupt:
            log.info("stopping")
        finally:
            self.running = False
            self.sock.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=P.CAM_PORT_DEFAULT)
    parser.add_argument("--beacon-to", default=f"127.0.0.1:{P.BEACON_PORT}")
    parser.add_argument("--clip", help="raw .h264 file to replay (default: tests/data/sample.h264)")
    parser.add_argument("--fps", type=float, default=20.0)
    parser.add_argument("--loss", type=int, default=0, help="drop every Nth outgoing packet")
    parser.add_argument(
        "--refuse-settings",
        action="store_true",
        help="answer Ret=0 to settings changed mid-stream, like the reference camera",
    )
    parser.add_argument(
        "--ignore-start",
        action="store_true",
        help="acknowledge the start command but never send video, like a dead encoder",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )
    host, _, port = args.beacon_to.rpartition(":")
    camera = SimulatedCamera(
        bind=args.bind,
        port=args.port,
        beacon_to=(host or "127.0.0.1", int(port)),
        clip=args.clip,
        fps=args.fps,
        loss=args.loss,
        refuse_settings=args.refuse_settings,
        ignore_start=args.ignore_start,
    )
    camera.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
