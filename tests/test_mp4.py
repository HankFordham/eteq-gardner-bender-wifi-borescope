"""Tests for :mod:`eteq.mp4`.

These run anywhere: pytest plus the standard library, no ffmpeg, and the only
input is the small committed fixture ``tests/data/sample.h264`` (320x240
Baseline H.264, 20 frames, 2 IDRs, generated once with ffmpeg and checked in).
The 7 MB real-camera capture is deliberately *not* required.
"""

from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

import pytest

# Make the package importable when the repo has not been pip-installed, so a
# bare `pytest` works from a fresh checkout on any platform.
_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from eteq.mp4 import (  # noqa: E402  (import must follow the path bootstrap)
    SAMPLE_FLAGS_NON_SYNC,
    SAMPLE_FLAGS_SYNC,
    FragmentedMP4Muxer,
    Frame,
    H264Framer,
    MP4FileWriter,
    annexb_to_avcc,
    iter_nal_units,
    parse_sps,
)

FIXTURE = Path(__file__).parent / "data" / "sample.h264"

#: ISO-BMFF boxes whose payload is just more boxes.
_CONTAINER_BOXES = {
    "moov", "trak", "mdia", "minf", "stbl", "dinf", "mvex", "moof", "traf",
}


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def raw() -> bytes:
    data = FIXTURE.read_bytes()
    assert data, "fixture is empty"
    return data


@pytest.fixture(scope="module")
def frames(raw: bytes) -> list[Frame]:
    framer = H264Framer()
    out = framer.push(raw)
    out += framer.flush()
    return out


def sps_of(data: bytes) -> bytes:
    for nal in iter_nal_units(data):
        if (nal[0] & 0x1F) == 7:
            return nal
    raise AssertionError("no SPS in stream")


def walk_boxes(data: bytes, start: int = 0, end: int | None = None):
    """Parse a flat run of ISO-BMFF boxes.

    Returns ``[(type, box_start, size, payload_start, payload_end), ...]`` and
    asserts that the declared sizes tile the range exactly, with no gap and no
    leftover bytes.
    """
    if end is None:
        end = len(data)
    boxes = []
    pos = start
    while pos < end:
        assert end - pos >= 8, f"truncated box header at {pos} ({end - pos} bytes left)"
        size, raw_type = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:                                   # 64-bit largesize
            assert end - pos >= 16
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:                                 # box extends to EOF
            size = end - pos
        assert size >= header, f"box {raw_type!r} at {pos} declares size {size}"
        assert pos + size <= end, (
            f"box {raw_type!r} at {pos} size {size} overruns container end {end}"
        )
        boxes.append(
            (raw_type.decode("latin1"), pos, size, pos + header, pos + size)
        )
        pos += size
    assert pos == end, f"leftover bytes: consumed up to {pos}, container ends at {end}"
    return boxes


def find_box(data: bytes, boxes, name: str):
    """Depth-first search for one box by type, descending into containers."""
    for box in boxes:
        if box[0] == name:
            return box
        children = None
        if box[0] in _CONTAINER_BOXES:
            children = walk_boxes(data, box[3], box[4])
        elif box[0] == "stsd":
            children = walk_boxes(data, box[3] + 8, box[4])  # ver/flags + count
        elif box[0] == "avc1":
            children = walk_boxes(data, box[3] + 78, box[4])  # VisualSampleEntry
        if children:
            hit = find_box(data, children, name)
            if hit is not None:
                return hit
    return None


def mux_all(frames: list[Frame]) -> bytes:
    buf = io.BytesIO()
    with MP4FileWriter(buf) as writer:
        for frame in frames:
            writer.write_frame(frame)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# SPS parsing
# --------------------------------------------------------------------------- #

def test_parse_sps_fixture_dimensions(raw: bytes) -> None:
    info = parse_sps(sps_of(raw))
    assert info["width"] == 320
    assert info["height"] == 240
    assert info["profile_idc"] == 66           # Baseline
    assert info["level_idc"] == 30             # level 3.0
    assert info["codec_string"] == "avc1.{:02X}{:02X}{:02X}".format(
        info["profile_idc"], info["constraint_flags"], info["level_idc"]
    )
    assert info["codec_string"].startswith("avc1.42")


