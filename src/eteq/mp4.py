"""Dependency-free fragmented-MP4 muxer for raw H.264 Annex B streams.

Why this module exists
----------------------
The target camera hands us a bare H.264 elementary stream: Annex B byte
stream, Baseline profile, one VCL NAL per frame, no container and no timing
information at all (per-frame millisecond timestamps arrive on a *separate*
channel).  Browsers cannot play that.  ``<video>`` wants a container, and the
only way to feed a live stream into a browser without plugins is Media Source
Extensions, which wants ISO-BMFF *fragmented* MP4: an initialisation segment
(``ftyp`` + ``moov`` with ``mvex``) followed by a stream of ``moof`` + ``mdat``
media segments.

We refuse to depend on ffmpeg for that.  Shelling out to a 100 MB binary to
wrap bytes in a handful of length-prefixed boxes is absurd, it makes the
project painful to install, and it puts a subprocess in the hot path of a live
video relay.  So this module does the wrapping itself, using nothing but the
Python standard library.

What it gives you
-----------------
``H264Framer``
    Reassembles arbitrary-sized socket chunks into complete H.264 *access
    units* (one displayable frame), tagging each with a timestamp and a
    keyframe flag.

``parse_sps``
    Decodes the Sequence Parameter Set far enough to learn the real cropped
    picture size and the ``avc1.PPCCLL`` codec string that MSE's
    ``isTypeSupported`` / ``addSourceBuffer`` require.

``FragmentedMP4Muxer``
    Emits an MSE-ready init segment and one media segment per frame.

``MP4FileWriter``
    Same thing, pointed at a file, so a recording lands on disk as a playable
    ``.mp4``.

Scope limits (deliberate)
-------------------------
Video only, single track (track ID 1), no B-frames and therefore no
composition offsets / ``ctts``.  That matches the camera: Baseline profile has
no B-frames by definition.  AVCC output uses 4-byte NAL length prefixes.

MIT licensed.
"""

from __future__ import annotations

import logging
import os
import struct
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import IO, Any

logger = logging.getLogger(__name__)

__all__ = [
    "Frame",
    "H264Framer",
    "parse_sps",
    "FragmentedMP4Muxer",
    "MP4FileWriter",
    "annexb_to_avcc",
    "iter_nal_units",
]


# --------------------------------------------------------------------------- #
# NAL unit types we care about (ITU-T H.264 Table 7-1)
# --------------------------------------------------------------------------- #

NAL_SLICE = 1          # coded slice of a non-IDR picture (P-frame here)
NAL_DPA = 2            # slice data partition A
NAL_DPB = 3            # slice data partition B
NAL_DPC = 4            # slice data partition C
NAL_IDR = 5            # coded slice of an IDR picture (keyframe)
NAL_SEI = 6            # supplemental enhancement information
NAL_SPS = 7            # sequence parameter set
NAL_PPS = 8            # picture parameter set
NAL_AUD = 9            # access unit delimiter

#: NAL types that carry coded picture data.
VCL_NAL_TYPES = frozenset({NAL_SLICE, NAL_DPA, NAL_DPB, NAL_DPC, NAL_IDR})

#: NAL types that may begin a new access unit once a VCL NAL has been seen.
#: Per the framing contract: a VCL NAL after a VCL NAL, or the first
#: SPS / SEI / AUD after a VCL NAL.  PPS is *not* in this set; in practice it
#: always trails an SPS, which has already opened the new access unit.
AU_START_NAL_TYPES = frozenset(VCL_NAL_TYPES | {NAL_SEI, NAL_SPS, NAL_AUD})

#: Parameter-set and metadata NALs that live in ``avcC``, not in ``mdat``.
NON_SAMPLE_NAL_TYPES = frozenset({NAL_SEI, NAL_SPS, NAL_PPS, NAL_AUD})

_START_CODE = b"\x00\x00\x01"
_START_CODE_4 = b"\x00\x00\x00\x01"


# --------------------------------------------------------------------------- #
# Frame
# --------------------------------------------------------------------------- #

@dataclass
class Frame:
    """One H.264 access unit plus the timing/keyframe metadata a muxer needs."""

    data: bytes            # one Annex B access unit, start codes included
    timestamp_ms: int      # presentation time in ms
    is_keyframe: bool


