"""Wire-format tests.

Several of these assert against bytes captured from a real camera, so they fail
loudly if the encoding ever drifts.
"""

from __future__ import annotations

import struct

import pytest

from eteq import protocol as P

# Captured from a Gardner Bender eTEQ WIC-100 on 2026-10-01. These are the exact
# payloads that made it stream, minus the 4-byte transport header.
REAL_GET_ALLINFO = b"0010000800001007000001AllInfo1"
REAL_START = (
    b"001000060000b1"
    b"05000001Audio1"
    b"05000001Video1"
    b"09000007FrameSize28000f0"
    b"09000002FrameRate14"
    b"07000003BitRate800"
    b"04000001Zoom0"
    b"0a000002Brightness80"
    b"08000001Contrast4"
    b"0a000001Saturation4"
    b"0a000001FlipMirror3"
)
REAL_SET_ACK = b"001000070000" b"0c" b"03000001Ret1"
REAL_HEARTBEAT = b"0010001500001" b"4" b"0C000000GetSnapPhoto"


class TestItems:
    def test_item_layout(self):
        assert P.item("Video", 1) == b"05000001Video1"
        assert P.item("AllInfo", 1) == b"07000001AllInfo1"

    def test_integers_are_lowercase_hex_without_padding(self):
        # FrameSize for 640x240 is 0x28000f0, and the camera expects exactly this.
        assert P.item("FrameSize", P.frame_size_value(640, 240)) == b"09000007FrameSize28000f0"
        assert P.item("BitRate", 2048) == b"07000003BitRate800"

    def test_binary_values_survive(self):
        payload = bytes(range(256))
        encoded = P.item("Data", payload)
        key, value = P.parse_items(encoded)[0]
        assert key == b"Data"
        assert value == payload

    def test_round_trip(self):
        pairs = [("Video", "1"), ("FrameRate", "14"), ("Note", "")]
        body = P.items(pairs)
        assert [(k.decode(), v.decode()) for k, v in P.parse_items(body)] == pairs

    def test_truncated_item_is_dropped_not_guessed(self):
        body = P.item("Video", 1) + b"09000007Frame"  # claims 7 bytes, supplies 5
        assert P.parse_items(body) == [(b"Video", b"1")]

    def test_garbage_lengths_stop_parsing(self):
        assert P.parse_items(b"zz000001Video1") == []

    def test_item_limit(self):
        body = P.items([("K", str(i)) for i in range(40)])
        assert len(P.parse_items(body)) == P.MAX_ITEMS


class TestMessages:
    def test_get_allinfo_matches_capture(self):
        assert P.build_get_allinfo() == REAL_GET_ALLINFO

    def test_start_command_matches_capture(self):
        pairs = [
            ("Audio", 1),
            ("Video", 1),
            ("FrameSize", P.frame_size_value(640, 240)),
            ("FrameRate", 20),
            ("BitRate", 2048),
            ("Zoom", 0),
            ("Brightness", 128),
            ("Contrast", 4),
            ("Saturation", 4),
            ("FlipMirror", 3),
        ]
        assert P.build_set(pairs) == REAL_START

    def test_heartbeat_matches_capture(self):
        assert P.build_user_command(P.HEARTBEAT_UDC) == REAL_HEARTBEAT

    def test_stop_is_video_zero(self):
        assert P.build_stop() == b"001000060000" b"0e" b"05000001Video0"

    def test_parse_set_ack(self):
        msg = P.parse_message(REAL_SET_ACK)
        assert msg is not None
        assert msg.code == P.CODE_SET_ACK
        assert msg.get("Ret") == b"1"

    def test_zero_prefix_is_accepted(self):
        """The camera zeroes the prefix when it retransmits."""
        normal = P.parse_message(REAL_SET_ACK)
        retransmitted = P.parse_message(P.ZERO_PREFIX + REAL_SET_ACK[4:])
        assert retransmitted is not None
        assert retransmitted.code == normal.code
        assert retransmitted.items == normal.items

    def test_user_ack_body_is_not_parsed_as_items(self):
        msg = P.parse_message(P.message(P.CODE_USR_ACK, b"05000001Video1"))
        assert msg.items == []
        assert msg.body == b"05000001Video1"

    @pytest.mark.parametrize(
        "payload",
        [b"", b"short", b"9999000800001007000001AllInfo1", b"0010000800zzzz07000001AllInfo1"],
    )
    def test_rubbish_is_rejected(self, payload):
        assert P.parse_message(payload) is None

    def test_body_longer_than_declared_is_truncated(self):
        msg = P.parse_message(b"0010" + b"0007" + b"000004" + b"ABCDEFGH")
        assert msg.body == b"ABCD"

    def test_code_must_be_four_characters(self):
        with pytest.raises(ValueError):
            P.message(b"00", b"")