def test_parse_sps_strips_emulation_prevention(raw: bytes) -> None:
    """The fixture's SPS really does contain 00 00 03 sequences."""
    sps = sps_of(raw)
    assert b"\x00\x00\x03" in sps, "fixture no longer exercises the EP-byte path"
    assert parse_sps(sps)["width"] == 320      # would misparse without stripping


def test_parse_sps_known_camera_sps() -> None:
    """The real camera's SPS, captured verbatim from live.h264."""
    sps = bytes([0x67, 0x42, 0x00, 0x1E, 0xA9, 0x50, 0x14, 0x0F, 0xC8])
    assert parse_sps(sps) == {
        "width": 640,
        "height": 240,
        "profile_idc": 0x42,
        "constraint_flags": 0x00,
        "level_idc": 0x1E,
        "codec_string": "avc1.42001E",
    }


def test_parse_sps_applies_vertical_frame_cropping() -> None:
    """1920x1080 High profile: 1088 coded rows cropped down to 1080.

    Exercises the 4:2:0 crop units -- CropUnitY is 2 for a progressive frame,
    so crop_bottom = 4 must remove 8 luma rows, not 4.  (Real x264 output.)
    """
    sps = bytes.fromhex("67640028acd940780227e5c044000003000400000300283c60c658")
    info = parse_sps(sps)
    assert (info["width"], info["height"]) == (1920, 1080)
    assert info["profile_idc"] == 100
    assert info["codec_string"] == "avc1.640028"


def test_parse_sps_applies_horizontal_frame_cropping() -> None:
    """854x480: 856 coded columns cropped to 854 (CropUnitX = 2 for 4:2:0)."""
    sps = bytes.fromhex("67640016acd940d83de6f0110000030001000003000a0f162d96")
    info = parse_sps(sps)
    assert (info["width"], info["height"]) == (854, 480)
    assert info["codec_string"] == "avc1.640016"


def test_parse_sps_tolerates_start_code() -> None:
    sps = sps_of(FIXTURE.read_bytes())
    assert parse_sps(b"\x00\x00\x00\x01" + sps) == parse_sps(sps)
    assert parse_sps(b"\x00\x00\x01" + sps) == parse_sps(sps)


def test_parse_sps_rejects_non_sps() -> None:
    with pytest.raises(ValueError):
        parse_sps(bytes([0x68, 0xCB, 0x83, 0xCB, 0x20]))
    with pytest.raises(ValueError):
        parse_sps(b"\x67\x42")


# --------------------------------------------------------------------------- #
# Framer
# --------------------------------------------------------------------------- #

def test_framer_finds_expected_frame_count(frames: list[Frame]) -> None:
    assert len(frames) == 20


def test_framer_is_chunk_size_independent(raw: bytes) -> None:
    """Identical frame boundaries no matter how the stream is sliced."""
    results = {}
    for size in (1, 7, 1000, len(raw)):
        framer = H264Framer()
        out: list[Frame] = []
        for offset in range(0, len(raw), size):
            out += framer.push(raw[offset:offset + size])
        out += framer.flush()
        results[size] = [(f.data, f.is_keyframe) for f in out]

    reference = results[len(raw)]
    assert reference, "whole-file pass produced nothing"
    for size, got in results.items():
        assert got == reference, f"chunk size {size} disagrees with whole-file framing"


def test_framer_preserves_all_payload_bytes(raw: bytes, frames: list[Frame]) -> None:
    """Every NAL in the input shows up in exactly one access unit, in order."""
    source = [bytes(n) for n in iter_nal_units(raw)]
    seen: list[bytes] = []
    for frame in frames:
        seen.extend(iter_nal_units(frame.data))
    assert seen == source


def test_framer_access_unit_shape(frames: list[Frame]) -> None:
    """Each access unit holds exactly one VCL NAL (the camera never splits slices)."""
    for frame in frames:
        vcl = [n for n in iter_nal_units(frame.data) if 1 <= (n[0] & 0x1F) <= 5]
        assert len(vcl) == 1
        assert frame.data.startswith(b"\x00\x00\x00\x01")


def test_framer_keyframe_detection(frames: list[Frame]) -> None:
    flags = [f.is_keyframe for f in frames]
    assert flags[0] is True, "stream must start on a keyframe"
    assert sum(flags) == 2, "fixture was encoded with keyint=10 over 20 frames"
    for frame in frames:
        has_idr = any((n[0] & 0x1F) == 5 for n in iter_nal_units(frame.data))
        assert frame.is_keyframe is has_idr