# --------------------------------------------------------------------------- #
# Annex B helpers
# --------------------------------------------------------------------------- #

def iter_nal_units(data: bytes) -> Iterator[bytes]:
    """Yield NAL unit payloads (no start codes) from an Annex B byte string.

    Trailing zero bytes are stripped from each payload: an RBSP always ends
    with the ``rbsp_stop_one_bit``, so a NAL's last meaningful byte is never
    0x00, which makes the zeros unambiguously ``trailing_zero_8bits`` padding
    or part of the following four-byte start code.
    """
    n = len(data)
    pos = data.find(_START_CODE)
    if pos < 0:
        return
    start = pos + 3
    while True:
        nxt = data.find(_START_CODE, start)
        end = n if nxt < 0 else nxt
        while end > start and data[end - 1] == 0:
            end -= 1
        if end > start:
            yield data[start:end]
        if nxt < 0:
            return
        start = nxt + 3


def annexb_to_avcc(data: bytes, *, drop_parameter_sets: bool = True) -> bytes:
    """Convert an Annex B access unit to AVCC (4-byte big-endian length prefixes).

    With ``drop_parameter_sets`` the SPS / PPS / AUD / SEI NALs are removed,
    because in an MP4 the parameter sets belong in the ``avcC`` configuration
    record rather than in every sample.
    """
    out = bytearray()
    for nal in iter_nal_units(data):
        if drop_parameter_sets and (nal[0] & 0x1F) in NON_SAMPLE_NAL_TYPES:
            continue
        out += struct.pack(">I", len(nal))
        out += nal
    return bytes(out)


def _strip_emulation_prevention(nal: bytes) -> bytes:
    """Remove ``emulation_prevention_three_byte`` (the 0x03 in ``00 00 03``).

    The encoder inserts those so that NAL payloads can never contain a start
    code.  They are not part of the RBSP and must go before any bit-level
    syntax parsing.
    """
    if b"\x00\x00\x03" not in nal:
        return nal
    out = bytearray()
    i = 0
    n = len(nal)
    while i < n:
        if i + 2 < n and nal[i] == 0 and nal[i + 1] == 0 and nal[i + 2] == 3:
            out += b"\x00\x00"
            i += 3
        else:
            out.append(nal[i])
            i += 1
    return bytes(out)


class _BitReader:
    """Minimal MSB-first bit reader with H.264 Exp-Golomb support."""

    __slots__ = ("_data", "_pos", "_nbits")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self._nbits = len(data) * 8

    def bit(self) -> int:
        if self._pos >= self._nbits:
            raise ValueError("H.264 bitstream truncated")
        byte = self._data[self._pos >> 3]
        value = (byte >> (7 - (self._pos & 7))) & 1
        self._pos += 1
        return value

    def bits(self, count: int) -> int:
        value = 0
        for _ in range(count):
            value = (value << 1) | self.bit()
        return value

    def ue(self) -> int:
        """Unsigned Exp-Golomb, ue(v)."""
        leading_zeros = 0
        while self.bit() == 0:
            leading_zeros += 1
            if leading_zeros > 32:
                raise ValueError("invalid Exp-Golomb code")
        if leading_zeros == 0:
            return 0
        return (1 << leading_zeros) - 1 + self.bits(leading_zeros)

    def se(self) -> int:
        """Signed Exp-Golomb, se(v)."""
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)

    def skip_scaling_list(self, size: int) -> None:
        """scaling_list() from clause 7.3.2.1.1.1."""
        last_scale = 8
        next_scale = 8
        for _ in range(size):
            if next_scale != 0:
                delta = self.se()
                next_scale = (last_scale + delta + 256) % 256
            if next_scale != 0:
                last_scale = next_scale


# --------------------------------------------------------------------------- #
# SPS parsing
# --------------------------------------------------------------------------- #

#: Profiles that carry chroma_format_idc and the scaling-list syntax.
_HIGH_PROFILES = frozenset(
    {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}
)


