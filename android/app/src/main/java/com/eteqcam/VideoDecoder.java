package com.eteqcam;

import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;
import android.os.Build;
import android.view.Surface;

import com.eteqcam.net.H264Framer;

import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.TimeUnit;

/**
 * Hardware H.264 decoding straight onto the screen.
 *
 * <p>This is the reason a native app is worth building. The phone's own decoder
 * takes the camera's frames and writes them into the view's surface with no copy
 * through Java, no container, and no transcoding, which is as direct as the path
 * gets.
 *
 * <p>Two details matter for latency. Frames are rendered as soon as they come out
 * rather than being scheduled against a clock, because the newest picture is the
 * only one worth showing on a live view. And the queue between the network thread
 * and the decoder is deliberately tiny: if decoding ever falls behind, the right
 * answer is to drop old frames, not to build a backlog that shows the viewer the
 * past.
 */
public final class VideoDecoder {

    /** Status reported back to the UI. */
    public interface Listener {
        void onFirstFrame(int width, int height);

        void onError(String message);
    }

    private static final String MIME = "video/avc";
    private static final int QUEUE_DEPTH = 8;

    private final Listener listener;
    private final BlockingQueue<H264Framer.Frame> pending = new ArrayBlockingQueue<>(QUEUE_DEPTH);

    private volatile Surface surface;
    private volatile boolean running;
    private Thread worker;
    private MediaCodec codec;

    private int droppedFrames;
    private int decodedFrames;
    private boolean announced;

    public VideoDecoder(Listener listener) {
        this.listener = listener;
    }

    public void start(Surface target) {
        stop();
        this.surface = target;
        this.running = true;
        this.announced = false;
        this.decodedFrames = 0;
        this.droppedFrames = 0;
        worker = new Thread(this::run, "video-decoder");
        worker.start();
    }

    public void stop() {
        running = false;
        if (worker != null) {
            worker.interrupt();
            try {
                worker.join(1500);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
            worker = null;
        }
        pending.clear();
    }

    public int decodedFrames() {
        return decodedFrames;
    }

    public int droppedFrames() {
        return droppedFrames;
    }

    /** Hand over one access unit. Never blocks the caller. */
    public void submit(H264Framer.Frame frame) {
        if (!running) {
            return;
        }
        if (!pending.offer(frame)) {
            // Full: throw away the oldest so the viewer sees the newest.
            pending.poll();
            droppedFrames++;
            pending.offer(frame);
        }
    }

    // -- the decoding thread -------------------------------------------------

    private void run() {
        long firstTimestampMs = -1;
        try {
            while (running) {
                H264Framer.Frame frame = pending.poll(200, TimeUnit.MILLISECONDS);
                if (frame == null) {
                    continue;
                }
                if (codec == null) {
                    // Nothing can be decoded before the parameter sets arrive, and
                    // starting on a mid-stream frame shows a screen of garbage.
                    if (!frame.keyframe) {
                        continue;
                    }
                    byte[] config = parameterSets(frame.data);
                    if (config == null) {
                        continue;
                    }
                    configure(config);
                    firstTimestampMs = frame.timestampMs;
                }
                long presentationUs = Math.max(0, (frame.timestampMs - firstTimestampMs)) * 1000L;
                feed(frame.data, presentationUs);
                drain();
            }
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        } catch (Exception e) {
            if (running) {
                listener.onError("the decoder stopped: " + e);
            }
        } finally {
            releaseCodec();
        }
    }

    private void configure(byte[] parameterSets) throws Exception {
        // The real dimensions come from the parameter sets themselves; the values
        // passed here are only a hint, and the decoder corrects them.
        MediaFormat format = MediaFormat.createVideoFormat(MIME, 640, 240);
        format.setByteBuffer("csd-0", ByteBuffer.wrap(parameterSets));
        format.setInteger(MediaFormat.KEY_COLOR_FORMAT,
                MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            // Tells the decoder not to hold frames back for reordering. There are
            // no B-frames in this stream, so there is nothing to reorder anyway.
            format.setInteger(MediaFormat.KEY_LOW_LATENCY, 1);
        }
        codec = MediaCodec.createDecoderByType(MIME);
        codec.configure(format, surface, null, 0);
        codec.start();
    }

    private void feed(byte[] data, long presentationUs) {
        int index = codec.dequeueInputBuffer(100_000);
        if (index < 0) {
            droppedFrames++;
            return;
        }
        ByteBuffer input = codec.getInputBuffer(index);
        if (input == null) {
            return;
        }
        input.clear();
        if (input.capacity() < data.length) {
            codec.queueInputBuffer(index, 0, 0, presentationUs, 0);
            droppedFrames++;
            return;
        }
        input.put(data);
        codec.queueInputBuffer(index, 0, data.length, presentationUs, 0);
    }

    private void drain() {
        MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
        while (running) {
            int index = codec.dequeueOutputBuffer(info, 0);
            if (index == MediaCodec.INFO_TRY_AGAIN_LATER) {
                return;
            }
            if (index == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                MediaFormat output = codec.getOutputFormat();
                if (!announced) {
                    announced = true;
                    listener.onFirstFrame(
                            output.getInteger(MediaFormat.KEY_WIDTH),
                            output.getInteger(MediaFormat.KEY_HEIGHT));
                }
                continue;
            }
            if (index < 0) {
                return;
            }
            // true means "show it now".
            codec.releaseOutputBuffer(index, true);
            decodedFrames++;
        }
    }

    private void releaseCodec() {
        if (codec == null) {
            return;
        }
        try {
            codec.stop();
        } catch (Exception ignored) {
            // already dead
        }
        try {
            codec.release();
        } catch (Exception ignored) {
            // nothing useful to do
        }
        codec = null;
    }

    // -- parameter sets ------------------------------------------------------

    /**
     * Pull the SPS and PPS out of an access unit, as one Annex B blob.
     *
     * <p>Returns null when the unit does not carry both, which happens on every
     * frame that is not a keyframe.
     */
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