def test_framer_parameter_sets_attach_to_their_keyframe(frames: list[Frame]) -> None:
    """SPS/PPS precede the IDR inside the same access unit, not a separate one."""
    for frame in frames:
        if frame.is_keyframe:
            types = [n[0] & 0x1F for n in iter_nal_units(frame.data)]
            assert 7 in types and 8 in types
            assert types[-1] == 5


def test_framer_synthesises_timestamps_when_absent(raw: bytes) -> None:
    framer = H264Framer(default_fps=25.0)
    out = framer.push(raw) + framer.flush()
    assert [f.timestamp_ms for f in out[:5]] == [0, 40, 80, 120, 160]


def test_framer_default_fps_is_30(raw: bytes) -> None:
    framer = H264Framer()
    out = framer.push(raw) + framer.flush()
    assert [f.timestamp_ms for f in out[:4]] == [0, 33, 67, 100]


def test_framer_uses_supplied_timestamps(raw: bytes) -> None:
    """One timestamped push per frame, exactly how the camera feeds us."""
    framer = H264Framer()
    reference = H264Framer()
    whole = reference.push(raw) + reference.flush()

    # Re-cut the stream at access-unit boundaries, then replay with timestamps.
    stamps = [108, 142, 175, 208, 242]
    out: list[Frame] = []
    for index, frame in enumerate(whole):
        ts = stamps[index] if index < len(stamps) else None
        out += framer.push(frame.data, ts)
    out += framer.flush()

    assert len(out) == len(whole)
    assert [f.timestamp_ms for f in out[:len(stamps)]] == stamps
    # After the supplied stamps run out we fall back to default_fps from the
    # last known time rather than restarting at zero.
    assert out[len(stamps)].timestamp_ms == stamps[-1] + 33


def test_framer_push_without_data_is_harmless() -> None:
    framer = H264Framer()
    assert framer.push(b"") == []
    assert framer.flush() == []


def test_framer_ignores_leading_garbage() -> None:
    framer = H264Framer()
    data = b"\xde\xad\xbe\xef" + b"\x00\x00\x00\x01\x65\x88\x82\x00"
    out = framer.push(data) + framer.flush()
    assert len(out) == 1
    assert out[0].is_keyframe


def test_framer_rejects_bad_fps() -> None:
    with pytest.raises(ValueError):
        H264Framer(default_fps=0)


# --------------------------------------------------------------------------- #
# Annex B -> AVCC
# --------------------------------------------------------------------------- #

def test_annexb_to_avcc_drops_parameter_sets(frames: list[Frame]) -> None:
    keyframe = frames[0]
    avcc = annexb_to_avcc(keyframe.data)
    # Walk the length-prefixed NALs back out.
    types, pos = [], 0
    while pos < len(avcc):
        (length,) = struct.unpack_from(">I", avcc, pos)
        types.append(avcc[pos + 4] & 0x1F)
        pos += 4 + length
    assert pos == len(avcc), "AVCC length prefixes do not tile the payload"
    assert types == [5], "SPS/PPS/SEI must be stripped from the sample payload"


def test_annexb_to_avcc_can_keep_parameter_sets(frames: list[Frame]) -> None:
    kept = annexb_to_avcc(frames[0].data, drop_parameter_sets=False)
    assert len(kept) > len(annexb_to_avcc(frames[0].data))


# --------------------------------------------------------------------------- #
# Muxer: init segment
# --------------------------------------------------------------------------- #

def test_muxer_not_ready_before_parameter_sets() -> None:
    muxer = FragmentedMP4Muxer()
    assert muxer.ready is False
    assert muxer.init_segment() is None
    # A lone P-frame teaches it nothing, so it stays unready and emits nothing.
    assert muxer.add_frame(Frame(b"\x00\x00\x00\x01\x41\x9a", 0, False)) is None
    assert muxer.ready is False


def test_muxer_becomes_ready_and_reports_geometry(frames: list[Frame]) -> None:
    muxer = FragmentedMP4Muxer()
    assert muxer.add_frame(frames[0]) is not None
    assert muxer.ready is True
    assert (muxer.width, muxer.height) == (320, 240)
    assert muxer.codec_string.startswith("avc1.42")


