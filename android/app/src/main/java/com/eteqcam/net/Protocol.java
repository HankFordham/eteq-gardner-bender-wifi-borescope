package com.eteqcam.net;

import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * Wire format of the OV780 WiFi camera protocol (eTEQ / Gardner Bender WIC-100).
 *
 * <p>Three layers sit on one UDP socket to the camera's port 1000:
 *
 * <ol>
 *   <li>A 4-byte transport header {@code [type][seq][ack]['v']} providing ordering,
 *       acknowledgement and retransmission (see {@link Transport}).
 *   <li>A text message {@code "0010" + <4-char code> + <6 hex digit length> + body}.
 *   <li>A body made of length-prefixed items {@code %02x%06x%s%s} =
 *       (key length, value length, key, value).
 * </ol>
 *
 * <p>Everything in this class is pure parsing and formatting, with no I/O, so it is
 * cheap to unit test off-device. This is a faithful port of {@code eteq/protocol.py};
 * see {@code docs/PROTOCOL.md} for how the format was derived and which parts are
 * confirmed against real hardware.
 *
 * <p>Plain Java 17, standard library only. Nothing in this package imports
 * {@code android.*}, deliberately: the protocol has to be testable without a device.
 */
public final class Protocol {

    private Protocol() {
    }

    // --- transport -----------------------------------------------------------

    /** UDP port on the camera. From {@code access_open(ctx, ip, 0x3e8)} in the vendor SDK. */
    public static final int CAM_PORT = 1000;

    /** UDP port the camera broadcasts its presence to, once per second. */
    public static final int BEACON_PORT = 2000;

    /** Fourth header byte of every transport packet, the ASCII letter 'v'. */
    public static final int PKT_MAGIC = 0x76;

    public static final int PKT_DATA = 0, PKT_ACK = 1, PKT_NACK = 2;

    /** Receive window. The camera keeps 32 slots indexed by {@code seq & 0x1f}. */
    public static final int WINDOW = 32;

    /** 1028 bytes: 4 header bytes plus at most 1024 of payload. */
    public static final int MAX_DATAGRAM = 0x404;

    public static final byte[] BEACON_MAGIC = {'8', '7', '1', '3'};
    public static final int BEACON_SIZE = 32;

    // --- messages ------------------------------------------------------------

    public static final byte[] MSG_PREFIX = {'0', '0', '1', '0'};

    /**
     * Retransmitted messages arrive with the prefix zeroed. Observed on real hardware:
     * when the camera resends a data packet the {@code 0010} prefix is replaced by four
     * zero bytes and everything after it is identical. The vendor's own parser rejects
     * that form, which is why the phone app silently restarts its stream every so often.
     * {@link #parse} accepts both.
     */
    public static final byte[] ZERO_PREFIX = {0, 0, 0, 0};

    public static final String CODE_SET = "0006";
    public static final String CODE_SET_ACK = "0007";
    public static final String CODE_GET = "0008";
    public static final String CODE_GET_ACK = "0009";
    public static final String CODE_STREAM = "0011";
    public static final String CODE_USR = "0015";
    public static final String CODE_USR_ACK = "0016";

    /** The vendor parser gives up past this many items in one body. */
    public static final int MAX_ITEMS = 16;

    /**
     * Raw user command the phone app sends once per second.
     *
     * <p>The camera answers {@code 05000001Video0} normally and {@code 05000001Video1}
     * when its physical snapshot button has been pressed.
     */
    public static final byte[] HEARTBEAT_UDC = ascii("0C000000GetSnapPhoto");

    public static final byte[] SNAPSHOT_PRESSED = ascii("05000001Video1");

    /** Parameter table from the vendor library, in its original index order. */
    public static final String[] PARAM_NAMES = {
        "Video", "Audio", "FrameSize", "FrameRate", "BitRate", "Zoom", "Brightness",
        "Contrast", "Saturation", "FlipMirror", "LightCond", "LightFreq", "AlertMode",
        "AudioAlertV", "Infrared",
    };

    public static final int STREAM_TYPE_KEYFRAME = 0;
    public static final int STREAM_TYPE_INTER = 2;
    public static final int STREAM_TYPE_AUDIO = 3;

