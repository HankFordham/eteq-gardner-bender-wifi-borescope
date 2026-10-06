package com.eteqcam.net;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Deque;
import java.util.List;

/**
 * Splits a continuous H.264 Annex B byte stream into access units. A faithful port of
 * the {@code H264Framer} class in {@code eteq/mp4.py} (and only that class: the Android
 * app decodes with hardware and needs no MP4 muxing).
 *
 * <p>Feed it whatever the socket gave you; it buffers across chunk boundaries and
 * returns complete {@link Frame} objects. The emitted {@code data} is normalised
 * Annex B: every NAL is prefixed with a four-byte start code and
 * {@code trailing_zero_8bits} padding is dropped, so the same stream split at
 * different chunk sizes always produces byte-identical frames. That is exactly what
 * {@code MediaCodec} wants when it is configured for {@code video/avc} with Annex B
 * input.
 *
 * <p>An access unit is closed when, after at least one VCL NAL (types 1-5), we meet
 * another VCL NAL or the first SPS / SEI / AUD. {@code keyframe} is set when the unit
 * contains an IDR slice (type 5).
 *
 * <p>Plain Java 17, standard library only.
 */
public final class H264Framer {

    /** One H.264 access unit plus the timing and keyframe metadata a decoder needs. */
    public static final class Frame {
        /** One Annex B access unit, start codes included. */
        public final byte[] data;
        /** Presentation time in milliseconds. */
        public final long timestampMs;
        public final boolean keyframe;

        public Frame(byte[] data, long timestampMs, boolean keyframe) {
            this.data = data;
            this.timestampMs = timestampMs;
            this.keyframe = keyframe;
        }
    }

    // NAL unit types we care about (ITU-T H.264 Table 7-1)
    private static final int NAL_SLICE = 1;  // coded slice of a non-IDR picture
    private static final int NAL_DPA = 2;
    private static final int NAL_DPB = 3;
    private static final int NAL_DPC = 4;
    private static final int NAL_IDR = 5;    // coded slice of an IDR picture (keyframe)
    private static final int NAL_SEI = 6;
    private static final int NAL_SPS = 7;
    private static final int NAL_AUD = 9;

    /**
     * Cap on un-consumed caller timestamps, so a stream that never yields an access
     * unit cannot grow the queue without bound. Oldest are dropped.
     */
    public static final int MAX_PENDING_TIMESTAMPS = 256;

    private static boolean isVcl(int t) {
        return t == NAL_SLICE || t == NAL_DPA || t == NAL_DPB || t == NAL_DPC || t == NAL_IDR;
    }

    /**
     * NAL types that may begin a new access unit once a VCL NAL has been seen: a VCL
     * NAL after a VCL NAL, or the first SPS / SEI / AUD after one. PPS is deliberately
     * not here; in practice it always trails an SPS, which has already opened the new
     * access unit.
     */
    private static boolean startsAccessUnit(int t) {
        return isVcl(t) || t == NAL_SEI || t == NAL_SPS || t == NAL_AUD;
    }

    private final double defaultFps;

    // The scan buffer. buf[0..len) holds unconsumed bytes.
    private byte[] buf = new byte[8192];
    private int len = 0;

    /**
     * Index in {@link #buf} where the in-progress NAL's payload starts, or -1 while we
     * have not yet seen the stream's first start code.
     */
    private int nalStart = -1;
    private int search = 0;                        // resume position for the start-code scan
    private final List<byte[]> nals = new ArrayList<>();  // NALs of the access unit being built
    private boolean seenVcl = false;
    private boolean isKeyframe = false;
    private long auTs = 0;

    /**
     * Timestamps supplied by the caller, consumed in order by access units as they
     * start.
     *
     * <p>This has to be a queue, not a single slot: an Annex B NAL only ends when the
     * <em>next</em> start code arrives, so feeding one timestamped camera frame per
     * push means each access unit is recognised one push after its own timestamp was
     * handed over. With a single slot every other timestamp would be overwritten
     * before it was ever used.
     */
    private final Deque<Long> tsQueue = new ArrayDeque<>();

    /** Next synthesised timestamp, in ms. Kept in floating point on purpose; see below. */
    private double synthNext = 0.0;

    public H264Framer() {
        this(30.0);
    }

    public H264Framer(double defaultFps) {
        if (defaultFps <= 0) {
            throw new IllegalArgumentException("defaultFps must be positive");
        }
        this.defaultFps = defaultFps;
    }

