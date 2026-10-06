package com.eteqcam;

import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Context;
import android.media.MediaCodec;
import android.media.MediaFormat;
import android.media.MediaMuxer;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.MediaStore;

import com.eteqcam.net.H264Framer;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * Writes the camera's frames to an MP4 in the phone's Movies folder.
 *
 * <p>Nothing is re-encoded. The camera's H.264 is placed into a container exactly
 * as it arrived, which costs almost no battery and loses no quality.
 *
 * <p>Recording starts on the first keyframe rather than the next frame, because a
 * file that begins mid-picture cannot be decoded from the start.
 */
public final class Recorder {

    private final Context context;
    private MediaMuxer muxer;
    private ParcelFileDescriptor descriptor;
    private Uri target;
    private int track = -1;
    private long firstTimestampMs = -1;
    private int frames;
    private String displayName;

    public Recorder(Context context) {
        this.context = context.getApplicationContext();
    }

    public boolean isRecording() {
        return muxer != null || target != null;
    }

    public int frameCount() {
        return frames;
    }

    public String name() {
        return displayName;
    }

    /** Create the file. Nothing is written until the first keyframe arrives. */
    public void start() throws IOException {
        if (isRecording()) {
            return;
        }
        displayName = "eteq-" + new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(new Date()) + ".mp4";

        ContentValues values = new ContentValues();
        values.put(MediaStore.MediaColumns.DISPLAY_NAME, displayName);
        values.put(MediaStore.MediaColumns.MIME_TYPE, "video/mp4");
        values.put(MediaStore.MediaColumns.RELATIVE_PATH, "Movies/eteq");
        values.put(MediaStore.MediaColumns.IS_PENDING, 1);

        ContentResolver resolver = context.getContentResolver();
        target = resolver.insert(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, values);
        if (target == null) {
            throw new IOException("could not create a file in Movies");
        }
        descriptor = resolver.openFileDescriptor(target, "rw");
        if (descriptor == null) {
            throw new IOException("could not open the new file for writing");
        }
        frames = 0;
        firstTimestampMs = -1;
        track = -1;
    }

    /** Offer a frame. Ignored until the first keyframe. */
    public void write(H264Framer.Frame frame) {
        if (target == null) {
            return;
        }
        try {
            if (muxer == null) {
                if (!frame.keyframe) {
                    return;
                }
                byte[][] parameterSets = VideoDecoder.spsPps(frame.data);
                if (parameterSets == null) {
                    return;
                }
                muxer = new MediaMuxer(descriptor.getFileDescriptor(),
                        MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4);
                MediaFormat format = MediaFormat.createVideoFormat("video/avc", 640, 240);
                format.setByteBuffer("csd-0", ByteBuffer.wrap(parameterSets[0]));
                format.setByteBuffer("csd-1", ByteBuffer.wrap(parameterSets[1]));
                format.setInteger(MediaFormat.KEY_FRAME_RATE, 30);
                track = muxer.addTrack(format);
                muxer.start();
                firstTimestampMs = frame.timestampMs;
            }

            MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
            info.offset = 0;
            info.size = frame.data.length;
            info.presentationTimeUs = Math.max(0, frame.timestampMs - firstTimestampMs) * 1000L;
            info.flags = frame.keyframe ? MediaCodec.BUFFER_FLAG_KEY_FRAME : 0;
            muxer.writeSampleData(track, ByteBuffer.wrap(frame.data), info);
            frames++;
        } catch (Exception e) {
            // A failed recording must never take the live picture down with it.
            abandon();
        }
    }

    /** Finish the file and publish it. Returns its name, or null if nothing was written. */
    public String stop() {
        String finished = frames > 0 ? displayName : null;
        try {
            if (muxer != null) {
                muxer.stop();
                muxer.release();
            }
        } catch (Exception ignored) {
            finished = null;
        }
        muxer = null;

        closeDescriptor();

        if (target != null) {
            if (finished != null) {
                ContentValues done = new ContentValues();
                done.put(MediaStore.MediaColumns.IS_PENDING, 0);
                context.getContentResolver().update(target, done, null, null);
            } else if (finished == null) {
                context.getContentResolver().delete(target, null, null);
            }
            target = null;
        }
        return finished;
    }

    private void abandon() {
        try {
            if (muxer != null) {
                muxer.release();
            }
        } catch (Exception ignored) {
            // nothing useful to do
        }
        muxer = null;
        closeDescriptor();
        if (target != null) {
            context.getContentResolver().delete(target, null, null);
            target = null;
        }
    }

    private void closeDescriptor() {
        if (descriptor != null) {
            try {
                descriptor.close();
            } catch (IOException ignored) {
                // nothing useful to do
            }
            descriptor = null;
        }
    }
}