class TestStreamChunks:
    @staticmethod
    def build_chunk(frame_type=0, total=7072, counter=0, index=0, ts=132, data=b"\x00\x00\x00\x01abc"):
        info = struct.pack(">7I", frame_type, total, counter, index, 0, ts, 0)
        body = P.item("Ret", "1") + P.item("Type", "Video") + P.item("Info", info) + P.item("Data", data)
        return P.message(P.CODE_STREAM, body)

    def test_item_order_matches_hardware(self):
        """Real chunks are Ret, Type, Info, Data in that order."""
        raw = self.build_chunk()
        assert raw.startswith(b"00100011")
        assert b"03000001Ret1" in raw
        assert b"04000005TypeVideo" in raw
        assert b"0400001cInfo" in raw

    def test_parse(self):
        msg = P.parse_message(self.build_chunk(frame_type=0, ts=132))
        chunk = P.parse_stream_chunk(msg)
        assert chunk is not None
        assert chunk.media_type == b"Video"
        assert chunk.data == b"\x00\x00\x00\x01abc"
        assert chunk.info.timestamp_ms == 132
        assert chunk.info.is_keyframe
        assert chunk.info.starts_frame
        assert not chunk.is_audio

    def test_inter_frame(self):
        msg = P.parse_message(self.build_chunk(frame_type=2, index=3))
        chunk = P.parse_stream_chunk(msg)
        assert not chunk.info.is_keyframe
        assert not chunk.info.starts_frame

    def test_audio_detected_by_type_or_by_info(self):
        info = struct.pack(">7I", P.STREAM_TYPE_AUDIO, 0, 0, 0, 0, 1, 0)
        body = P.item("Type", "Audio") + P.item("Info", info) + P.item("Data", b"pcm")
        chunk = P.parse_stream_chunk(P.parse_message(P.message(P.CODE_STREAM, body)))
        assert chunk.is_audio

    def test_short_info_is_tolerated(self):
        body = P.item("Info", b"\x00\x01") + P.item("Data", b"x")
        chunk = P.parse_stream_chunk(P.parse_message(P.message(P.CODE_STREAM, body)))
        assert chunk.info is None
        assert chunk.data == b"x"

    def test_non_stream_message_returns_none(self):
        assert P.parse_stream_chunk(P.parse_message(REAL_SET_ACK)) is None

    def test_real_info_values_decode_sensibly(self):
        """Values lifted straight from a capture: an I-frame then its tail chunk."""
        first = P.StreamInfo.parse(struct.pack(">7I", 0, 7072, 0, 0, 0, 132, 0))
        last = P.StreamInfo.parse(struct.pack(">7I", 2, 0, 1, 7, 0, 132, 0))
        assert first.is_keyframe and first.frame_bytes == 7072 and first.starts_frame
        assert not last.is_keyframe and last.chunk_index == 7
        assert first.timestamp_ms == last.timestamp_ms == 132


class TestHelpers:
    def test_frame_size_round_trip(self):
        for width, height in P.FRAME_SIZES:
            assert P.parse_frame_size(P.frame_size_value(width, height)) == (width, height)

    def test_frame_size_known_value(self):
        assert P.frame_size_value(640, 240) == 0x28000F0

    def test_between_wraps(self):
        assert P.between(250, 252, 4)
        assert P.between(250, 0, 4)
        assert not P.between(250, 4, 4)
        assert not P.between(250, 10, 4)

    def test_hexdump_shows_ascii_and_hex(self):
        out = P.hexdump(b"0010\x00\xff")
        assert "30 30 31 30 00 ff" in out
        assert "0010.." in out

    def test_hexdump_respects_limit(self):
        assert len(P.hexdump(b"x" * 1000, maxlen=16).splitlines()) == 1


class TestWifiCredentials:
    def test_encoding(self):
        raw = P.build_wifi_credentials("WIFICAMERA", "88888888")
        assert b"0400000aSSIDWIFICAMERA" in raw
        assert b"08000008PASSWORD88888888" in raw

    @pytest.mark.parametrize(
        "ssid,password",
        [("", "88888888"), ("x" * 17, "88888888"), ("ok", "short"), ("ok", "x" * 17)],
    )
    def test_rejects_values_the_app_would_refuse(self, ssid, password):
        with pytest.raises(ValueError):
            P.build_wifi_credentials(ssid, password)