def test_init_segment_box_structure(frames: list[Frame]) -> None:
    muxer = FragmentedMP4Muxer()
    muxer.add_frame(frames[0])
    init = muxer.init_segment()
    assert init is not None

    top = walk_boxes(init)
    assert [b[0] for b in top] == ["ftyp", "moov"]

    ftyp = top[0]
    brands = init[ftyp[3]:ftyp[4]]
    assert brands[:4] == b"isom"                      # major_brand
    for brand in (b"isom", b"iso2", b"avc1", b"mp41", b"iso5"):
        assert brand in brands[8:], f"missing compatible brand {brand!r}"

    moov = walk_boxes(init, top[1][3], top[1][4])
    assert [b[0] for b in moov] == ["mvhd", "trak", "mvex"]

    trak = walk_boxes(init, moov[1][3], moov[1][4])
    assert [b[0] for b in trak] == ["tkhd", "mdia"]

    mdia = walk_boxes(init, trak[1][3], trak[1][4])
    assert [b[0] for b in mdia] == ["mdhd", "hdlr", "minf"]

    minf = walk_boxes(init, mdia[2][3], mdia[2][4])
    assert [b[0] for b in minf] == ["vmhd", "dinf", "stbl"]

    stbl_box = minf[2]
    stbl = walk_boxes(init, stbl_box[3], stbl_box[4])
    assert [b[0] for b in stbl] == ["stsd", "stts", "stsc", "stsz", "stco"]

    mvex = walk_boxes(init, moov[2][3], moov[2][4])
    assert [b[0] for b in mvex] == ["trex"]


def test_init_segment_header_fields(frames: list[Frame]) -> None:
    muxer = FragmentedMP4Muxer(timescale=90000)
    muxer.add_frame(frames[0])
    init = muxer.init_segment()
    assert init is not None
    top = walk_boxes(init)

    mvhd = find_box(init, top, "mvhd")
    assert mvhd is not None
    timescale, duration = struct.unpack_from(">II", init, mvhd[3] + 4 + 8)
    assert (timescale, duration) == (90000, 0)

    tkhd = find_box(init, top, "tkhd")
    assert tkhd is not None
    version_flags = struct.unpack_from(">I", init, tkhd[3])[0]
    assert version_flags & 0xFFFFFF == 0x000007, "tkhd flags must be enabled|in-movie|in-preview"
    track_id, _, tk_duration = struct.unpack_from(">III", init, tkhd[3] + 4 + 8)
    assert track_id == 1
    assert tk_duration == 0

    mdhd = find_box(init, top, "mdhd")
    assert mdhd is not None
    md_timescale, md_duration = struct.unpack_from(">II", init, mdhd[3] + 4 + 8)
    assert (md_timescale, md_duration) == (90000, 0)

    hdlr = find_box(init, top, "hdlr")
    assert hdlr is not None
    assert init[hdlr[3] + 8:hdlr[3] + 12] == b"vide"

    dref = find_box(init, top, "dinf")
    assert dref is not None
    assert b"url " in init[dref[3]:dref[4]]

    trex = find_box(init, top, "trex")
    assert trex is not None
    fields = struct.unpack_from(">IIIII", init, trex[3] + 4)
    assert fields == (1, 1, 0, 0, 0)