    // --- item and message encoding -------------------------------------------

    /**
     * Encode one body item as {@code %02x%06x%s%s}.
     *
     * <p>Values are raw bytes: {@code Info}, {@code Data} and {@code AllInfo} are binary,
     * so a body is not NUL-safe text, only length-delimited.
     */
    public static byte[] item(String key, byte[] value) {
        byte[] k = ascii(key);
        if (k.length > 0xFF) {
            throw new IllegalArgumentException("key too long");
        }
        byte[] header = ascii(String.format("%02x%06x", k.length, value.length));
        byte[] out = new byte[header.length + k.length + value.length];
        System.arraycopy(header, 0, out, 0, header.length);
        System.arraycopy(k, 0, out, header.length, k.length);
        System.arraycopy(value, 0, out, header.length + k.length, value.length);
        return out;
    }

    /** Encode one body item whose value is text. */
    public static byte[] item(String key, String value) {
        return item(key, ascii(value));
    }

    /**
     * Encode one body item whose value is a number.
     *
     * <p>Integers are formatted the way the vendor app does it, with {@code "%x"}:
     * lowercase hex and no padding. 2048 becomes {@code "800"}, not {@code "0800"}.
     */
    public static byte[] item(String key, long value) {
        return item(key, hex(value));
    }

    /** Lowercase hex, no padding, exactly like C's {@code "%x"}. */
    public static String hex(long value) {
        return Long.toHexString(value);
    }