def parse_sps(sps_rbsp: bytes) -> dict:
    """Parse an SPS NAL and return its geometry plus the MSE codec string.

    ``sps_rbsp`` is the NAL unit *without* a start code but *with* its header
    byte (0x67 for a Baseline SPS).  A leading Annex B start code is tolerated
    and skipped.

    Returns a dict with ``width``, ``height``, ``profile_idc``,
    ``constraint_flags``, ``level_idc`` and ``codec_string`` (e.g.
    ``'avc1.42001E'``).
    """
    if sps_rbsp.startswith(_START_CODE_4):
        sps_rbsp = sps_rbsp[4:]
    elif sps_rbsp.startswith(_START_CODE):
        sps_rbsp = sps_rbsp[3:]
    if len(sps_rbsp) < 5:
        raise ValueError("SPS too short")
    if (sps_rbsp[0] & 0x1F) != NAL_SPS:
        raise ValueError(f"not an SPS NAL (type {sps_rbsp[0] & 0x1F})")

    rbsp = _strip_emulation_prevention(sps_rbsp)
    reader = _BitReader(rbsp[1:])  # drop the NAL header byte

    profile_idc = reader.bits(8)
    constraint_flags = reader.bits(8)  # constraint_set0..5 + reserved_zero_2bits
    level_idc = reader.bits(8)
    reader.ue()  # seq_parameter_set_id

    chroma_format_idc = 1  # 4:2:0 is the default for non-high profiles
    separate_colour_plane_flag = 0
    if profile_idc in _HIGH_PROFILES:
        chroma_format_idc = reader.ue()
        if chroma_format_idc == 3:
            separate_colour_plane_flag = reader.bit()
        reader.ue()  # bit_depth_luma_minus8
        reader.ue()  # bit_depth_chroma_minus8
        reader.bit()  # qpprime_y_zero_transform_bypass_flag
        if reader.bit():  # seq_scaling_matrix_present_flag
            count = 8 if chroma_format_idc != 3 else 12
            for i in range(count):
                if reader.bit():  # seq_scaling_list_present_flag[i]
                    reader.skip_scaling_list(16 if i < 6 else 64)

    reader.ue()  # log2_max_frame_num_minus4
    pic_order_cnt_type = reader.ue()
    if pic_order_cnt_type == 0:
        reader.ue()  # log2_max_pic_order_cnt_lsb_minus4
    elif pic_order_cnt_type == 1:
        reader.bit()  # delta_pic_order_always_zero_flag
        reader.se()   # offset_for_non_ref_pic
        reader.se()   # offset_for_top_to_bottom_field
        for _ in range(reader.ue()):  # num_ref_frames_in_pic_order_cnt_cycle
            reader.se()               # offset_for_ref_frame[i]

    reader.ue()   # max_num_ref_frames
    reader.bit()  # gaps_in_frame_num_value_allowed_flag
    pic_width_in_mbs_minus1 = reader.ue()
    pic_height_in_map_units_minus1 = reader.ue()
    frame_mbs_only_flag = reader.bit()
    if not frame_mbs_only_flag:
        reader.bit()  # mb_adaptive_frame_field_flag
    reader.bit()  # direct_8x8_inference_flag -- present unconditionally

    crop_left = crop_right = crop_top = crop_bottom = 0
    if reader.bit():  # frame_cropping_flag
        crop_left = reader.ue()
        crop_right = reader.ue()
        crop_top = reader.ue()
        crop_bottom = reader.ue()

    # Clause 7.4.2.1.1: crop offsets are expressed in "crop units", which
    # depend on the chroma sampling and on frame/field coding.
    chroma_array_type = 0 if separate_colour_plane_flag else chroma_format_idc
    if chroma_array_type == 0:
        crop_unit_x = 1
        crop_unit_y = 2 - frame_mbs_only_flag
    else:
        sub_width_c = 2 if chroma_array_type in (1, 2) else 1
        sub_height_c = 2 if chroma_array_type == 1 else 1
        crop_unit_x = sub_width_c
        crop_unit_y = sub_height_c * (2 - frame_mbs_only_flag)

    width = (pic_width_in_mbs_minus1 + 1) * 16 - crop_unit_x * (crop_left + crop_right)
    height = (
        (2 - frame_mbs_only_flag) * (pic_height_in_map_units_minus1 + 1) * 16
        - crop_unit_y * (crop_top + crop_bottom)
    )

    return {
        "width": width,
        "height": height,
        "profile_idc": profile_idc,
        "constraint_flags": constraint_flags,
        "level_idc": level_idc,
        "codec_string": f"avc1.{profile_idc:02X}{constraint_flags:02X}{level_idc:02X}",
    }