def test_avc1_and_avcc_contents(raw: bytes, frames: list[Frame]) -> None:
    muxer = FragmentedMP4Muxer()
    muxer.add_frame(frames[0])
    init = muxer.init_segment()
    assert init is not None

    avc1 = find_box(init, walk_boxes(init), "avc1")
    assert avc1 is not None
    body = avc1[3]
    assert struct.unpack_from(">H", init, body + 6)[0] == 1          # data_reference_index
    width, height = struct.unpack_from(">HH", init, body + 24)
    assert (width, height) == (320, 240)
    name_len = init[body + 42]
    assert 0 < name_len <= 31
    assert init[body + 43:body + 43 + name_len] == b"AVC Coding"
    depth = struct.unpack_from(">H", init, body + 74)[0]
    assert depth == 0x0018                                          # 24-bit colour
    assert struct.unpack_from(">h", init, body + 76)[0] == -1       # pre_defined

    avcc = find_box(init, walk_boxes(init), "avcC")
    assert avcc is not None
    cfg = init[avcc[3]:avcc[4]]
    sps = sps_of(raw)
    pps = next(n for n in iter_nal_units(raw) if (n[0] & 0x1F) == 8)
    assert cfg[0] == 1                       # configurationVersion
    assert cfg[1] == sps[1]                  # AVCProfileIndication
    assert cfg[2] == sps[2]                  # profile_compatibility
    assert cfg[3] == sps[3]                  # AVCLevelIndication
    assert cfg[4] == 0xFF                    # lengthSizeMinusOne == 3
    assert cfg[5] == 0xE1                    # numOfSequenceParameterSets == 1
    assert struct.unpack_from(">H", cfg, 6)[0] == len(sps)
    assert cfg[8:8 + len(sps)] == sps
    tail = 8 + len(sps)
    assert cfg[tail] == 1                    # numOfPictureParameterSets
    assert struct.unpack_from(">H", cfg, tail + 1)[0] == len(pps)
    assert cfg[tail + 3:tail + 3 + len(pps)] == pps
    assert tail + 3 + len(pps) == len(cfg), "trailing bytes in avcC"


def test_init_segment_is_stable(frames: list[Frame]) -> None:
    muxer = FragmentedMP4Muxer()
    muxer.add_frame(frames[0])
    assert muxer.init_segment() == muxer.init_segment()


# --------------------------------------------------------------------------- #
# Muxer: media segments
# --------------------------------------------------------------------------- #

def media_segment_parts(segment: bytes):
    """Split one media segment and return the pieces the tests poke at."""
    top = walk_boxes(segment)
    assert [b[0] for b in top] == ["moof", "mdat"]
    moof, mdat = top
    moof_children = walk_boxes(segment, moof[3], moof[4])
    assert [b[0] for b in moof_children] == ["mfhd", "traf"]
    traf = walk_boxes(segment, moof_children[1][3], moof_children[1][4])
    assert [b[0] for b in traf] == ["tfhd", "tfdt", "trun"]
    return moof, mdat, moof_children[0], traf[0], traf[1], traf[2]


def test_media_segment_structure(frames: list[Frame]) -> None:
    muxer = FragmentedMP4Muxer()
    segment = muxer.add_frame(frames[0])
    assert segment is not None
    moof, mdat, mfhd, tfhd, tfdt, trun = media_segment_parts(segment)

    assert struct.unpack_from(">I", segment, mfhd[3] + 4)[0] == 1  # sequence_number

    tfhd_flags = struct.unpack_from(">I", segment, tfhd[3])[0] & 0xFFFFFF
    assert tfhd_flags & 0x020000, "tfhd must set default-base-is-moof"
    assert struct.unpack_from(">I", segment, tfhd[3] + 4)[0] == 1  # track_ID

    assert segment[tfdt[3]] == 1, "tfdt must be version 1 (64-bit decode time)"
    assert struct.unpack_from(">Q", segment, tfdt[3] + 4)[0] == 0

    trun_flags = struct.unpack_from(">I", segment, trun[3])[0] & 0xFFFFFF
    for bit in (0x000001, 0x000100, 0x000200, 0x000400):
        assert trun_flags & bit, f"trun flag {bit:#08x} missing"
    assert struct.unpack_from(">I", segment, trun[3] + 4)[0] == 1  # sample_count


def test_media_segment_sequence_numbers_increase(frames: list[Frame]) -> None:
    muxer = FragmentedMP4Muxer()
    numbers = []
    for frame in frames:
        segment = muxer.add_frame(frame)
        assert segment is not None
        _, _, mfhd, _, _, _ = media_segment_parts(segment)
        numbers.append(struct.unpack_from(">I", segment, mfhd[3] + 4)[0])
    assert numbers == list(range(1, len(frames) + 1))


def test_trun_data_offset_points_at_mdat_payload(frames: list[Frame]) -> None:
    """data_offset is measured from the start of the moof, not the file."""
    muxer = FragmentedMP4Muxer()
    for frame in frames:
        segment = muxer.add_frame(frame)
        assert segment is not None
        moof, mdat, _, _, _, trun = media_segment_parts(segment)
        data_offset = struct.unpack_from(">i", segment, trun[3] + 8)[0]
        assert data_offset == moof[2] + 8, "expected len(moof) + mdat header"
        assert data_offset == mdat[3], "mdat payload starts at moof_start + data_offset"
        sample_size = struct.unpack_from(">I", segment, trun[3] + 16)[0]
        assert sample_size == mdat[4] - mdat[3], "sample size must match mdat payload"
        assert segment[data_offset:data_offset + 4] == struct.pack(
            ">I", sample_size - 4
        ), "first bytes at data_offset must be the leading AVCC length prefix"


