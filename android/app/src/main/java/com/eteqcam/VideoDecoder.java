package com.eteqcam;

import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaCodecList;
import android.media.MediaFormat;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.view.Surface;

import com.eteqcam.net.H264Framer;

import java.nio.ByteBuffer;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Hardware H.264 decoding straight onto the screen, in asynchronous mode.
 *
 * <p>The obvious way to drive MediaCodec is to ask for an input buffer, fill it,
 * then look for output. That is also a good way to build a stutter: asking for an
 * input buffer blocks, and while it blocks nothing is collecting the decoder's
 * output, so the decoder runs out of free buffers and has nothing to hand back,
 * and each frame costs the full timeout. The picture then lags further and further
 * behind.
 *
 * <p>So this uses callbacks instead. Output is released the instant it exists, and
 * input is filled the instant the decoder offers a buffer. Nothing blocks.
 *
 * <p>The queue between the network and the decoder is deliberately tiny. On a live
 * view the newest picture is the only one worth having, so when it fills the
 * oldest frame is thrown away rather than adding delay.
 */
public final class VideoDecoder {

    public interface Listener {
        void onFirstFrame(int width, int height);

        void onError(String message);
    }

    private static final String MIME = "video/avc";

    /** Three frames, about a tenth of a second. Beyond that, delay is worse than loss. */
    private static final int MAX_WAITING = 3;

    private final Listener listener;
    private final Object lock = new Object();

    private final ArrayDeque<Integer> freeInputs = new ArrayDeque<>();
    private final ArrayDeque<H264Framer.Frame> waiting = new ArrayDeque<>();
    private final Map<Long, Long> submittedAtNanos = new HashMap<>();

    private MediaCodec codec;
    private HandlerThread callbackThread;
    private Surface surface;
    private boolean running;
    private boolean announced;
    private long firstTimestampMs = -1;

    private volatile int decodedFrames;
    private volatile int droppedFrames;
    private volatile long lastLatencyMs;
    private volatile String decoderName = "";

    /** Smooth playback at the camera's own cadence, rather than as frames land. */
    private volatile boolean paced = true;

    /** How far ahead of the newest frame the paced clock aims to sit. */
    private static final long LEAD_NANOS = 30_000_000L;

    private long clockBaseNanos;
    private long clockBasePtsUs = -1;

    public VideoDecoder(Listener listener) {
        this.listener = listener;
    }

    public void start(Surface target) {
        stop();
        synchronized (lock) {
            surface = target;
            running = true;
            announced = false;
            decodedFrames = 0;
            droppedFrames = 0;
            lastLatencyMs = 0;
            firstTimestampMs = -1;
        }
    }

    public void stop() {
        MediaCodec doomed;
        HandlerThread thread;
        synchronized (lock) {
            running = false;
            doomed = codec;
            codec = null;
            thread = callbackThread;
            callbackThread = null;
            freeInputs.clear();
            waiting.clear();
            submittedAtNanos.clear();
        }
        if (doomed != null) {
            try {
                doomed.stop();
            } catch (Exception ignored) {
                // already gone
            }
            try {
                doomed.release();
            } catch (Exception ignored) {
                // nothing useful to do
            }
        }
        if (thread != null) {
            thread.quitSafely();
        }
    }

    public int decodedFrames() {
        return decodedFrames;
    }

    public int droppedFrames() {
        return droppedFrames;
    }

    /** Milliseconds between handing a frame to the decoder and getting it back. */
    public long lastLatencyMs() {
        return lastLatencyMs;
    }

    /** Which decoder the system gave us, useful when a device behaves oddly. */
    public String decoderName() {
        return decoderName;
    }