# --------------------------------------------------------------------------- #
# Access-unit framer
# --------------------------------------------------------------------------- #

class H264Framer:
    """Splits a continuous Annex B byte stream into access units.

    Feed it whatever the socket gave you; it buffers across chunk boundaries
    and yields complete :class:`Frame` objects.  The emitted ``data`` is
    normalised Annex B: every NAL is prefixed with a four-byte start code and
    ``trailing_zero_8bits`` padding is dropped, so the same stream split at
    different chunk sizes always produces byte-identical frames.

    An access unit is closed when, after at least one VCL NAL (types 1-5), we
    meet another VCL NAL or the first SPS / SEI / AUD.  ``is_keyframe`` is set
    when the unit contains an IDR slice (type 5).
    """

    #: Cap on un-consumed caller timestamps, so a stream that never yields an
    #: access unit cannot grow the queue without bound.  Oldest are dropped.
    MAX_PENDING_TIMESTAMPS = 256

    def __init__(self, default_fps: float = 30.0) -> None:
        if default_fps <= 0:
            raise ValueError("default_fps must be positive")
        self.default_fps = float(default_fps)
        self._buf = bytearray()
        # Index in ``_buf`` where the in-progress NAL's payload starts, or None
        # while we have not yet seen the stream's first start code.
        self._nal_start: int | None = None
        self._search = 0            # resume position for the start-code scan
        self._nals: list[bytes] = []  # NALs of the access unit being built
        self._seen_vcl = False
        self._is_keyframe = False
        self._au_ts = 0
        # Timestamps supplied by the caller, consumed in order by access units
        # as they start.  This has to be a queue, not a single slot: an Annex B
        # NAL only ends when the *next* start code arrives, so feeding one
        # timestamped frame per push means each access unit is recognised one
        # push after its own timestamp was handed over.
        self._ts_queue: deque[int] = deque(maxlen=self.MAX_PENDING_TIMESTAMPS)
        self._synth_next = 0.0      # next synthesised timestamp, in ms

    # -- public API -------------------------------------------------------- #

    def push(self, data: bytes, timestamp_ms: int | None = None) -> list[Frame]:
        """Feed arbitrary-sized chunks.  Returns complete access units.

        Supplied timestamps are queued and handed to access units in arrival
        order, so the natural usage -- one ``push`` per timestamped camera
        frame -- lines each frame up with its own timestamp even though an
        access unit can only be *recognised* one push later.  Pushes with
        ``timestamp_ms=None`` contribute nothing to the queue and any access
        unit that finds it empty gets a timestamp synthesised from
        ``default_fps``.
        """
        if timestamp_ms is not None:
            if len(self._ts_queue) == self._ts_queue.maxlen:
                logger.debug("timestamp queue full; dropping the oldest entry")
            self._ts_queue.append(int(timestamp_ms))
        if not data:
            return []

        self._buf += data
        frames: list[Frame] = []
        buf = self._buf

        while True:
            hit = buf.find(_START_CODE, self._search)
            if hit < 0:
                # A start code may straddle the next chunk; rescan the last
                # two bytes then.  This keeps the scan O(len(data)) per push.
                self._search = max(0, len(buf) - 2)
                break
            if self._nal_start is not None:
                nal = self._take_nal(buf, self._nal_start, hit)
                if nal is not None:
                    frame = self._accept_nal(nal)
                    if frame is not None:
                        frames.append(frame)
            self._nal_start = hit + 3
            self._search = hit + 3

        # Drop everything before the NAL currently under construction.
        cut = self._nal_start if self._nal_start is not None else 0
        if cut:
            del buf[:cut]
            self._nal_start = 0
            self._search = max(0, self._search - cut)
        return frames

    def flush(self) -> list[Frame]:
        """Close the stream: emit the final NAL and the pending access unit."""
        frames: list[Frame] = []
        if self._nal_start is not None:
            nal = self._take_nal(self._buf, self._nal_start, len(self._buf))
            self._nal_start = None
            if nal is not None:
                frame = self._accept_nal(nal)
                if frame is not None:
                    frames.append(frame)
        self._buf.clear()
        self._search = 0
        final = self._close_au()
        if final is not None:
            frames.append(final)
        return frames

    # -- internals --------------------------------------------------------- #

    @staticmethod
    def _take_nal(buf: bytearray, start: int, end: int) -> bytes | None:
        while end > start and buf[end - 1] == 0:
            end -= 1
        return bytes(buf[start:end]) if end > start else None

    def _accept_nal(self, nal: bytes) -> Frame | None:
        """Append one NAL, closing the previous access unit if it ends here."""
        nal_type = nal[0] & 0x1F
        finished: Frame | None = None
        if self._nals and self._seen_vcl and nal_type in AU_START_NAL_TYPES:
            finished = self._close_au()
        if not self._nals:
            self._au_ts = self._next_timestamp()
        self._nals.append(nal)
        if nal_type in VCL_NAL_TYPES:
            self._seen_vcl = True
            if nal_type == NAL_IDR:
                self._is_keyframe = True
        return finished

    def _close_au(self) -> Frame | None:
        if not self._nals:
            return None
        data = b"".join(_START_CODE_4 + nal for nal in self._nals)
        frame = Frame(data=data, timestamp_ms=self._au_ts, is_keyframe=self._is_keyframe)
        self._nals = []
        self._seen_vcl = False
        self._is_keyframe = False
        return frame

    def _next_timestamp(self) -> int:
        """Timestamp for an access unit that is just starting.

        The synthesised clock accumulates in floating point and is only
        rounded on the way out, so a non-integral frame interval (33.33 ms at
        30 fps) does not drift: 0, 33, 67, 100, ... rather than 0, 33, 66, 99.
        """
        if self._ts_queue:
            ts = self._ts_queue.popleft()
            self._synth_next = ts + 1000.0 / self.default_fps
        else:
            ts = int(round(self._synth_next))
            self._synth_next += 1000.0 / self.default_fps
        return ts


