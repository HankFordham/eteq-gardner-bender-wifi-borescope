"""Wire format of the OV780 WiFi camera protocol.

Three layers sit on one UDP socket to the camera's port 1000:

1. A 4-byte transport header ``[type][seq][ack]['v']`` providing ordering,
   acknowledgement and retransmission (see :mod:`eteq.transport`).
2. A text message ``"0010" + <4-char code> + <6 hex digit length> + body``.
3. A body made of length-prefixed items ``%02x%06x%s%s`` =
   (key length, value length, key, value).

Everything in this module is pure parsing and formatting, with no I/O, so it is
cheap to unit test. See ``docs/PROTOCOL.md`` for how it was derived and which
parts are confirmed against real hardware.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# --- transport ---------------------------------------------------------------

CAM_PORT_DEFAULT = 1000
"""UDP port on the camera. From ``access_open(ctx, ip, 0x3e8)`` in the vendor SDK."""

BEACON_PORT = 2000
"""UDP port the camera broadcasts its presence to, once per second."""

BEACON_MAGIC = b"8713"
BEACON_SIZE = 32

PKT_MAGIC = 0x76
"""Fourth header byte of every transport packet, the ASCII letter 'v'."""

PKT_DATA, PKT_ACK, PKT_NACK = 0, 1, 2

WINDOW = 32
"""Receive window. The camera keeps 32 slots indexed by ``seq & 0x1f``."""

MAX_DATAGRAM = 0x404
"""1028 bytes: 4 header bytes plus at most 1024 of payload."""

# --- messages ----------------------------------------------------------------

MSG_PREFIX = b"0010"
ZERO_PREFIX = b"\0\0\0\0"
"""Retransmitted messages arrive with the prefix zeroed. Observed on real hardware."""

CODE_SET = b"0006"
CODE_SET_ACK = b"0007"
CODE_GET = b"0008"
CODE_GET_ACK = b"0009"
CODE_STREAM = b"0011"
CODE_USR = b"0015"
CODE_USR_ACK = b"0016"

MAX_ITEMS = 16
"""The vendor parser gives up past this many items in one body."""

# --- parameters --------------------------------------------------------------

PARAM_NAMES: tuple[str, ...] = (
    "Video",
    "Audio",
    "FrameSize",
    "FrameRate",
    "BitRate",
    "Zoom",
    "Brightness",
    "Contrast",
    "Saturation",
    "FlipMirror",
    "LightCond",
    "LightFreq",
    "AlertMode",
    "AudioAlertV",
    "Infrared",
)
"""Parameter table from the vendor library, in its original index order."""

HEARTBEAT_UDC = b"0C000000GetSnapPhoto"
"""Raw user command the phone app sends once per second.