    /**
     * Prefer a hardware decoder that advertises low latency.
     *
     * <p>Decoders normally pipeline two or three frames deep, which at thirty
     * frames a second is most of a tenth of a second of delay before anything
     * reaches the screen. Some expose a mode that shortens the pipeline, and
     * asking for one by name is more reliable than hoping the default honours the
     * low-latency flag.
     *
     * @return a codec name, or null to let the system choose
     */
    private static String preferredDecoder() {
        try {
            MediaCodecList list = new MediaCodecList(MediaCodecList.REGULAR_CODECS);
            String hardware = null;
            for (MediaCodecInfo info : list.getCodecInfos()) {
                if (info.isEncoder()) {
                    continue;
                }
                boolean handlesAvc = false;
                for (String type : info.getSupportedTypes()) {
                    if (MIME.equalsIgnoreCase(type)) {
                        handlesAvc = true;
                        break;
                    }
                }
                if (!handlesAvc) {
                    continue;
                }
                if (!info.isHardwareAccelerated()) {
                    continue;
                }
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                    MediaCodecInfo.CodecCapabilities caps = info.getCapabilitiesForType(MIME);
                    if (caps != null && caps.isFeatureSupported(
                            MediaCodecInfo.CodecCapabilities.FEATURE_LowLatency)) {
                        return info.getName();
                    }
                }
                if (hardware == null) {
                    hardware = info.getName();
                }
            }
            return hardware;
        } catch (Exception e) {
            return null;
        }
    }

    /** Hand over one access unit. Never blocks the caller. */
    public void submit(H264Framer.Frame frame) {
        synchronized (lock) {
            if (!running) {
                return;
            }
            if (codec == null) {
                // Nothing decodes before the parameter sets arrive, and starting
                // mid-picture shows a screen of rubbish.
                if (!frame.keyframe) {
                    return;
                }
                byte[] parameterSets = parameterSets(frame.data);
                if (parameterSets == null) {
                    return;
                }
                try {
                    configure(parameterSets);
                } catch (Exception e) {
                    listener.onError("could not start the decoder: " + e);
                    return;
                }
                firstTimestampMs = frame.timestampMs;
            }

            Integer index = freeInputs.poll();
            if (index != null) {
                fill(index, frame);
                return;
            }
            if (waiting.size() >= MAX_WAITING) {
                waiting.poll();
                droppedFrames++;
            }
            waiting.add(frame);
        }
    }

    // -- internals, all called with the lock held or from the callback thread --

    private void configure(byte[] parameterSets) throws Exception {
        // The real dimensions come from the parameter sets; these are only a hint.
        MediaFormat format = MediaFormat.createVideoFormat(MIME, 640, 240);
        format.setByteBuffer("csd-0", ByteBuffer.wrap(parameterSets));
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            // Do not hold frames back for reordering. There are no B-frames here.
            format.setInteger(MediaFormat.KEY_LOW_LATENCY, 1);
        }
        // Realtime, not throughput: decode each frame as it comes rather than
        // batching for efficiency.
        format.setInteger(MediaFormat.KEY_PRIORITY, 0);
        format.setInteger(MediaFormat.KEY_OPERATING_RATE, Short.MAX_VALUE);
        // Vendor spellings of the same idea. An unknown key is ignored, so it is
        // safe to offer several and let the device take whichever it knows.
        format.setInteger("vendor.qti-ext-dec-low-latency.enable", 1);
        format.setInteger("vendor.low-latency.enable", 1);
        format.setInteger("low-latency", 1);

        callbackThread = new HandlerThread("decoder-callbacks",
                android.os.Process.THREAD_PRIORITY_URGENT_DISPLAY);
        callbackThread.start();

        String name = preferredDecoder();
        codec = name != null ? MediaCodec.createByCodecName(name) : MediaCodec.createDecoderByType(MIME);
        decoderName = codec.getName();
        codec.setCallback(new MediaCodec.Callback() {
            @Override
            public void onInputBufferAvailable(MediaCodec mc, int index) {
                synchronized (lock) {
                    if (!running) {
                        return;
                    }
                    H264Framer.Frame next = waiting.poll();
                    if (next != null) {
                        fill(index, next);
                    } else {
                        freeInputs.add(index);
                    }
                }
            }

            @Override
            public void onOutputBufferAvailable(MediaCodec mc, int index, MediaCodec.BufferInfo info) {
                try {
                    if (paced) {
                        mc.releaseOutputBuffer(index, renderTimeFor(info.presentationTimeUs));
                    } else {
                        mc.releaseOutputBuffer(index, true);
                    }
                } catch (Exception ignored) {
                    return;
                }
                decodedFrames++;
                synchronized (lock) {
                    Long sentAt = submittedAtNanos.remove(info.presentationTimeUs);
                    if (sentAt != null) {
                        lastLatencyMs = (System.nanoTime() - sentAt) / 1_000_000L;
                    }
                    if (submittedAtNanos.size() > 64) {
                        submittedAtNanos.clear();
                    }
                }
            }

            @Override
            public void onOutputFormatChanged(MediaCodec mc, MediaFormat format) {
                if (announced) {
                    return;
                }
                announced = true;
                listener.onFirstFrame(
                        format.getInteger(MediaFormat.KEY_WIDTH),
                        format.getInteger(MediaFormat.KEY_HEIGHT));
            }

            @Override
            public void onError(MediaCodec mc, MediaCodec.CodecException e) {
                listener.onError("the decoder failed: " + e.getDiagnosticInfo());
            }
        }, new Handler(callbackThread.getLooper()));

        codec.configure(format, surface, null, 0);
        codec.start();

        // Some devices only accept the request once running rather than at
        // configure time, so ask again. Unknown keys are ignored.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            try {
                android.os.Bundle runtime = new android.os.Bundle();
                runtime.putInt(MediaFormat.KEY_LOW_LATENCY, 1);
                runtime.putInt("vendor.qti-ext-dec-low-latency.enable", 1);
                codec.setParameters(runtime);
            } catch (Exception ignored) {
                // the decoder is simply not interested
            }
        }
    }

    /** Whether to display at the camera's cadence or the instant each frame decodes. */
    public void setPaced(boolean value) {
        paced = value;
        synchronized (lock) {
            clockBasePtsUs = -1;
        }
    }

    public boolean isPaced() {
        return paced;
    }

    /**
     * When this frame should appear, on the phone's own clock.
     *
     * <p>Frames arrive in bursts: a picture is eight packets that land together,
     * then a gap. Showing each one the moment it decodes therefore reproduces the
     * network's jitter as visible unevenness. Scheduling them at the spacing the
     * camera recorded, a fraction of a second behind the newest, makes the motion
     * even. The cost is that fraction of a second, which is why it can be turned
     * off.
     *
     * <p>The two clocks drift, so the base is nudged gently towards where it should
     * be and reset outright if it ever ends up far away.
     */
    private long renderTimeFor(long presentationTimeUs) {
        long now = System.nanoTime();
        synchronized (lock) {
            if (clockBasePtsUs < 0) {
                clockBasePtsUs = presentationTimeUs;
                clockBaseNanos = now + LEAD_NANOS;
            }
            long target = clockBaseNanos + (presentationTimeUs - clockBasePtsUs) * 1000L;
            long ahead = target - now;
            if (ahead < 0 || ahead > LEAD_NANOS * 6) {
                // Lost the thread of it: start the clock again from here.
                clockBasePtsUs = presentationTimeUs;
                clockBaseNanos = now + LEAD_NANOS;
                return clockBaseNanos;
            }
            // Ease towards the intended lead instead of correcting in one jump,
            // which would itself be visible.
            clockBaseNanos += (LEAD_NANOS - ahead) / 16;
            return target;
        }
    }

    private void fill(int index, H264Framer.Frame frame) {
        MediaCodec target = codec;
        if (target == null) {
            return;
        }
        try {
            ByteBuffer input = target.getInputBuffer(index);
            if (input == null) {
                return;
            }
            input.clear();
            if (input.capacity() < frame.data.length) {
                target.queueInputBuffer(index, 0, 0, 0, 0);
                droppedFrames++;
                return;
            }
            input.put(frame.data);
            long presentationUs = Math.max(0, frame.timestampMs - firstTimestampMs) * 1000L;
            submittedAtNanos.put(presentationUs, System.nanoTime());
            target.queueInputBuffer(index, 0, frame.data.length, presentationUs, 0);
        } catch (Exception e) {
            droppedFrames++;
        }
    }

    // -- parameter sets ------------------------------------------------------

    /** The SPS and PPS of an access unit joined together, or null if it has neither. */
    static byte[] parameterSets(byte[] annexB) {
        byte[][] both = spsPps(annexB);
        if (both == null) {
            return null;
        }
        byte[] out = new byte[both[0].length + both[1].length];
        System.arraycopy(both[0], 0, out, 0, both[0].length);
        System.arraycopy(both[1], 0, out, both[0].length, both[1].length);
        return out;
    }

    /**
     * The SPS and PPS of an access unit, each with a four-byte start code.
     *
     * <p>Returns {sps, pps}, or null when the unit carries neither, which is every
     * frame that is not a keyframe. The recorder needs them separately; the
     * decoder takes them joined.
     */
    static byte[][] spsPps(byte[] annexB) {
        byte[] sps = null;
        byte[] pps = null;
        for (int[] nal : splitNals(annexB)) {
            int type = annexB[nal[0]] & 0x1F;
            if (type == 7 && sps == null) {
                sps = withStartCode(annexB, nal[0], nal[1]);
            } else if (type == 8 && pps == null) {
                pps = withStartCode(annexB, nal[0], nal[1]);
            }
        }
        return (sps == null || pps == null) ? null : new byte[][]{sps, pps};
    }

    private static byte[] withStartCode(byte[] data, int from, int to) {
        byte[] out = new byte[4 + (to - from)];
        out[3] = 1;
        System.arraycopy(data, from, out, 4, to - from);
        return out;
    }

    /** Payload ranges of each NAL unit, start codes excluded. */
    private static List<int[]> splitNals(byte[] data) {
        List<int[]> out = new ArrayList<>();
        int i = 0;
        int previous = -1;
        while (i + 2 < data.length) {
            if (data[i] == 0 && data[i + 1] == 0 && data[i + 2] == 1) {
                int payload = i + 3;
                if (previous >= 0) {
                    int end = i;
                    if (end > previous && data[end - 1] == 0) {
                        end--;  // the three-byte code may have had a leading zero
                    }
                    out.add(new int[]{previous, end});
                }
                previous = payload;
                i = payload;
            } else {
                i++;
            }
        }
        if (previous >= 0 && previous < data.length) {
            out.add(new int[]{previous, data.length});
        }
        return out;
    }
}