# --------------------------------------------------------------------------- #
# ISO-BMFF box construction
# --------------------------------------------------------------------------- #

def _box(box_type: bytes, *payload: bytes) -> bytes:
    """Build ``size(4) | type(4) | payload``."""
    body = b"".join(payload)
    return struct.pack(">I", 8 + len(body)) + box_type + body


def _full_box(box_type: bytes, version: int, flags: int, *payload: bytes) -> bytes:
    """Build a FullBox: like :func:`_box` but with ``version(1) | flags(3)``."""
    header = struct.pack(">B3s", version, flags.to_bytes(3, "big"))
    return _box(box_type, header, *payload)


#: 3x3 unity transformation matrix in 16.16 / 2.30 fixed point.
_UNITY_MATRIX = struct.pack(
    ">9i", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000
)


def _ftyp() -> bytes:
    return _box(
        b"ftyp",
        b"isom",                          # major_brand
        struct.pack(">I", 0x200),         # minor_version
        b"isom", b"iso2", b"avc1", b"mp41", b"iso5",
    )


def _avcc(sps: bytes, pps: bytes) -> bytes:
    """AVCDecoderConfigurationRecord (ISO/IEC 14496-15 clause 5.3.3.1)."""
    return _box(
        b"avcC",
        bytes(
            (
                1,        # configurationVersion
                sps[1],   # AVCProfileIndication
                sps[2],   # profile_compatibility
                sps[3],   # AVCLevelIndication
                0xFF,     # '111111' reserved + lengthSizeMinusOne = 3 (4-byte NALs)
                0xE1,     # '111' reserved + numOfSequenceParameterSets = 1
            )
        ),
        struct.pack(">H", len(sps)), sps,
        b"\x01",                                  # numOfPictureParameterSets
        struct.pack(">H", len(pps)), pps,
    )


def _avc1(width: int, height: int, sps: bytes, pps: bytes) -> bytes:
    """VisualSampleEntry for AVC (ISO/IEC 14496-12 clause 12.1.3)."""
    compressor = b"AVC Coding"
    compressorname = bytes([len(compressor)]) + compressor
    compressorname += b"\x00" * (32 - len(compressorname))
    return _box(
        b"avc1",
        b"\x00" * 6,                      # reserved
        struct.pack(">H", 1),             # data_reference_index
        struct.pack(">H", 0),             # pre_defined
        struct.pack(">H", 0),             # reserved
        b"\x00" * 12,                     # pre_defined[3]
        struct.pack(">HH", width, height),
        struct.pack(">I", 0x00480000),    # horizresolution 72 dpi
        struct.pack(">I", 0x00480000),    # vertresolution 72 dpi
        struct.pack(">I", 0),             # reserved
        struct.pack(">H", 1),             # frame_count
        compressorname,
        struct.pack(">H", 0x0018),        # depth: 24-bit colour, no alpha
        struct.pack(">h", -1),            # pre_defined
        _avcc(sps, pps),
    )


