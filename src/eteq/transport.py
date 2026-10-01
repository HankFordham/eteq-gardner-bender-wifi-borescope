"""Reliable UDP transport to the camera.

Every datagram in both directions starts with four bytes:

===== ==========================================================================
byte  meaning
===== ==========================================================================
0     type: 0 data, 1 ack, 2 nack
1     sequence number of this data packet, or the one being referred to
2     the sender's next expected incoming sequence number (a cumulative ack)
3     the literal byte ``'v'``; anything else is not ours
===== ==========================================================================

Both sides must acknowledge everything, or the other end eventually gives up.
Sequence numbers are 8 bits and wrap, and the receive window is 32 packets.

Two behaviours here were learned the hard way against real hardware and matter
more than they look:

* The camera retransmits a packet whenever one of our acks goes missing. Those
  duplicates are normal and must simply be re-acked. Treating them as a sign of
  a desynchronised session and renumbering in response produces an ack storm
  that kills the stream within a couple of minutes.
* Windows reports an ICMP port-unreachable on a UDP socket as a
  ``ConnectionResetError`` from ``recvfrom``, long after the datagram that caused
  it. It must be swallowed, not treated as a fatal socket error.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable

from .protocol import PKT_ACK, PKT_DATA, PKT_MAGIC, PKT_NACK, WINDOW, between, hexdump

log = logging.getLogger(__name__)

MessageHandler = Callable[[bytes], None]


class Transport:
    """One UDP conversation with one camera."""

    def __init__(
        self,
        cam_ip: str,
        cam_port: int,
        local_port: int = 0,
        use_connect: bool = False,
        seq_start: int = 0,
        dump_packets: int = 8,
        rto_ms: float = 20.0,
        max_rto_ms: float = 1000.0,
        max_retries: int = 60,
    ) -> None:
        self.peer: tuple[str, int] = (cam_ip, cam_port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 0x20000)
        self.sock.bind(("", local_port))
        if use_connect:
            self.sock.connect(self.peer)
        self.use_connect = use_connect

        self.lock = threading.Lock()

        # sender
        self.send_seq = seq_start & 0xFF
        self.send_base = seq_start & 0xFF
        self.unacked: dict[int, list] = {}
        self.base_rto = rto_ms / 1000.0
        self.rto = self.base_rto
        self.max_rto = max_rto_ms / 1000.0
        self.max_retries = max_retries
        self.link_lost = False

        # receiver
        self.recv_expected = 0
        self.recv_buf: dict[int, bytes] = {}
        self.synced = False
        self.bad_ack_count = 0
        self.bad_seq_count = 0

        # stats
        self.dump_left_rx = dump_packets
        self.dump_left_tx = dump_packets
        self.rx_packets = 0
        self.rx_bytes = 0
        self.tx_packets = 0
        self.rx_duplicates = 0
        self.rx_unknown = 0
        self.sock_errors = 0
        self.last_rx_time: float | None = None

        self.on_message: MessageHandler | None = None

    # -- plumbing ------------------------------------------------------------

    @property
    def local_port(self) -> int:
        return self.sock.getsockname()[1]

    def _raw_send(self, pkt: bytes) -> None:
        if self.use_connect:
            self.sock.send(pkt)
        else:
            self.sock.sendto(pkt, self.peer)
        self.tx_packets += 1
        if self.dump_left_tx > 0:
            self.dump_left_tx -= 1
            log.info("TX %d bytes -> %s:%d\n%s", len(pkt), self.peer[0], self.peer[1], hexdump(pkt))

    def send_data(self, payload: bytes) -> int:
        """Send a payload reliably. Returns its sequence number."""
        with self.lock:
            seq = self.send_seq
            pkt = bytes([PKT_DATA, seq, self.recv_expected, PKT_MAGIC]) + payload
            self.unacked[seq] = [pkt, time.monotonic(), 0]
            self.send_seq = (seq + 1) & 0xFF
            self._raw_send(pkt)
            return seq

    def send_ack(self, seq: int) -> None:
        self._raw_send(bytes([PKT_ACK, seq & 0xFF, self.recv_expected, PKT_MAGIC]))

    def send_nack(self, seq: int) -> None:
        self._raw_send(bytes([PKT_NACK, seq & 0xFF, self.recv_expected, PKT_MAGIC]))

    # -- sender side ---------------------------------------------------------

    def _process_ack_field(self, ack: int) -> None:
        """Retire everything the camera says it has received."""
        if not self.unacked:
            return
        if not between(self.send_base, ack, (self.send_seq + 1) & 0xFF):
            self.bad_ack_count += 1
            if self.bad_ack_count >= 3:
                self._resync_sender(ack)
            return
        self.bad_ack_count = 0
        steps = 0
        while self.send_base != ack and steps < 256:
            self.unacked.pop(self.send_base, None)
            self.send_base = (self.send_base + 1) & 0xFF
            steps += 1
        if not self.unacked:
            self.rto = self.base_rto

    def _resync_sender(self, ack: int) -> None:
        """Adopt the camera's numbering when it is clearly continuing an old session."""
        log.warning(
            "camera keeps acking %d while our window is %d..%d; adopting its numbering",
            ack,
            self.send_base,
            self.send_seq,
        )
        pending = [
            self.unacked[s][0][4:]
            for s in sorted(self.unacked, key=lambda s: (s - self.send_base) & 0xFF)
        ]
        self.unacked.clear()
        self.send_base = self.send_seq = ack
        self.bad_ack_count = 0
        for payload in pending:
            seq = self.send_seq
            pkt = bytes([PKT_DATA, seq, self.recv_expected, PKT_MAGIC]) + payload
            self.unacked[seq] = [pkt, time.monotonic(), 0]
            self.send_seq = (seq + 1) & 0xFF
            self._raw_send(pkt)

    def retransmit_tick(self) -> None:
        """Resend the oldest unacknowledged packet once its timer expires."""
        with self.lock:
            if not self.unacked:
                return
            entry = self.unacked.get(self.send_base)
            if entry is None:
                self.send_base = min(self.unacked, key=lambda s: (s - self.send_base) & 0xFF)
                entry = self.unacked[self.send_base]
            now = time.monotonic()
            if now - entry[1] < self.rto:
                return
            if entry[2] >= self.max_retries:
                self.link_lost = True
                return
            pkt = bytearray(entry[0])
            pkt[2] = self.recv_expected  # refresh the piggybacked ack
            entry[0] = bytes(pkt)
            entry[1] = now
            entry[2] += 1
            self.rto = min(self.rto * 2, self.max_rto)
            log.debug("retransmit seq=%d try=%d rto=%.0fms", self.send_base, entry[2], self.rto * 1000)
            self._raw_send(entry[0])

    # -- receiver side -------------------------------------------------------

    def handle_datagram(self, pkt: bytes, addr: tuple[str, int]) -> None:
        self.rx_packets += 1
        self.rx_bytes += len(pkt)
        self.last_rx_time = time.monotonic()
        if self.dump_left_rx > 0:
            self.dump_left_rx -= 1
            log.info("RX %d bytes <- %s:%d\n%s", len(pkt), addr[0], addr[1], hexdump(pkt))

        if len(pkt) < 4 or pkt[3] != PKT_MAGIC:
            self.rx_unknown += 1
            if self.rx_unknown <= 5:
                log.warning("datagram without the 'v' marker (%d bytes), ignored:\n%s", len(pkt), hexdump(pkt))
            return

        ptype, seq, ack = pkt[0], pkt[1], pkt[2]
        with self.lock:
            if ptype in (PKT_DATA, PKT_ACK):
                self._process_ack_field(ack)

            if ptype == PKT_NACK:
                entry = self.unacked.get(seq)
                if entry:
                    entry[1] = time.monotonic()
                    entry[2] += 1
                    self._raw_send(entry[0])
                return

            if ptype != PKT_DATA:
                return

            payload = pkt[4:]
            if seq == self.recv_expected:
                self._deliver(payload)
                self.recv_expected = (self.recv_expected + 1) & 0xFF
                while self.recv_expected in self.recv_buf:
                    self._deliver(self.recv_buf.pop(self.recv_expected))
                    self.recv_expected = (self.recv_expected + 1) & 0xFF
                self.send_ack(seq)
                self.bad_seq_count = 0
                self.synced = True
            elif ((seq - self.recv_expected) & 0xFF) < WINDOW:
                # Ahead of us: hold it, ack it, and ask for the gap.
                self.recv_buf[seq] = payload
                self.send_ack(seq)
                missing = self.recv_expected
                while missing != seq:
                    if missing not in self.recv_buf:
                        self.send_nack(missing)
                    missing = (missing + 1) & 0xFF
            else:
                self._handle_old_packet(seq, payload)

    def _handle_old_packet(self, seq: int, payload: bytes) -> None:
        """A duplicate, or a camera that never stopped its previous session.

        Before the first in-order packet of a session either is possible, so after
        a few we adopt the camera's numbering. Afterwards it is always just a
        duplicate, and the only correct response is another ack.
        """
        self.rx_duplicates += 1
        if not self.synced:
            self.bad_seq_count += 1
            if self.bad_seq_count >= 3:
                log.warning("camera sends seq %d but we expect %d; adopting its numbering", seq, self.recv_expected)
                self.bad_seq_count = 0
                self.recv_buf.clear()
                self.recv_expected = seq
                self._deliver(payload)
                self.recv_expected = (self.recv_expected + 1) & 0xFF
                self.synced = True
        self.send_ack(seq)

    def _deliver(self, payload: bytes) -> None:
        if self.on_message is None:
            return
        try:
            self.on_message(payload)
        except Exception:  # a broken handler must not kill the link
            log.exception("message handler failed")

    # -- lifecycle -----------------------------------------------------------

    def receive_once(self, timeout: float) -> bool:
        """Wait up to ``timeout`` for one datagram. Returns True if one arrived."""
        self.sock.settimeout(max(timeout, 0.0))
        try:
            if self.use_connect:
                data, addr = self.sock.recv(4096), self.peer
            else:
                data, addr = self.sock.recvfrom(4096)
        except TimeoutError:
            return False
        except (ConnectionResetError, OSError) as exc:
            # Windows surfaces ICMP unreachable here, often for a datagram we sent
            # seconds ago. Not fatal.
            self.sock_errors += 1
            if self.sock_errors <= 2:
                log.warning("socket error (camera port closed or ICMP unreachable?): %s", exc)
            else:
                log.debug("socket error #%d: %s", self.sock_errors, exc)
            time.sleep(0.05)
            return False

        if not self.use_connect:
            if addr[0] != self.peer[0]:
                log.warning("datagram from unexpected host %s:%d ignored", addr[0], addr[1])
                return False
            if addr[1] != self.peer[1]:
                log.warning("camera replied from port %d (expected %d); adopting it", addr[1], self.peer[1])
                self.peer = (addr[0], addr[1])
        self.handle_datagram(data, addr)
        return True

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
