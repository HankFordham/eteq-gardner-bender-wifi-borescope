package com.eteqcam;

import android.app.Activity;
import android.content.ContentResolver;
import android.content.ContentValues;
import android.graphics.Bitmap;
import android.graphics.SurfaceTexture;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.MediaStore;
import android.view.Surface;
import android.view.TextureView;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.TextView;
import android.widget.Toast;

import com.eteqcam.net.CameraSession;
import com.eteqcam.net.H264Framer;

import java.io.OutputStream;
import java.net.DatagramSocket;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * The whole user interface: a picture, a status line and four buttons.
 *
 * <p>The phone talks to the camera directly. There is no computer in the path and
 * nothing is transcoded, so the delay is roughly the camera's own encoding time
 * plus one WiFi hop.
 */
public class MainActivity extends Activity implements CameraSession.Listener, VideoDecoder.Listener {

    private static final float FOUR_THREE = 4f / 3f;

    private final Handler main = new Handler(Looper.getMainLooper());

    private TextureView video;
    private FrameLayout videoHolder;
    private TextView status;
    private TextView message;
    private Button connect;
    private Button snapshot;
    private Button record;
    private Button aspect;

    private CameraNetwork network;
    private VideoDecoder decoder;
    private Recorder recorder;
    private CameraSession session;
    private Thread sessionThread;

    private Surface surface;
    private String cameraIp;
    private boolean wantFourThree = true;
    private int videoWidth = 640;
    private int videoHeight = 240;
    private boolean streaming;

    private long lastStatsAt;
    private int lastFrameCount;
    private double measuredFps;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        setContentView(R.layout.activity_main);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        video = findViewById(R.id.video);
        videoHolder = findViewById(R.id.videoHolder);
        status = findViewById(R.id.status);
        message = findViewById(R.id.message);
        connect = findViewById(R.id.connect);
        snapshot = findViewById(R.id.snapshot);
        record = findViewById(R.id.record);
        aspect = findViewById(R.id.aspect);

        network = new CameraNetwork(this);
        decoder = new VideoDecoder(this);
        recorder = new Recorder(this);

        connect.setOnClickListener(v -> {
            if (streaming || sessionThread != null) {
                stopStreaming("stopped");
            } else {
                startStreaming();
            }
        });
        snapshot.setOnClickListener(v -> takeSnapshot());
        record.setOnClickListener(v -> toggleRecording());
        aspect.setOnClickListener(v -> {
            wantFourThree = !wantFourThree;
            aspect.setText(wantFourThree ? "4:3" : "native");
            applyVideoSize();
        });

        video.setSurfaceTextureListener(new TextureView.SurfaceTextureListener() {
            @Override
            public void onSurfaceTextureAvailable(SurfaceTexture texture, int width, int height) {
                surface = new Surface(texture);
                applyVideoSize();
            }

            @Override
            public void onSurfaceTextureSizeChanged(SurfaceTexture texture, int width, int height) {
                // handled by applyVideoSize
            }

            @Override
            public boolean onSurfaceTextureDestroyed(SurfaceTexture texture) {
                surface = null;
                return true;
            }

            @Override
            public void onSurfaceTextureUpdated(SurfaceTexture texture) {
                // nothing to do per frame
            }
        });