def _stbl(width: int, height: int, sps: bytes, pps: bytes) -> bytes:
    """Sample table.  All tables are empty: samples live in the fragments."""
    return _box(
        b"stbl",
        _full_box(b"stsd", 0, 0, struct.pack(">I", 1), _avc1(width, height, sps, pps)),
        _full_box(b"stts", 0, 0, struct.pack(">I", 0)),
        _full_box(b"stsc", 0, 0, struct.pack(">I", 0)),
        _full_box(b"stsz", 0, 0, struct.pack(">II", 0, 0)),
        _full_box(b"stco", 0, 0, struct.pack(">I", 0)),
    )


def _minf(width: int, height: int, sps: bytes, pps: bytes) -> bytes:
    dref = _full_box(
        b"dref", 0, 0,
        struct.pack(">I", 1),
        _full_box(b"url ", 0, 1),  # flags bit 0 = media is in this same file
    )
    return _box(
        b"minf",
        _full_box(b"vmhd", 0, 1, struct.pack(">HHHH", 0, 0, 0, 0)),
        _box(b"dinf", dref),
        _stbl(width, height, sps, pps),
    )


def _mdia(timescale: int, width: int, height: int, sps: bytes, pps: bytes) -> bytes:
    hdlr = _full_box(
        b"hdlr", 0, 0,
        struct.pack(">I", 0),     # pre_defined
        b"vide",                  # handler_type
        b"\x00" * 12,             # reserved
        b"VideoHandler\x00",
    )
    mdhd = _full_box(
        b"mdhd", 0, 0,
        struct.pack(
            ">IIIIHH",
            0,            # creation_time
            0,            # modification_time
            timescale,
            0,            # duration: unknown / live
            0x55C4,       # language: 'und' packed as three 5-bit values
            0,            # pre_defined
        ),
    )
    return _box(b"mdia", mdhd, hdlr, _minf(width, height, sps, pps))


def _trak(timescale: int, width: int, height: int, sps: bytes, pps: bytes) -> bytes:
    tkhd = _full_box(
        b"tkhd", 0, 0x000007,  # enabled | in_movie | in_preview
        struct.pack(
            ">IIIII",
            0,   # creation_time
            0,   # modification_time
            1,   # track_ID
            0,   # reserved
            0,   # duration: unknown / live
        ),
        b"\x00" * 8,                       # reserved
        struct.pack(">hhhH", 0, 0, 0, 0),  # layer, alternate_group, volume, reserved
        _UNITY_MATRIX,
        struct.pack(">II", width << 16, height << 16),  # 16.16 display size
    )
    return _box(b"trak", tkhd, _mdia(timescale, width, height, sps, pps))


def _moov(timescale: int, width: int, height: int, sps: bytes, pps: bytes) -> bytes:
    mvhd = _full_box(
        b"mvhd", 0, 0,
        struct.pack(
            ">IIII",
            0,          # creation_time
            0,          # modification_time
            timescale,
            0,          # duration: unknown / live
        ),
        struct.pack(">IH", 0x00010000, 0x0100),  # rate 1.0, volume 1.0
        b"\x00" * 10,                            # reserved
        _UNITY_MATRIX,
        b"\x00" * 24,                            # pre_defined[6]
        struct.pack(">I", 2),                    # next_track_ID
    )
    # mvex tells the parser that samples arrive in movie fragments.
    trex = _full_box(
        b"trex", 0, 0,
        struct.pack(
            ">IIIII",
            1,  # track_ID
            1,  # default_sample_description_index
            0,  # default_sample_duration
            0,  # default_sample_size
            0,  # default_sample_flags
        ),
    )
    return _box(
        b"moov",
        mvhd,
        _trak(timescale, width, height, sps, pps),
        _box(b"mvex", trex),
    )