    /** Encode several items in order. Each pair is {@code {key, value}}. */
    public static byte[] items(List<String[]> pairs) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        for (String[] pair : pairs) {
            byte[] enc = item(pair[0], pair[1]);
            out.write(enc, 0, enc.length);
        }
        return out.toByteArray();
    }

    /** Wrap a body in the {@code 0010} message envelope. */
    public static byte[] message(String code, byte[] body) {
        byte[] c = ascii(code);
        if (c.length != 4) {
            throw new IllegalArgumentException("code must be 4 characters");
        }
        byte[] len = ascii(String.format("%06x", body.length));
        byte[] out = new byte[4 + 4 + 6 + body.length];
        System.arraycopy(MSG_PREFIX, 0, out, 0, 4);
        System.arraycopy(c, 0, out, 4, 4);
        System.arraycopy(len, 0, out, 8, 6);
        System.arraycopy(body, 0, out, 14, body.length);
        return out;
    }

    // --- command builders ----------------------------------------------------

    /**
     * {@code GET AllInfo}. The phone app sends this first.
     *
     * <p>Real hardware answers with 700 bytes that are almost entirely zero, so it
     * carries no information, but it is a cheap round-trip that proves the camera
     * is listening.
     */
    public static byte[] buildGetAllInfo() {
        return message(CODE_GET, item("AllInfo", 1L));
    }

    /** A {@code SET} message. Sending {@code Video=1} is what starts the stream. */
    public static byte[] buildSet(List<String[]> pairs) {
        return message(CODE_SET, items(pairs));
    }

    /** A raw user command (code 0015), used for the heartbeat and for WiFi setup. */
    public static byte[] buildUserCommand(byte[] raw) {
        return message(CODE_USR, raw);
    }

    /** {@code Video=0}. The phone app has no stop command and just closes its socket. */
    public static byte[] buildStop() {
        List<String[]> pairs = new ArrayList<>();
        pairs.add(new String[] {"Video", "0"});
        return buildSet(pairs);
    }

    /** {@code FrameSize} packs the dimensions into one integer as {@code (w << 16) | h}. */
    public static int frameSizeValue(int w, int h) {
        return (w << 16) | h;
    }

    /** Inverse of {@link #frameSizeValue}: returns {@code {width, height}}. */
    public static int[] parseFrameSize(int value) {
        return new int[] {(value >>> 16) & 0xFFFF, value & 0xFFFF};
    }

    // --- message parsing -----------------------------------------------------

    /** A decoded camera message. */
    public static final class Message {
        public final String code;
        public final byte[] body;
        /** Each entry is {@code {key, value}}; empty for a {@code 0016} user reply. */
        public final List<byte[][]> items;

        public Message(String code, byte[] body, List<byte[][]> items) {
            this.code = code;
            this.body = body;
            this.items = items;
        }

        /** The value of one item, or null when absent. */
        public byte[] get(String key) {
            byte[] want = ascii(key);
            for (byte[][] kv : items) {
                if (Arrays.equals(kv[0], want)) {
                    return kv[1];
                }
            }
            return null;
        }

        /** The value of one item as text, or null when absent. */
        public String getText(String key) {
            byte[] v = get(key);
            return v == null ? null : new String(v, StandardCharsets.ISO_8859_1);
        }
    }

    /**
     * Decode one message payload, or return null if it is not one.
     *
     * <p>Accepts both the normal {@code 0010} prefix and the zeroed prefix the camera
     * uses when it retransmits. See {@link #ZERO_PREFIX}: rejecting the zeroed form was
     * one of the two client bugs that used to kill the stream within a couple of minutes.
     */
    public static Message parse(byte[] payload) {
        if (payload == null || payload.length < 14) {
            return null;
        }
        if (!startsWith(payload, MSG_PREFIX) && !startsWith(payload, ZERO_PREFIX)) {
            return null;
        }
        String code = new String(payload, 4, 4, StandardCharsets.ISO_8859_1);
        int blen = parseHex(payload, 8, 6);
        if (blen < 0) {
            return null;
        }
        int end = Math.max(14, Math.min(payload.length, 14 + blen));
        byte[] body = Arrays.copyOfRange(payload, 14, end);
        // A 0016 user reply carries raw bytes, not items; the vendor parser does not
        // try to split it either.
        List<byte[][]> parsed =
            CODE_USR_ACK.equals(code) ? new ArrayList<>() : parseItems(body, MAX_ITEMS);
        return new Message(code, body, parsed);
    }

    /** Split a body into (key, value) pairs, stopping at the first malformed one. */
    public static List<byte[][]> parseItems(byte[] body, int limit) {
        List<byte[][]> out = new ArrayList<>();
        int pos = 0;
        while (pos + 8 <= body.length && out.size() < limit) {
            int klen = parseHex(body, pos, 2);
            int vlen = parseHex(body, pos + 2, 6);
            if (klen < 0 || vlen < 0) {
                break;
            }
            int start = pos + 8;
            if (start + klen > body.length || start + klen + vlen > body.length) {
                break;
            }
            byte[] key = Arrays.copyOfRange(body, start, start + klen);
            byte[] val = Arrays.copyOfRange(body, start + klen, start + klen + vlen);
            out.add(new byte[][] {key, val});
            pos = start + klen + vlen;
        }
        return out;
    }

    // --- stream chunks -------------------------------------------------------

    /**
     * The 28-byte {@code Info} item carried by every {@code 0011} chunk: seven
     * big-endian uint32. Field meanings were read off real hardware; see
     * {@code docs/PROTOCOL.md} section 3.3.
     */
    public static final class StreamInfo {
        /** 0 for an I-frame, 2 for a P-frame, 3 for audio. */
        public final int frameType;
        /** Total size of the frame, present only in its first chunk. */
        public final int frameBytes;
        /** Rolling counter, increments roughly once per frame. */
        public final int counter;
        /** Index of this chunk inside the frame. 0 is the first. */
        public final int chunkIndex;
        /** Always 0 on the reference camera. */
        public final int reserved4;
        /** Millisecond presentation clock, restarts each session. */
        public final int timestampMs;
        /** Always 0 on the reference camera. */
        public final int reserved6;

        public StreamInfo(int frameType, int frameBytes, int counter, int chunkIndex,
                          int reserved4, int timestampMs, int reserved6) {
            this.frameType = frameType;
            this.frameBytes = frameBytes;
            this.counter = counter;
            this.chunkIndex = chunkIndex;
            this.reserved4 = reserved4;
            this.timestampMs = timestampMs;
            this.reserved6 = reserved6;
        }

        public boolean isKeyframe() {
            return frameType == STREAM_TYPE_KEYFRAME;
        }

        public boolean isAudio() {
            return frameType == STREAM_TYPE_AUDIO;
        }

        public boolean startsFrame() {
            return chunkIndex == 0;
        }

        /** Null if shorter than 28 bytes. */
        public static StreamInfo parse(byte[] raw) {
            if (raw == null || raw.length < 28) {
                return null;
            }
            return new StreamInfo(be32(raw, 0), be32(raw, 4), be32(raw, 8), be32(raw, 12),
                                  be32(raw, 16), be32(raw, 20), be32(raw, 24));
        }

        @Override
        public String toString() {
            return "[" + frameType + ", " + frameBytes + ", " + counter + ", " + chunkIndex
                   + ", " + reserved4 + ", " + timestampMs + ", " + reserved6 + "]";
        }
    }

    /** One {@code 0011} media message. */
    public static final class StreamChunk {
        public final String mediaType;
        public final StreamInfo info;
        public final byte[] data;

        public StreamChunk(String mediaType, StreamInfo info, byte[] data) {
            this.mediaType = mediaType;
            this.info = info;
            this.data = data;
        }

        public boolean isAudio() {
            if ("Audio".equals(mediaType)) {
                return true;
            }
            return info != null && info.isAudio();
        }
    }

    /** Pull the media payload out of a {@code 0011} message, or null if it is not one. */
    public static StreamChunk parseStreamChunk(Message m) {
        if (m == null || !CODE_STREAM.equals(m.code)) {
            return null;
        }
        String type = m.getText("Type");
        if (type == null) {
            type = "Video";
        }
        byte[] rawInfo = m.get("Info");
        byte[] data = m.get("Data");
        return new StreamChunk(type, rawInfo == null ? null : StreamInfo.parse(rawInfo),
                               data == null ? new byte[0] : data);
    }

    // --- helpers -------------------------------------------------------------

    /** Compact hex plus ASCII, for logs that get compared against Wireshark. */
    public static String hexdump(byte[] data, int maxLen) {
        int n = Math.min(data.length, Math.max(0, maxLen));
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < n; i += 16) {
            int end = Math.min(n, i + 16);
            StringBuilder hx = new StringBuilder();
            StringBuilder asc = new StringBuilder();
            for (int j = i; j < end; j++) {
                int b = data[j] & 0xFF;
                if (j > i) {
                    hx.append(' ');
                }
                hx.append(String.format("%02x", b));
                asc.append(b >= 32 && b < 127 ? (char) b : '.');
            }
            if (sb.length() > 0) {
                sb.append('\n');
            }
            sb.append(String.format("  %04x  %-48s  %s", i, hx, asc));
        }
        return sb.toString();
    }

    /**
     * Circular comparison mod 256: is {@code b} in {@code [a, c)}?
     *
     * <p>Named after the helper of the same purpose in the vendor library.
     */
    public static boolean between(int a, int b, int c) {
        return ((b - a) & 0xFF) < ((c - a) & 0xFF);
    }

    static byte[] ascii(String s) {
        return s.getBytes(StandardCharsets.ISO_8859_1);
    }

    static boolean startsWith(byte[] data, byte[] prefix) {
        if (data.length < prefix.length) {
            return false;
        }
        for (int i = 0; i < prefix.length; i++) {
            if (data[i] != prefix[i]) {
                return false;
            }
        }
        return true;
    }

    /** Strict hex field parse: -1 when any character is not a hex digit. */
    static int parseHex(byte[] data, int off, int len) {
        if (off < 0 || off + len > data.length) {
            return -1;
        }
        int value = 0;
        for (int i = off; i < off + len; i++) {
            int digit = Character.digit((char) (data[i] & 0xFF), 16);
            if (digit < 0) {
                return -1;
            }
            value = (value << 4) | digit;
        }
        return value;
    }

    static int be32(byte[] b, int off) {
        return ((b[off] & 0xFF) << 24) | ((b[off + 1] & 0xFF) << 16)
             | ((b[off + 2] & 0xFF) << 8) | (b[off + 3] & 0xFF);
    }
}