def test_trun_sample_payload_is_avcc(frames: list[Frame]) -> None:
    muxer = FragmentedMP4Muxer()
    segment = muxer.add_frame(frames[0])
    assert segment is not None
    _, mdat, _, _, _, _ = media_segment_parts(segment)
    assert segment[mdat[3]:mdat[4]] == annexb_to_avcc(frames[0].data)


def test_trun_sample_flags_mark_sync_and_non_sync(frames: list[Frame]) -> None:
    muxer = FragmentedMP4Muxer()
    for frame in frames:
        segment = muxer.add_frame(frame)
        assert segment is not None
        _, _, _, _, _, trun = media_segment_parts(segment)
        flags = struct.unpack_from(">I", segment, trun[3] + 20)[0]
        depends_on = (flags >> 24) & 0x3
        non_sync = (flags >> 16) & 0x1
        if frame.is_keyframe:
            assert flags == SAMPLE_FLAGS_SYNC
            assert (depends_on, non_sync) == (2, 0)
        else:
            assert flags == SAMPLE_FLAGS_NON_SYNC
            assert (depends_on, non_sync) == (1, 1)


def test_sample_flag_constants() -> None:
    assert SAMPLE_FLAGS_SYNC == 0x02000000
    assert SAMPLE_FLAGS_NON_SYNC == 0x01010000


# --------------------------------------------------------------------------- #
# Muxer: timing
# --------------------------------------------------------------------------- #

def decode_times_and_durations(frames: list[Frame], **kwargs):
    muxer = FragmentedMP4Muxer(**kwargs)
    times, durations = [], []
    for frame in frames:
        segment = muxer.add_frame(frame)
        assert segment is not None
        _, _, _, _, tfdt, trun = media_segment_parts(segment)
        times.append(struct.unpack_from(">Q", segment, tfdt[3] + 4)[0])
        durations.append(struct.unpack_from(">I", segment, trun[3] + 12)[0])
    return times, durations


def test_decode_times_follow_supplied_timestamps(frames: list[Frame]) -> None:
    stamped = [
        Frame(f.data, 108 + 33 * i, f.is_keyframe) for i, f in enumerate(frames)
    ]
    times, durations = decode_times_and_durations(stamped, timescale=90000)
    # The camera clock starts at 108 ms; the media timeline is rebased to zero.
    assert times[0] == 0
    assert times == [33 * i * 90000 // 1000 for i in range(len(stamped))]
    # First sample has no predecessor, so it falls back to default_fps.
    assert durations[0] == 3000                 # 90000 / 30
    assert all(d == 33 * 90 for d in durations[1:])


def test_durations_fall_back_to_default_fps(frames: list[Frame]) -> None:
    """All timestamps identical: every gap is zero, so default_fps wins."""
    flat = [Frame(f.data, 500, f.is_keyframe) for f in frames]
    times, durations = decode_times_and_durations(flat, timescale=1000, default_fps=25.0)
    assert times == [0] * len(flat)
    assert durations == [40] * len(flat)


def test_durations_reject_implausible_gaps(frames: list[Frame]) -> None:
    """A clock jump must not become a multi-hour sample duration."""
    jumpy = [
        Frame(frames[0].data, 0, True),
        Frame(frames[1].data, 10_000_000, False),
    ]
    _, durations = decode_times_and_durations(jumpy, timescale=1000, default_fps=10.0)
    assert durations == [100, 100]


def test_timescale_is_honoured(frames: list[Frame]) -> None:
    stamped = [Frame(f.data, 1000 * i, f.is_keyframe) for i, f in enumerate(frames[:3])]
    times, durations = decode_times_and_durations(stamped, timescale=1000)
    assert times == [0, 1000, 2000]
    assert durations[1:] == [1000, 1000]


def test_muxer_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError):
        FragmentedMP4Muxer(timescale=0)
    with pytest.raises(ValueError):
        FragmentedMP4Muxer(default_fps=0)