# trun sample_flags layout (ISO/IEC 14496-12 clause 8.8.3.1):
#   reserved(4) is_leading(2) sample_depends_on(2) sample_is_depended_on(2)
#   sample_has_redundancy(2) sample_padding_value(3) sample_is_non_sync(1)
#   sample_degradation_priority(16)
#: Sync sample: does not depend on others (depends_on=2), is a sync sample.
SAMPLE_FLAGS_SYNC = 2 << 24
#: Non-sync sample: depends on others (depends_on=1) and is not a random
#: access point.  Chrome refuses to seek if this is wrong.
SAMPLE_FLAGS_NON_SYNC = (1 << 24) | (1 << 16)


@dataclass
class _Sample:
    data: bytes
    duration: int
    is_keyframe: bool


def _build_media_segment(
    sequence_number: int, base_media_decode_time: int, samples: Sequence[_Sample]
) -> bytes:
    """Build one ``moof`` + ``mdat`` pair.

    ``trun``'s ``data_offset`` is relative to the start of the ``moof`` box
    (because ``tfhd`` sets ``default-base-is-moof``), so it is exactly
    ``len(moof) + 8`` -- the moof plus the mdat box header.  Getting this
    wrong is the classic fMP4 bug, so the moof is built twice: once to learn
    its length, once with the real offset patched in.  The length of the
    ``data_offset`` field is fixed at 4 bytes, so the size does not change
    between the two passes.
    """
    mfhd = _full_box(b"mfhd", 0, 0, struct.pack(">I", sequence_number))
    tfhd = _full_box(
        b"tfhd", 0, 0x020000,  # default-base-is-moof
        struct.pack(">I", 1),  # track_ID
    )
    tfdt = _full_box(
        b"tfdt", 1, 0, struct.pack(">Q", base_media_decode_time)
    )

    # data-offset | sample-duration | sample-size | sample-flags present
    trun_flags = 0x000001 | 0x000100 | 0x000200 | 0x000400
    per_sample = b"".join(
        struct.pack(
            ">III",
            s.duration,
            len(s.data),
            SAMPLE_FLAGS_SYNC if s.is_keyframe else SAMPLE_FLAGS_NON_SYNC,
        )
        for s in samples
    )

    def make(data_offset: int) -> bytes:
        trun = _full_box(
            b"trun", 0, trun_flags,
            struct.pack(">Ii", len(samples), data_offset),
            per_sample,
        )
        return _box(b"moof", mfhd, _box(b"traf", tfhd, tfdt, trun))

    moof = make(0)
    moof = make(len(moof) + 8)
    payload = b"".join(s.data for s in samples)
    return moof + _box(b"mdat", payload)


# --------------------------------------------------------------------------- #
# Muxer
# --------------------------------------------------------------------------- #