    /**
     * Feed arbitrary-sized chunks. Returns the access units that became complete.
     *
     * <p>Supplied timestamps are queued and handed to access units in arrival order, so
     * the natural usage -- one {@code push} per timestamped camera frame -- lines each
     * frame up with its own timestamp even though an access unit can only be
     * <em>recognised</em> one push later. A negative {@code timestampMs} means
     * "unknown": it contributes nothing to the queue, and any access unit that finds
     * the queue empty gets a timestamp synthesised from the default frame rate.
     */
    public List<Frame> push(byte[] data, long timestampMs) {
        if (timestampMs >= 0) {
            if (tsQueue.size() == MAX_PENDING_TIMESTAMPS) {
                tsQueue.pollFirst();
            }
            tsQueue.addLast(timestampMs);
        }
        List<Frame> frames = new ArrayList<>();
        if (data == null || data.length == 0) {
            return frames;
        }

        append(data);

        while (true) {
            int hit = findStartCode(search);
            if (hit < 0) {
                // A start code may straddle the next chunk; rescan the last two bytes
                // then. This keeps the scan O(len(data)) per push.
                search = Math.max(0, len - 2);
                break;
            }
            if (nalStart >= 0) {
                byte[] nal = takeNal(nalStart, hit);
                if (nal != null) {
                    Frame frame = acceptNal(nal);
                    if (frame != null) {
                        frames.add(frame);
                    }
                }
            }
            nalStart = hit + 3;
            search = hit + 3;
        }

        // Drop everything before the NAL currently under construction.
        int cut = nalStart >= 0 ? nalStart : 0;
        if (cut > 0) {
            System.arraycopy(buf, cut, buf, 0, len - cut);
            len -= cut;
            nalStart = 0;
            search = Math.max(0, search - cut);
        }
        return frames;
    }

    /** Close the stream: emit the final NAL and the pending access unit. */
    public List<Frame> flush() {
        List<Frame> frames = new ArrayList<>();
        if (nalStart >= 0) {
            byte[] nal = takeNal(nalStart, len);
            nalStart = -1;
            if (nal != null) {
                Frame frame = acceptNal(nal);
                if (frame != null) {
                    frames.add(frame);
                }
            }
        }
        len = 0;
        search = 0;
        Frame finalFrame = closeAu();
        if (finalFrame != null) {
            frames.add(finalFrame);
        }
        return frames;
    }

    // -- internals ------------------------------------------------------------

    private void append(byte[] data) {
        if (len + data.length > buf.length) {
            int cap = Math.max(buf.length * 2, len + data.length);
            buf = Arrays.copyOf(buf, cap);
        }
        System.arraycopy(data, 0, buf, len, data.length);
        len += data.length;
    }

    /** Index of the next three-byte start code at or after {@code from}, or -1. */
    private int findStartCode(int from) {
        for (int i = Math.max(0, from); i + 2 < len; i++) {
            if (buf[i] == 0 && buf[i + 1] == 0 && buf[i + 2] == 1) {
                return i;
            }
        }
        return -1;
    }

    /** The NAL in {@code [start, end)} with trailing zero padding removed, or null. */
    private byte[] takeNal(int start, int end) {
        while (end > start && buf[end - 1] == 0) {
            end--;
        }
        return end > start ? Arrays.copyOfRange(buf, start, end) : null;
    }

    /** Append one NAL, closing the previous access unit if it ends here. */
    private Frame acceptNal(byte[] nal) {
        int nalType = nal[0] & 0x1F;
        Frame finished = null;
        if (!nals.isEmpty() && seenVcl && startsAccessUnit(nalType)) {
            finished = closeAu();
        }
        if (nals.isEmpty()) {
            auTs = nextTimestamp();
        }
        nals.add(nal);
        if (isVcl(nalType)) {
            seenVcl = true;
            if (nalType == NAL_IDR) {
                isKeyframe = true;
            }
        }
        return finished;
    }

    private Frame closeAu() {
        if (nals.isEmpty()) {
            return null;
        }
        int total = 0;
        for (byte[] nal : nals) {
            total += 4 + nal.length;
        }
        byte[] data = new byte[total];
        int pos = 0;
        for (byte[] nal : nals) {
            data[pos + 2] = 0;
            data[pos + 3] = 1; // 00 00 00 01
            pos += 4;
            System.arraycopy(nal, 0, data, pos, nal.length);
            pos += nal.length;
        }
        Frame frame = new Frame(data, auTs, isKeyframe);
        nals.clear();
        seenVcl = false;
        isKeyframe = false;
        return frame;
    }

    /**
     * Timestamp for an access unit that is just starting.
     *
     * <p>The synthesised clock accumulates in floating point and is only rounded on the
     * way out, so a non-integral frame interval (33.33 ms at 30 fps) does not drift:
     * 0, 33, 67, 100, ... rather than 0, 33, 66, 99. {@link Math#rint} is used rather
     * than {@link Math#round} because it breaks ties to even, which is what Python's
     * {@code round()} does, so the two implementations agree even at an exact .5.
     */
    private long nextTimestamp() {
        long ts;
        if (!tsQueue.isEmpty()) {
            ts = tsQueue.pollFirst();
            synthNext = ts + 1000.0 / defaultFps;
        } else {
            ts = (long) Math.rint(synthNext);
            synthNext += 1000.0 / defaultFps;
        }
        return ts;
    }
}