        videoHolder.addOnLayoutChangeListener(
                (v, l, t, r, b, ol, ot, or, ob) -> applyVideoSize());
        main.post(this::tick);
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        stopStreaming(null);
        network.release();
    }

    // -- connecting ----------------------------------------------------------

    private void startStreaming() {
        connect.setEnabled(false);
        connect.setText("Connecting");
        message.setVisibility(View.VISIBLE);
        message.setText("Looking for the camera's WiFi…");

        network.acquire(new CameraNetwork.Callback() {
            @Override
            public void onAvailable(String gatewayIp) {
                cameraIp = gatewayIp != null ? gatewayIp : "192.168.2.103";
                message.setText("Found the network. Asking " + cameraIp + " for pictures…");
                beginSession();
            }

            @Override
            public void onLost(String reason) {
                stopStreaming(null);
                message.setVisibility(View.VISIBLE);
                message.setText("Could not use the WiFi: " + reason
                        + "\n\nJoin the camera's own network, usually called WIFICAMERA,"
                        + "\nand make sure nothing else is connected to it.");
            }
        });
    }

    private void beginSession() {
        if (surface == null) {
            message.setText("Waiting for the display…");
            main.postDelayed(this::beginSession, 200);
            return;
        }
        decoder.start(surface);

        CameraSession.Settings settings = new CameraSession.Settings();
        session = new CameraSession(cameraIp, settings, this);
        session.setSocketFactory(new CameraSession.SocketFactory() {
            @Override
            public DatagramSocket create() throws java.io.IOException {
                // Bound to the camera's network, or Android sends it over mobile data.
                return network.createSocket(0);
            }
        });
        sessionThread = new Thread(session, "camera-session");
        sessionThread.start();

        connect.setEnabled(true);
        connect.setText("Stop");
    }

    private void stopStreaming(String note) {
        streaming = false;
        if (session != null) {
            session.stop();
            session = null;
        }
        if (sessionThread != null) {
            try {
                sessionThread.join(1500);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
            sessionThread = null;
        }
        decoder.stop();
        if (recorder.isRecording()) {
            recorder.stop();
        }
        network.release();

        connect.setEnabled(true);
        connect.setText("Connect");
        snapshot.setEnabled(false);
        record.setEnabled(false);
        record.setText("Record");
        if (note != null) {
            status.setText(note);
            message.setVisibility(View.VISIBLE);
            message.setText("Join the camera's WiFi, then press Connect.");
        }
    }

    // -- session callbacks, all on the session thread ------------------------

    @Override
    public void onFrame(H264Framer.Frame frame) {
        decoder.submit(frame);
        if (recorder.isRecording()) {
            recorder.write(frame);
        }
        if (!streaming) {
            streaming = true;
            main.post(() -> {
                message.setVisibility(View.GONE);
                snapshot.setEnabled(true);
                record.setEnabled(true);
            });
        }
    }

    @Override
    public void onState(String state) {
        main.post(() -> status.setText(state));
    }

    @Override
    public void onError(String error) {
        main.post(() -> {
            message.setVisibility(View.VISIBLE);
            message.setText(error);
        });
    }

    @Override
    public void onSnapshotButton() {
        main.post(this::takeSnapshot);
    }

    @Override
    public void onFirstFrame(int width, int height) {
        main.post(() -> {
            videoWidth = width;
            videoHeight = height;
            applyVideoSize();
        });
    }

    // -- picture -------------------------------------------------------------

    /** Size the view so the picture keeps the chosen shape inside the screen. */
    private void applyVideoSize() {
        int holderWidth = videoHolder.getWidth();
        int holderHeight = videoHolder.getHeight();
        if (holderWidth == 0 || holderHeight == 0) {
            return;
        }
        // The camera sends 640x240; its own app stretches that to 4:3, which is
        // what the lens actually sees.
        float wanted = wantFourThree ? FOUR_THREE : (float) videoWidth / Math.max(1, videoHeight);
        int width = holderWidth;
        int height = Math.round(width / wanted);
        if (height > holderHeight) {
            height = holderHeight;
            width = Math.round(height * wanted);
        }
        FrameLayout.LayoutParams params = new FrameLayout.LayoutParams(width, height);
        params.gravity = android.view.Gravity.CENTER;
        video.setLayoutParams(params);
    }

    private void takeSnapshot() {
        Bitmap frame = video.getBitmap();
        if (frame == null) {
            toast("No picture to save yet");
            return;
        }
        String name = "eteq-" + new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(new Date()) + ".png";
        ContentValues values = new ContentValues();
        values.put(MediaStore.MediaColumns.DISPLAY_NAME, name);
        values.put(MediaStore.MediaColumns.MIME_TYPE, "image/png");
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            values.put(MediaStore.MediaColumns.RELATIVE_PATH, "Pictures/eteq");
        }
        ContentResolver resolver = getContentResolver();
        Uri uri = resolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values);
        if (uri == null) {
            toast("Could not save the photo");
            return;
        }
        try (OutputStream out = resolver.openOutputStream(uri)) {
            frame.compress(Bitmap.CompressFormat.PNG, 100, out);
            toast("Saved " + name);
        } catch (Exception e) {
            resolver.delete(uri, null, null);
            toast("Could not save the photo: " + e.getMessage());
        }
    }

    private void toggleRecording() {
        if (recorder.isRecording()) {
            String name = recorder.stop();
            record.setText("Record");
            toast(name != null ? "Saved " + name : "Nothing was recorded");
            return;
        }
        try {
            recorder.start();
            record.setText("Stop rec");
            toast("Recording to Movies/eteq");
        } catch (Exception e) {
            toast("Could not start recording: " + e.getMessage());
        }
    }

    // -- status line ---------------------------------------------------------

    private void tick() {
        if (session != null) {
            long now = System.currentTimeMillis();
            if (lastStatsAt > 0 && now > lastStatsAt) {
                double seconds = (now - lastStatsAt) / 1000.0;
                measuredFps = (session.videoFrames - lastFrameCount) / seconds;
            }
            lastStatsAt = now;
            lastFrameCount = session.videoFrames;

            String line = String.format(Locale.US,
                    "%s  %dx%d  %.1f fps  %d frames%s%s",
                    cameraIp == null ? "camera" : cameraIp,
                    videoWidth, videoHeight, measuredFps, session.videoFrames,
                    decoder.droppedFrames() > 0 ? "  " + decoder.droppedFrames() + " dropped" : "",
                    recorder.isRecording() ? "  REC " + recorder.frameCount() : "");
            status.setText(line);
        }
        main.postDelayed(this::tick, 1000);
    }

    private void toast(String text) {
        Toast.makeText(this, text, Toast.LENGTH_SHORT).show();
    }
}
