"""Transport tests.

These exist because of two real failures against hardware. The camera retransmits
whenever one of our acknowledgements goes missing, and an early version treated
those duplicates as proof that the session had desynchronised. It renumbered
itself in response, which produced an acknowledgement storm that killed the
stream after about 75 seconds. The tests below pin down the correct behaviour in
both directions.
"""

from __future__ import annotations

import socket

import pytest

from eteq import protocol as P
from eteq.transport import Transport


def data_packet(seq: int, payload: bytes = b"hello", ack: int = 0) -> bytes:
    return bytes([P.PKT_DATA, seq, ack, P.PKT_MAGIC]) + payload


def ack_packet(seq: int, ack: int) -> bytes:
    return bytes([P.PKT_ACK, seq, ack, P.PKT_MAGIC])


@pytest.fixture
def t():
    """A transport with its socket replaced by a capture list."""
    transport = Transport("127.0.0.1", 1000, local_port=0, dump_packets=0)
    transport.sent = []
    transport._raw_send = lambda pkt: transport.sent.append(pkt)  # type: ignore[assignment]
    transport.received = []
    transport.on_message = transport.received.append
    yield transport
    transport.close()


ADDR = ("127.0.0.1", 1000)


class TestReceiving:
    def test_in_order_delivery_and_ack(self, t):
        t.handle_datagram(data_packet(0, b"first"), ADDR)
        assert t.received == [b"first"]
        assert t.sent == [ack_packet(0, 1)]
        assert t.recv_expected == 1

    def test_out_of_order_is_buffered_then_released(self, t):
        t.handle_datagram(data_packet(0, b"a"), ADDR)
        t.sent.clear()
        t.handle_datagram(data_packet(2, b"c"), ADDR)
        assert t.received == [b"a"]  # c is held back
        assert ack_packet(2, 1) in t.sent
        assert bytes([P.PKT_NACK, 1, 1, P.PKT_MAGIC]) in t.sent, "the gap must be requested"

        t.handle_datagram(data_packet(1, b"b"), ADDR)
        assert t.received == [b"a", b"b", b"c"], "the buffered packet must follow immediately"
        assert t.recv_expected == 3

    def test_duplicate_after_sync_is_acked_not_redelivered(self, t):
        t.handle_datagram(data_packet(0, b"a"), ADDR)
        t.handle_datagram(data_packet(1, b"b"), ADDR)
        t.sent.clear()

        t.handle_datagram(data_packet(0, b"a"), ADDR)  # the camera missed our ack

        assert t.received == [b"a", b"b"], "a duplicate must not be delivered twice"
        assert t.sent == [ack_packet(0, 2)], "a duplicate must be re-acknowledged"
        assert t.recv_expected == 2, "a duplicate must not move the window"
        assert t.rx_duplicates == 1

    def test_many_duplicates_never_renumber_an_established_session(self, t):
        """The regression that killed real sessions."""
        t.handle_datagram(data_packet(0), ADDR)
        for _ in range(50):
            t.handle_datagram(data_packet(200, b"old"), ADDR)
        assert t.recv_expected == 1
        assert t.received == [b"hello"]

    def test_unsynced_session_adopts_the_camera_numbering(self, t):
        """Before the first in-order packet, the camera may be continuing an old session."""
        for _ in range(3):
            t.handle_datagram(data_packet(90, b"mid-session"), ADDR)
        assert t.synced
        assert t.recv_expected == 91
        assert t.received == [b"mid-session"]

    def test_packets_without_the_marker_are_ignored(self, t):
        t.handle_datagram(b"\x00\x00\x00\x00rubbish", ADDR)
        t.handle_datagram(b"ab", ADDR)
        assert t.received == []
        assert t.sent == []
        assert t.rx_unknown == 2

    def test_counters(self, t):
        t.handle_datagram(data_packet(0, b"12345"), ADDR)
        assert t.rx_packets == 1
        assert t.rx_bytes == 9
        assert t.last_rx_time is not None


class TestSending:
    def test_send_assigns_increasing_sequence_numbers(self, t):
        assert t.send_data(b"one") == 0
        assert t.send_data(b"two") == 1
        assert t.sent[0][:4] == bytes([P.PKT_DATA, 0, 0, P.PKT_MAGIC])
        assert len(t.unacked) == 2

    def test_ack_field_retires_packets(self, t):
        t.send_data(b"one")
        t.send_data(b"two")
        t.handle_datagram(ack_packet(0, 1), ADDR)
        assert 0 not in t.unacked and 1 in t.unacked

        t.handle_datagram(ack_packet(1, 2), ADDR)
        assert t.unacked == {}

    def test_ack_piggybacked_on_data_counts(self, t):
        t.send_data(b"one")
        t.handle_datagram(data_packet(0, b"x", ack=1), ADDR)
        assert t.unacked == {}

    def test_nack_triggers_an_immediate_resend(self, t):
        t.send_data(b"one")
        t.sent.clear()
        t.handle_datagram(bytes([P.PKT_NACK, 0, 0, P.PKT_MAGIC]), ADDR)
        assert len(t.sent) == 1
        assert t.sent[0][1] == 0

    def test_retransmit_refreshes_the_ack_field(self, t):
        t.send_data(b"one")
        t.handle_datagram(data_packet(0, b"x"), ADDR)  # now we expect seq 1
        t.sent.clear()
        t.rto = 0  # fire immediately
        t.retransmit_tick()
        assert t.sent, "the packet should have been resent"
        assert t.sent[-1][2] == 1, "the resend must carry our current expectation"

    def test_link_is_declared_lost_after_the_retry_budget(self, t):
        t.max_retries = 3
        t.send_data(b"one")
        for _ in range(10):
            t.rto = 0
            t.retransmit_tick()
        assert t.link_lost

    def test_out_of_window_ack_eventually_resyncs(self, t):
        t.send_data(b"one")
        for _ in range(3):
            t.handle_datagram(ack_packet(0, 200), ADDR)
        assert t.send_base == t.send_seq - 1 or t.send_base == 200
        assert t.unacked, "the unsent payload must be requeued, not dropped"


class TestSocket:
    def test_receive_once_times_out_quietly(self):
        transport = Transport("127.0.0.1", 1, local_port=0, dump_packets=0)
        try:
            assert transport.receive_once(0.01) is False
        finally:
            transport.close()

    def test_real_datagram_round_trip(self):
        """Prove the socket path works, not just the state machine."""
        peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        peer.bind(("127.0.0.1", 0))
        transport = Transport("127.0.0.1", peer.getsockname()[1], local_port=0, dump_packets=0)
        got = []
        transport.on_message = got.append
        try:
            transport.send_data(b"ping")
            raw, sender = peer.recvfrom(2048)
            assert raw[4:] == b"ping"
            peer.sendto(data_packet(0, b"pong"), sender)
            assert transport.receive_once(2.0) is True
            assert got == [b"pong"]
        finally:
            transport.close()
            peer.close()

    def test_datagram_from_a_stranger_is_dropped(self):
        peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        peer.bind(("127.0.0.1", 0))
        # Point the transport at a different port than the one that will answer.
        transport = Transport("10.255.255.1", 1000, local_port=0, dump_packets=0)
        got = []
        transport.on_message = got.append
        try:
            peer.sendto(data_packet(0, b"evil"), ("127.0.0.1", transport.local_port))
            transport.receive_once(0.3)
            assert got == []
        finally:
            transport.close()
            peer.close()