class FragmentedMP4Muxer:
    """Turns :class:`Frame` objects into MSE-ready fragmented-MP4 segments.

    Call :meth:`add_frame` for every frame.  The muxer silently swallows
    frames until it has seen both an SPS and a PPS (it cannot describe the
    track before then); after that :meth:`init_segment` returns the
    ``ftyp`` + ``moov`` pair and every :meth:`add_frame` returns one
    ``moof`` + ``mdat`` media segment.
    """

    #: Inter-frame gaps outside this range (ms) are treated as bogus and
    #: replaced by the default frame duration.
    MAX_PLAUSIBLE_GAP_MS = 10_000

    def __init__(self, timescale: int = 90000, default_fps: float = 30.0) -> None:
        if timescale <= 0:
            raise ValueError("timescale must be positive")
        if default_fps <= 0:
            raise ValueError("default_fps must be positive")
        self.timescale = int(timescale)
        self.default_fps = float(default_fps)
        self._sps: bytes | None = None
        self._pps: bytes | None = None
        self._sps_info: dict | None = None
        self._sequence_number = 0
        self._first_ts: int | None = None
        self._prev_ts: int | None = None

    # -- properties -------------------------------------------------------- #

    @property
    def ready(self) -> bool:
        """True once both SPS and PPS have been seen."""
        return self._sps is not None and self._pps is not None

    @property
    def width(self) -> int:
        return self._sps_info["width"] if self._sps_info else 0

    @property
    def height(self) -> int:
        return self._sps_info["height"] if self._sps_info else 0

    @property
    def codec_string(self) -> str:
        return self._sps_info["codec_string"] if self._sps_info else ""

    @property
    def sequence_number(self) -> int:
        """Number of media segments emitted so far."""
        return self._sequence_number

    #: Default sample duration in the movie timescale.
    @property
    def _default_duration(self) -> int:
        return max(1, int(round(self.timescale / self.default_fps)))

    # -- segments ---------------------------------------------------------- #

    def init_segment(self) -> bytes | None:
        """``ftyp`` + ``moov`` (with ``mvex``/``trex``).  ``None`` until ready."""
        if not self.ready:
            return None
        assert self._sps is not None and self._pps is not None
        return _ftyp() + _moov(
            self.timescale, self.width, self.height, self._sps, self._pps
        )

    def add_frame(self, frame: Frame) -> bytes | None:
        """Return one ``moof`` + ``mdat`` media segment, or ``None`` if not ready.

        Parameter sets found in the access unit are harvested for ``avcC`` and
        then stripped, the remaining NALs are rewritten as AVCC, and the
        sample duration is taken from the gap to the previous frame's
        timestamp (falling back to ``default_fps``).
        """
        self._harvest_parameter_sets(frame.data)
        if not self.ready:
            return None

        payload = annexb_to_avcc(frame.data)
        if not payload:
            logger.debug("access unit at %d ms contained no VCL NAL", frame.timestamp_ms)
            return None

        ts = int(frame.timestamp_ms)
        if self._first_ts is None:
            self._first_ts = ts
        # The media timeline starts at zero even though the camera's clock
        # does not; tfdt is authoritative for MSE so this keeps the buffered
        # range anchored at 0.
        decode_time = max(0, ts - self._first_ts) * self.timescale // 1000

        duration = self._default_duration
        if self._prev_ts is not None:
            gap = ts - self._prev_ts
            if 0 < gap <= self.MAX_PLAUSIBLE_GAP_MS:
                duration = max(1, gap * self.timescale // 1000)
        self._prev_ts = ts

        self._sequence_number += 1
        return _build_media_segment(
            self._sequence_number,
            decode_time,
            [_Sample(data=payload, duration=duration, is_keyframe=frame.is_keyframe)],
        )

    # -- internals --------------------------------------------------------- #

    def _harvest_parameter_sets(self, annexb: bytes) -> None:
        for nal in iter_nal_units(annexb):
            nal_type = nal[0] & 0x1F
            if nal_type == NAL_SPS and self._sps is None:
                try:
                    self._sps_info = parse_sps(nal)
                except ValueError:
                    logger.warning("unparsable SPS, ignoring", exc_info=True)
                    continue
                self._sps = nal
            elif nal_type == NAL_PPS and self._pps is None:
                self._pps = nal


# --------------------------------------------------------------------------- #
# File writer
# --------------------------------------------------------------------------- #

class MP4FileWriter:
    """Writes a playable fragmented ``.mp4``.  Usable as a context manager.

    ``fileobj_or_path`` may be a path or any binary file-like object with a
    ``write`` method; an object we were handed is never closed by us.
    """

    def __init__(
        self,
        fileobj_or_path: str | os.PathLike[str] | IO[bytes],
        timescale: int = 90000,
        default_fps: float = 30.0,
    ) -> None:
        self.muxer = FragmentedMP4Muxer(timescale=timescale, default_fps=default_fps)
        if hasattr(fileobj_or_path, "write"):
            self._fh: IO[bytes] = fileobj_or_path  # type: ignore[assignment]
            self._owns_file = False
        else:
            self._fh = open(fileobj_or_path, "wb")
            self._owns_file = True
        self._wrote_init = False
        self._closed = False
        self.frames_written = 0

    def write_frame(self, frame: Frame) -> None:
        """Append one frame; writes the init segment first if it is still due."""
        if self._closed:
            raise ValueError("writer is closed")
        segment = self.muxer.add_frame(frame)
        if segment is None:
            return
        if not self._wrote_init:
            init = self.muxer.init_segment()
            assert init is not None
            self._fh.write(init)
            self._wrote_init = True
        self._fh.write(segment)
        self.frames_written += 1

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._wrote_init:
            logger.warning("no SPS/PPS seen; MP4 left empty")
        try:
            flush = getattr(self._fh, "flush", None)
            if flush is not None:
                flush()
        finally:
            if self._owns_file:
                self._fh.close()

    def __enter__(self) -> MP4FileWriter:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