The camera answers ``05000001Video0`` normally and ``05000001Video1`` when its
physical snapshot button has been pressed.
"""

SNAPSHOT_PRESSED = b"05000001Video1"

FRAME_SIZES: tuple[tuple[int, int], ...] = (
    (320, 240),
    (640, 240),
    (640, 480),
    (1280, 480),
    (1280, 720),
)
"""Sizes worth trying. The phone app only ever requests the first, second and last two."""

FRAME_RATES: tuple[int, ...] = (3, 5, 10, 15, 20, 25, 30)
BIT_RATES: tuple[int, ...] = (128, 256, 512, 768, 1024, 1536, 2048, 2560, 3072)


def item(key: str | bytes, value: str | bytes | int) -> bytes:
    """Encode one body item as ``%02x%06x%s%s``.

    Integers are formatted the way the vendor app does it, with ``%x``: lowercase
    hex and no padding.
    """
    if isinstance(key, str):
        key = key.encode()
    if isinstance(value, int):
        value = format(value, "x")
    if isinstance(value, str):
        value = value.encode()
    if len(key) > 0xFF:
        raise ValueError("key too long")
    return b"%02x%06x" % (len(key), len(value)) + key + value


def items(pairs: Iterable[tuple[str | bytes, str | bytes | int]]) -> bytes:
    """Encode several items in order."""
    return b"".join(item(k, v) for k, v in pairs)


def message(code: str | bytes, body: bytes) -> bytes:
    """Wrap a body in the ``0010`` message envelope."""
    if isinstance(code, str):
        code = code.encode()
    if len(code) != 4:
        raise ValueError("code must be 4 characters")
    return MSG_PREFIX + code + b"%06x" % len(body) + body


def parse_items(body: bytes, limit: int = MAX_ITEMS) -> list[tuple[bytes, bytes]]:
    """Split a body into (key, value) pairs, stopping at the first malformed one."""
    out: list[tuple[bytes, bytes]] = []
    pos = 0
    while pos + 8 <= len(body) and len(out) < limit:
        try:
            klen = int(body[pos : pos + 2], 16)
            vlen = int(body[pos + 2 : pos + 8], 16)
        except ValueError:
            break
        start = pos + 8
        key = body[start : start + klen]
        val = body[start + klen : start + klen + vlen]
        if len(key) != klen or len(val) != vlen:
            break
        out.append((key, val))
        pos = start + klen + vlen
    return out


@dataclass
class Message:
    """A decoded camera message."""

    code: bytes
    body: bytes
    items: list[tuple[bytes, bytes]]

    def get(self, key: str | bytes) -> bytes | None:
        if isinstance(key, str):
            key = key.encode()
        for k, v in self.items:
            if k == key:
                return v
        return None


def parse_message(payload: bytes) -> Message | None:
    """Decode one message payload, or return None if it is not one.

    Accepts both the normal ``0010`` prefix and the zeroed prefix the camera uses
    when it retransmits. The vendor's own parser rejects the zeroed form, which is
    why the phone app silently restarts its stream every so often.
    """
    if len(payload) < 14 or payload[:4] not in (MSG_PREFIX, ZERO_PREFIX):
        return None
    code = payload[4:8]
    try:
        blen = int(payload[8:14], 16)
    except ValueError:
        return None
    body = payload[14 : 14 + blen]
    parsed = [] if code == CODE_USR_ACK else parse_items(body)
    return Message(code=code, body=body, items=parsed)


# --- stream chunks -----------------------------------------------------------

STREAM_TYPE_KEYFRAME = 0
STREAM_TYPE_INTER = 2
STREAM_TYPE_AUDIO = 3


@dataclass
class StreamInfo:
    """The 28-byte ``Info`` item carried by every ``0011`` chunk.

    Field meanings were read off real hardware; see ``docs/PROTOCOL.md``.
    """

    frame_type: int
    """0 for an I-frame, 2 for a P-frame, 3 for audio."""
    frame_bytes: int
    """Total size of the frame, present only in its first chunk."""
    counter: int
    """Rolling counter, increments roughly once per frame."""
    chunk_index: int
    """Index of this chunk inside the frame. 0 is the first."""
    reserved4: int
    timestamp_ms: int
    """Millisecond presentation clock, restarts each session."""
    reserved6: int

    @classmethod
    def parse(cls, raw: bytes) -> StreamInfo | None:
        if len(raw) < 28:
            return None
        return cls(*struct.unpack(">7I", raw[:28]))

    @property
    def is_keyframe(self) -> bool:
        return self.frame_type == STREAM_TYPE_KEYFRAME

    @property
    def is_audio(self) -> bool:
        return self.frame_type == STREAM_TYPE_AUDIO

    @property
    def starts_frame(self) -> bool:
        return self.chunk_index == 0

    def as_list(self) -> list[int]:
        return [
            self.frame_type,
            self.frame_bytes,
            self.counter,
            self.chunk_index,
            self.reserved4,
            self.timestamp_ms,
            self.reserved6,
        ]


@dataclass
class StreamChunk:
    """One ``0011`` media message."""

    media_type: bytes
    info: StreamInfo | None
    data: bytes

    @property
    def is_audio(self) -> bool:
        if self.media_type == b"Audio":
            return True
        return self.info is not None and self.info.is_audio


def parse_stream_chunk(msg: Message) -> StreamChunk | None:
    """Pull the media payload out of a ``0011`` message."""
    if msg.code != CODE_STREAM:
        return None
    media_type = msg.get("Type") or b"Video"
    raw_info = msg.get("Info")
    data = msg.get("Data") or b""
    return StreamChunk(
        media_type=media_type,
        info=StreamInfo.parse(raw_info) if raw_info else None,
        data=data,
    )


# --- command builders --------------------------------------------------------


def frame_size_value(width: int, height: int) -> int:
    """``FrameSize`` packs the dimensions into one integer as ``(w << 16) | h``."""
    return (width << 16) | height


def parse_frame_size(value: int) -> tuple[int, int]:
    """Inverse of :func:`frame_size_value`."""
    return (value >> 16) & 0xFFFF, value & 0xFFFF


def build_get_allinfo() -> bytes:
    """``GET AllInfo``. The phone app sends this first.

    Real hardware answers with 700 bytes of zeros, so it carries no information,
    but it is a cheap round-trip that proves the camera is listening.
    """
    return message(CODE_GET, item("AllInfo", 1))


def build_set(pairs: Sequence[tuple[str | bytes, str | bytes | int]]) -> bytes:
    """A ``SET`` message. Sending ``Video=1`` is what starts the stream."""
    return message(CODE_SET, items(pairs))


def build_user_command(raw: bytes) -> bytes:
    """A raw user command (code 0015), used for the heartbeat and for WiFi setup."""
    return message(CODE_USR, raw)


def build_stop() -> bytes:
    """``Video=0``. The phone app has no stop command and just closes its socket."""
    return build_set([("Video", 0)])


def build_wifi_credentials(ssid: str, password: str) -> bytes:
    """Change the camera's own hotspot name and password.

    Mirrors the phone app's dialog exactly. The app refuses an empty SSID, an SSID
    over 16 characters, or a password outside 8 to 16 characters, and so do we.
    This is not wired into the command line on purpose: getting it wrong means
    factory-resetting the camera to get back in.
    """
    if not ssid or len(ssid) > 16:
        raise ValueError("SSID must be 1 to 16 characters")
    if not 8 <= len(password) <= 16:
        raise ValueError("password must be 8 to 16 characters")
    raw = (
        b"04" + b"%06x" % len(ssid) + b"SSID" + ssid.encode()
        + b"08" + b"%06x" % len(password) + b"PASSWORD" + password.encode()
    )
    return build_user_command(raw)


# --- helpers -----------------------------------------------------------------


def hexdump(data: bytes, maxlen: int = 96) -> str:
    """Compact hex plus ASCII, for logs that get compared against Wireshark."""
    data = bytes(data[:maxlen])
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i : i + 16]
        hx = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"  {i:04x}  {hx:<48}  {asc}")
    return "\n".join(lines)


def between(a: int, b: int, c: int) -> bool:
    """Circular comparison mod 256: is ``b`` in ``[a, c)``?

    Named after the helper of the same purpose in the vendor library.
    """
    return ((b - a) & 0xFF) < ((c - a) & 0xFF)