# --------------------------------------------------------------------------- #
# File writer + whole-file integrity
# --------------------------------------------------------------------------- #

def test_file_integrity_box_walk(frames: list[Frame]) -> None:
    """ftyp, moov, then moof/mdat pairs, with every size field exact."""
    data = mux_all(frames)
    top = walk_boxes(data)                     # also asserts "no leftover bytes"
    assert top[-1][4] == len(data)
    types = [b[0] for b in top]
    assert types[0] == "ftyp"
    assert types[1] == "moov"
    tail = types[2:]
    assert tail, "no media segments were written"
    assert len(tail) % 2 == 0
    assert tail == ["moof", "mdat"] * (len(tail) // 2)
    assert len(tail) // 2 == len(frames)


def test_file_integrity_nested_boxes(frames: list[Frame]) -> None:
    """Recursively re-walk every container; sizes must tile at every depth."""
    data = mux_all(frames)

    def descend(boxes) -> int:
        count = 0
        for box in boxes:
            count += 1
            if box[0] in _CONTAINER_BOXES:
                count += descend(walk_boxes(data, box[3], box[4]))
            elif box[0] == "stsd":
                count += descend(walk_boxes(data, box[3] + 8, box[4]))
            elif box[0] == "avc1":
                count += descend(walk_boxes(data, box[3] + 78, box[4]))
        return count

    assert descend(walk_boxes(data)) > 20


def test_writer_accepts_path(tmp_path: Path, frames: list[Frame]) -> None:
    out = tmp_path / "out.mp4"
    with MP4FileWriter(out) as writer:
        for frame in frames:
            writer.write_frame(frame)
        assert writer.frames_written == len(frames)
    data = out.read_bytes()
    assert data == mux_all(frames)
    assert [b[0] for b in walk_boxes(data)][:2] == ["ftyp", "moov"]


def test_writer_writes_init_segment_exactly_once(frames: list[Frame]) -> None:
    data = mux_all(frames)
    assert data.count(b"ftyp") == 1
    assert [b[0] for b in walk_boxes(data)].count("moov") == 1


def test_writer_does_not_close_borrowed_fileobj(frames: list[Frame]) -> None:
    buf = io.BytesIO()
    with MP4FileWriter(buf) as writer:
        writer.write_frame(frames[0])
    assert buf.closed is False
    assert buf.getvalue()


def test_writer_with_no_parameter_sets_produces_empty_file(tmp_path: Path) -> None:
    out = tmp_path / "empty.mp4"
    with MP4FileWriter(out) as writer:
        writer.write_frame(Frame(b"\x00\x00\x00\x01\x41\x9a", 0, False))
    assert out.read_bytes() == b""


def test_writer_rejects_use_after_close(frames: list[Frame]) -> None:
    writer = MP4FileWriter(io.BytesIO())
    writer.write_frame(frames[0])
    writer.close()
    writer.close()  # idempotent
    with pytest.raises(ValueError):
        writer.write_frame(frames[0])


def test_standalone_mse_pair_is_self_describing(frames: list[Frame]) -> None:
    """Init segment + one media segment must stand alone, as MSE feeds them."""
    muxer = FragmentedMP4Muxer()
    first = muxer.add_frame(frames[0])
    assert first is not None
    init = muxer.init_segment()
    assert init is not None
    pair = init + first
    assert [b[0] for b in walk_boxes(pair)] == ["ftyp", "moov", "moof", "mdat"]
    assert find_box(pair, walk_boxes(pair), "avcC") is not None


def test_end_to_end_from_raw_bytes(raw: bytes, tmp_path: Path) -> None:
    """The real pipeline: socket chunks in, playable file out."""
    framer = H264Framer()
    out = tmp_path / "e2e.mp4"
    with MP4FileWriter(out) as writer:
        for offset in range(0, len(raw), 1337):
            for frame in framer.push(raw[offset:offset + 1337], 100 + offset):
                writer.write_frame(frame)
        for frame in framer.flush():
            writer.write_frame(frame)
        assert writer.muxer.codec_string.startswith("avc1.42")
        assert (writer.muxer.width, writer.muxer.height) == (320, 240)
    types = [b[0] for b in walk_boxes(out.read_bytes())]
    assert types[:2] == ["ftyp", "moov"]
    assert (len(types) - 2) // 2 == 20
