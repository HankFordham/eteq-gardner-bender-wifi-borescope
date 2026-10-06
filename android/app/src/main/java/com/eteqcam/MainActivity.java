package com.eteqcam;

import android.app.Activity;
import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.MediaStore;
import android.view.GestureDetector;
import android.view.HapticFeedbackConstants;
import android.view.MotionEvent;
import android.view.PixelCopy;
import android.view.ScaleGestureDetector;
import android.view.Surface;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.view.WindowManager;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;

import androidx.constraintlayout.widget.ConstraintLayout;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.core.view.WindowInsetsControllerCompat;

import com.eteqcam.net.CameraSession;
import com.eteqcam.net.H264Framer;
import com.google.android.material.bottomsheet.BottomSheetDialog;
import com.google.android.material.button.MaterialButton;
import com.google.android.material.button.MaterialButtonToggleGroup;
import com.google.android.material.materialswitch.MaterialSwitch;
import com.google.android.material.snackbar.Snackbar;

import java.io.OutputStream;
import java.net.DatagramSocket;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;

/**
 * The whole interface: a picture, a few pills of status and a row of controls
 * that get out of the way.
 *
 * <p>The phone talks to the camera directly. There is no computer in the path and
 * nothing is transcoded, so the delay is roughly the camera's own encoding plus
 * one WiFi hop.
 */
public class MainActivity extends Activity implements CameraSession.Listener, VideoDecoder.Listener {

    private static final float FOUR_THREE = 4f / 3f;
    private static final long CONTROLS_LINGER_MS = 4200;
    private static final float MAX_ZOOM = 8f;

    private static final String PREFS = "eteq";
    private static final String PREF_FOUR_THREE = "fourThree";
    private static final String PREF_PACED = "paced";
    private static final String PREF_SCREEN_ON = "screenOn";
    private static final String PREF_DIAGNOSTICS = "diagnostics";

    private final Handler main = new Handler(Looper.getMainLooper());

    private ConstraintLayout root;
    private SurfaceView video;
    private View flash;
    private View scrimTop;
    private View scrimBottom;
    private LinearLayout topBar;
    private LinearLayout controls;
    private LinearLayout emptyState;
    private LinearLayout recordChip;
    private View statusDot;
    private TextView statusText;
    private TextView sizePill;
    private TextView fpsPill;
    private TextView recordTime;
    private TextView diagnostics;
    private TextView emptyTitle;
    private TextView emptyBody;
    private ImageView emptyIcon;
    private MaterialButton connect;
    private MaterialButton shutter;
    private MaterialButton record;
    private MaterialButton settings;

    private SharedPreferences prefs;
    private CameraNetwork network;
    private VideoDecoder decoder;
    private Recorder recorder;
    private CameraSession session;
    private Thread sessionThread;

    private Surface surface;
    private String cameraIp;
    private int videoWidth = 640;
    private int videoHeight = 240;
    private boolean streaming;
    private boolean controlsVisible = true;

    private boolean fourThree = true;
    private boolean showDiagnostics;
    private volatile String sessionState = "";

    private float zoom = 1f;
    private float panX;
    private float panY;

    private long recordingStartedAt;
    private long lastStatsAt;
    private int lastFrameCount;
    private double measuredFps;

    private final Runnable hideControls = this::hideControls;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        setContentView(R.layout.activity_main);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);

        bindViews();
        goEdgeToEdge();
        restorePreferences();

        network = new CameraNetwork(this);
        decoder = new VideoDecoder(this);
        recorder = new Recorder(this);
        decoder.setPaced(prefs.getBoolean(PREF_PACED, true));

        wireControls();
        wireGestures();
        wireSurface();

        showControls();
        main.post(this::tick);
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        stopStreaming(false);
        network.release();
    }

    private void bindViews() {
        root = findViewById(R.id.root);
        video = findViewById(R.id.video);
        flash = findViewById(R.id.flash);
        scrimTop = findViewById(R.id.scrimTop);
        scrimBottom = findViewById(R.id.scrimBottom);
        topBar = findViewById(R.id.topBar);
        controls = findViewById(R.id.controls);
        emptyState = findViewById(R.id.emptyState);
        recordChip = findViewById(R.id.recordChip);
        statusDot = findViewById(R.id.statusDot);
        statusText = findViewById(R.id.statusText);
        sizePill = findViewById(R.id.sizePill);
        fpsPill = findViewById(R.id.fpsPill);
        recordTime = findViewById(R.id.recordTime);
        diagnostics = findViewById(R.id.diagnostics);
        emptyTitle = findViewById(R.id.emptyTitle);
        emptyBody = findViewById(R.id.emptyBody);
        emptyIcon = findViewById(R.id.emptyIcon);
        connect = findViewById(R.id.connect);
        shutter = findViewById(R.id.shutter);
        record = findViewById(R.id.record);
        settings = findViewById(R.id.settings);
    }

    /** Let the picture reach the corners, and keep the controls clear of the bars. */
    private void goEdgeToEdge() {
        WindowCompat.setDecorFitsSystemWindows(getWindow(), false);
        WindowInsetsControllerCompat bars =
                WindowCompat.getInsetsController(getWindow(), getWindow().getDecorView());
        bars.setSystemBarsBehavior(
                WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
        bars.hide(WindowInsetsCompat.Type.systemBars());

        ViewCompat.setOnApplyWindowInsetsListener(root, (v, windowInsets) -> {
            Insets safe = windowInsets.getInsets(
                    WindowInsetsCompat.Type.systemBars() | WindowInsetsCompat.Type.displayCutout());
            topBar.setPadding(topBar.getPaddingLeft(), safe.top + dp(10),
                    topBar.getPaddingRight(), 0);
            controls.setPadding(0, 0, 0, safe.bottom + dp(12));
            return windowInsets;
        });
    }

    private void restorePreferences() {
        fourThree = prefs.getBoolean(PREF_FOUR_THREE, true);
        showDiagnostics = prefs.getBoolean(PREF_DIAGNOSTICS, false);
        diagnostics.setVisibility(showDiagnostics ? View.VISIBLE : View.GONE);
        applyScreenOn(prefs.getBoolean(PREF_SCREEN_ON, true));
    }

    // -- controls ------------------------------------------------------------

    private void wireControls() {
        connect.setOnClickListener(v -> {
            tap(v);
            if (streaming || sessionThread != null) {
                stopStreaming(true);
            } else {
                startStreaming();
            }
        });
        shutter.setOnClickListener(v -> {
            tap(v);
            takeSnapshot();
        });
        record.setOnClickListener(v -> {
            tap(v);
            toggleRecording();
        });
        settings.setOnClickListener(v -> {
            tap(v);
            showSettings();
        });
    }

    private void setPaced(boolean value) {
        decoder.setPaced(value);
        prefs.edit().putBoolean(PREF_PACED, value).apply();
    }

    private void tap(View v) {
        v.performHapticFeedback(HapticFeedbackConstants.VIRTUAL_KEY);
        showControls();
    }

    private void showControls() {
        main.removeCallbacks(hideControls);
        if (!controlsVisible) {
            controlsVisible = true;
            systemBars(true);
            fade(controls, 1f);
            fade(topBar, 1f);
            fade(scrimTop, 1f);
            fade(scrimBottom, 1f);
        }
        if (streaming) {
            main.postDelayed(hideControls, CONTROLS_LINGER_MS);
        }
    }

    /** Out of the way, but only while there is actually something to look at. */
    private void hideControls() {
        if (!streaming) {
            return;
        }
        controlsVisible = false;
        systemBars(false);
        fade(controls, 0f);
        fade(topBar, 0f);
        fade(scrimTop, 0f);
        fade(scrimBottom, 0f);
    }

    private void systemBars(boolean visible) {
        WindowInsetsControllerCompat bars =
                WindowCompat.getInsetsController(getWindow(), getWindow().getDecorView());
        if (visible) {
            bars.show(WindowInsetsCompat.Type.systemBars());
        } else {
            bars.hide(WindowInsetsCompat.Type.systemBars());
        }
    }

    private void fade(View v, float to) {
        v.animate().alpha(to).setDuration(220).start();
    }

    // -- gestures ------------------------------------------------------------

    private void wireGestures() {
        ScaleGestureDetector pinch = new ScaleGestureDetector(this,
                new ScaleGestureDetector.SimpleOnScaleGestureListener() {
                    @Override
                    public boolean onScale(ScaleGestureDetector detector) {
                        setZoom(zoom * detector.getScaleFactor());
                        return true;
                    }
                });

        GestureDetector taps = new GestureDetector(this, new GestureDetector.SimpleOnGestureListener() {
            @Override
            public boolean onSingleTapConfirmed(MotionEvent e) {
                if (controlsVisible) {
                    main.removeCallbacks(hideControls);
                    hideControls();
                } else {
                    showControls();
                }
                return true;
            }

            @Override
            public boolean onDoubleTap(MotionEvent e) {
                // A quick way back to the whole picture, or in for a closer look.
                setZoom(zoom > 1.05f ? 1f : 2.5f);
                return true;
            }

            @Override
            public boolean onScroll(MotionEvent down, MotionEvent at, float dx, float dy) {
                if (zoom <= 1.01f) {
                    return false;
                }
                panX -= dx;
                panY -= dy;
                applyTransform();
                return true;
            }
        });

        root.setOnTouchListener((v, event) -> {
            pinch.onTouchEvent(event);
            if (taps.onTouchEvent(event)) {
                // A confirmed tap is a click as far as accessibility services are
                // concerned, and saying so keeps the gesture reachable by them.
                v.performClick();
            }
            return true;
        });
        root.setOnClickListener(v -> { /* handled above; present for accessibility */ });
    }

    private void setZoom(float value) {
        zoom = Math.max(1f, Math.min(MAX_ZOOM, value));
        if (zoom <= 1.01f) {
            panX = 0;
            panY = 0;
        }
        applyTransform();
    }

    /** Keep the magnified picture from being dragged off the screen entirely. */
    private void applyTransform() {
        float slackX = Math.max(0, (video.getWidth() * zoom - video.getWidth()) / 2f);
        float slackY = Math.max(0, (video.getHeight() * zoom - video.getHeight()) / 2f);
        panX = Math.max(-slackX, Math.min(slackX, panX));
        panY = Math.max(-slackY, Math.min(slackY, panY));
        video.setScaleX(zoom);
        video.setScaleY(zoom);
        video.setTranslationX(panX);
        video.setTranslationY(panY);
    }

    private void wireSurface() {
        video.getHolder().addCallback(new SurfaceHolder.Callback() {
            @Override
            public void surfaceCreated(SurfaceHolder holder) {
                surface = holder.getSurface();
                applyVideoSize();
                if (session != null) {
                    // Returning from the background: the old surface was thrown
                    // away with the decoder, so both have to be made again.
                    decoder.start(surface);
                }
            }

            @Override
            public void surfaceChanged(SurfaceHolder holder, int format, int width, int height) {
                surface = holder.getSurface();
            }

            @Override
            public void surfaceDestroyed(SurfaceHolder holder) {
                surface = null;
                decoder.stop();
            }
        });
        root.addOnLayoutChangeListener((v, l, t, r, b, ol, ot, or, ob) -> applyVideoSize());
    }

    // -- connecting ----------------------------------------------------------

    private void startStreaming() {
        connect.setEnabled(false);
        setEmptyState(R.drawable.ic_wifi_off, R.string.empty_title_looking, R.string.empty_body_looking);
        setStatus(R.color.warn, R.string.state_looking);

        network.acquire(new CameraNetwork.Callback() {
            @Override
            public void onAvailable(String gatewayIp) {
                cameraIp = gatewayIp != null ? gatewayIp : "192.168.2.103";
                setStatus(R.color.warn, R.string.state_connecting);
                beginSession();
            }

            @Override
            public void onLost(String reason) {
                stopStreaming(false);
                emptyTitle.setText(R.string.empty_title_wifi);
                emptyBody.setText(getString(R.string.empty_body_wifi, reason));
                emptyState.setVisibility(View.VISIBLE);
            }
        });
    }

    private void beginSession() {
        if (surface == null) {
            main.postDelayed(this::beginSession, 200);
            return;
        }
        decoder.start(surface);

        CameraSession.Settings cameraSettings = new CameraSession.Settings();
        session = new CameraSession(cameraIp, cameraSettings, this);
        session.setSocketFactory(() -> network.createSocket(0));
        sessionThread = new Thread(() -> {
            // Video packets arrive 150 times a second and must not wait behind
            // background work, or the unevenness shows on screen.
            android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_URGENT_DISPLAY);
            session.run();
            // The session can also end by itself, when the camera is unreachable
            // or jammed, and the screen has to follow it back to idle.
            main.post(MainActivity.this::onSessionEnded);
        }, "camera-session");
        sessionThread.start();

        connect.setEnabled(true);
        connect.setIconResource(R.drawable.ic_stop);
        connect.setContentDescription(getString(R.string.action_disconnect));
    }

    /** Called when the session stops of its own accord rather than being told to. */
    private void onSessionEnded() {
        if (session == null) {
            return;  // we asked for it; stopStreaming has already tidied up
        }
        streaming = false;
        session = null;
        sessionThread = null;
        decoder.stop();
        if (recorder.isRecording()) {
            finishRecording();
        }
        network.release();
        connect.setEnabled(true);
        connect.setIconResource(R.drawable.ic_play);
        connect.setContentDescription(getString(R.string.action_connect));
        shutter.setEnabled(false);
        record.setEnabled(false);
        sizePill.setVisibility(View.GONE);
        fpsPill.setVisibility(View.GONE);
        showControls();
    }

    private void stopStreaming(boolean announce) {
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
            finishRecording();
        }
        network.release();

        setZoom(1f);
        connect.setEnabled(true);
        connect.setIconResource(R.drawable.ic_play);
        connect.setContentDescription(getString(R.string.action_connect));
        shutter.setEnabled(false);
        record.setEnabled(false);
        setStatus(R.color.on_surface_muted, R.string.state_idle);
        sizePill.setVisibility(View.GONE);
        fpsPill.setVisibility(View.GONE);
        showControls();
        if (announce) {
            setEmptyState(R.drawable.ic_wifi_off, R.string.empty_title, R.string.empty_body);
        }
    }

    private void setEmptyState(int icon, int title, int body) {
        emptyIcon.setImageResource(icon);
        emptyTitle.setText(title);
        emptyBody.setText(body);
        emptyState.setVisibility(View.VISIBLE);
    }

    private void setStatus(int colorRes, int textRes) {
        statusDot.getBackground().mutate().setTint(getColor(colorRes));
        statusText.setText(textRes);
    }

    // -- session callbacks, on the session thread ----------------------------

    @Override
    public void onFrame(H264Framer.Frame frame) {
        decoder.submit(frame);
        if (recorder.isRecording()) {
            recorder.write(frame);
        }
        if (!streaming) {
            streaming = true;
            main.post(() -> {
                emptyState.setVisibility(View.GONE);
                shutter.setEnabled(true);
                record.setEnabled(true);
                sizePill.setVisibility(View.VISIBLE);
                fpsPill.setVisibility(View.VISIBLE);
                setStatus(R.color.live, R.string.state_live);
                showControls();
            });
        }
    }

    @Override
    public void onState(String state) {
        // The pill stays short; the session's own wording goes to diagnostics.
        sessionState = state;
    }

    @Override
    public void onError(String error) {
        main.post(() -> {
            streaming = false;
            emptyIcon.setImageResource(R.drawable.ic_wifi_off);
            emptyTitle.setText(R.string.empty_title_problem);
            emptyBody.setText(error);
            emptyState.setVisibility(View.VISIBLE);
            setStatus(R.color.danger, R.string.state_stalled);
            showControls();
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
        int holderWidth = root.getWidth();
        int holderHeight = root.getHeight();
        if (holderWidth == 0 || holderHeight == 0) {
            return;
        }
        // The camera sends 640x240; its own app stretches that to 4:3, which is
        // what the lens actually sees.
        float wanted = fourThree ? FOUR_THREE : (float) videoWidth / Math.max(1, videoHeight);
        int width = holderWidth;
        int height = Math.round(width / wanted);
        if (height > holderHeight) {
            height = holderHeight;
            width = Math.round(height * wanted);
        }
        ConstraintLayout.LayoutParams params =
                (ConstraintLayout.LayoutParams) video.getLayoutParams();
        if (params.width != width || params.height != height) {
            params.width = width;
            params.height = height;
            video.setLayoutParams(params);
        }
    }

    private void takeSnapshot() {
        if (!streaming || surface == null || !surface.isValid()) {
            say(getString(R.string.toast_no_picture));
            return;
        }
        // A SurfaceView's pixels are not in the view hierarchy, so they have to be
        // copied out of the compositor rather than read from a canvas.
        Bitmap frame = Bitmap.createBitmap(video.getWidth(), video.getHeight(), Bitmap.Config.ARGB_8888);
        PixelCopy.request(video, frame, result -> {
            if (result == PixelCopy.SUCCESS) {
                playFlash();
                saveSnapshot(frame);
            } else {
                say("Could not capture the picture");
            }
        }, main);
    }

    private void playFlash() {
        flash.setVisibility(View.VISIBLE);
        flash.setAlpha(0.85f);
        flash.animate().alpha(0f).setDuration(240)
                .withEndAction(() -> flash.setVisibility(View.INVISIBLE)).start();
        video.performHapticFeedback(HapticFeedbackConstants.CONTEXT_CLICK);
    }

    private void saveSnapshot(Bitmap frame) {
        String name = "eteq-" + stamp() + ".png";
        ContentValues values = new ContentValues();
        values.put(MediaStore.MediaColumns.DISPLAY_NAME, name);
        values.put(MediaStore.MediaColumns.MIME_TYPE, "image/png");
        values.put(MediaStore.MediaColumns.RELATIVE_PATH, "Pictures/eteq");
        ContentResolver resolver = getContentResolver();
        Uri uri = resolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values);
        if (uri == null) {
            say("Could not save the photo");
            return;
        }
        try (OutputStream out = resolver.openOutputStream(uri)) {
            frame.compress(Bitmap.CompressFormat.PNG, 100, out);
            sayWithAction(getString(R.string.toast_photo_saved), getString(R.string.action_view),
                    () -> view(uri, "image/*"));
        } catch (Exception e) {
            resolver.delete(uri, null, null);
            say("Could not save the photo");
        }
    }

    // -- recording -----------------------------------------------------------

    private void toggleRecording() {
        if (recorder.isRecording()) {
            finishRecording();
            return;
        }
        try {
            recorder.start();
            recordingStartedAt = System.currentTimeMillis();
            recordChip.setVisibility(View.VISIBLE);
            record.setIconResource(R.drawable.ic_record_stop);
            record.setContentDescription(getString(R.string.action_record_stop));
            say(getString(R.string.toast_recording));
        } catch (Exception e) {
            say("Could not start recording");
        }
    }

    private void finishRecording() {
        String name = recorder.stop();
        recordChip.setVisibility(View.GONE);
        record.setIconResource(R.drawable.ic_record);
        record.setContentDescription(getString(R.string.action_record));
        if (name != null) {
            sayWithAction("Recording saved", getString(R.string.action_view),
                    () -> viewLibrary(true));
        }
    }

    // -- settings ------------------------------------------------------------

    private void showSettings() {
        BottomSheetDialog sheet = new BottomSheetDialog(this, R.style.SheetTheme);
        sheet.setContentView(R.layout.sheet_settings);

        MaterialSwitch shape = sheet.findViewById(R.id.switchShape);
        MaterialSwitch motion = sheet.findViewById(R.id.switchMotion);
        MaterialSwitch screenOn = sheet.findViewById(R.id.switchScreenOn);
        MaterialSwitch diags = sheet.findViewById(R.id.switchDiagnostics);
        View gallery = sheet.findViewById(R.id.openGallery);
        TextView about = sheet.findViewById(R.id.aboutText);
        if (shape == null || motion == null || screenOn == null || diags == null
                || gallery == null || about == null) {
            return;
        }

        shape.setChecked(fourThree);
        motion.setChecked(decoder.isPaced());
        screenOn.setChecked(prefs.getBoolean(PREF_SCREEN_ON, true));
        diags.setChecked(showDiagnostics);

        shape.setOnCheckedChangeListener((b, checked) -> {
            fourThree = checked;
            prefs.edit().putBoolean(PREF_FOUR_THREE, checked).apply();
            applyVideoSize();
        });
        motion.setOnCheckedChangeListener((b, checked) -> setPaced(checked));
        screenOn.setOnCheckedChangeListener((b, checked) -> {
            prefs.edit().putBoolean(PREF_SCREEN_ON, checked).apply();
            applyScreenOn(checked);
        });
        diags.setOnCheckedChangeListener((b, checked) -> {
            showDiagnostics = checked;
            prefs.edit().putBoolean(PREF_DIAGNOSTICS, checked).apply();
            diagnostics.setVisibility(checked ? View.VISIBLE : View.GONE);
        });

        wireCameraParameter(sheet, R.id.ledGroup, "Infrared",
                new int[]{R.id.led0, R.id.led1, R.id.led2});
        wireCameraParameter(sheet, R.id.flipGroup, "FlipMirror",
                new int[]{R.id.flip0, R.id.flip1, R.id.flip2, R.id.flip3});

        gallery.setOnClickListener(v -> {
            sheet.dismiss();
            viewLibrary(false);
        });
        about.setText(aboutText());

        sheet.show();
    }

    /**
     * Wire one of the camera's own parameters to a row of buttons.
     *
     * <p>Deliberately fire-and-forget: these cameras accept a change, refuse it, or
     * stop sending altogether, and the only honest feedback is the picture itself.
     */
    private void wireCameraParameter(BottomSheetDialog sheet, int groupId, String key, int[] buttons) {
        MaterialButtonToggleGroup group = sheet.findViewById(groupId);
        if (group == null) {
            return;
        }
        group.addOnButtonCheckedListener((g, checkedId, isChecked) -> {
            if (!isChecked || session == null) {
                return;
            }
            for (int i = 0; i < buttons.length; i++) {
                if (buttons[i] == checkedId) {
                    Map<String, Integer> change = new HashMap<>();
                    change.put(key, i);
                    session.requestSet(change);
                    say(getString(R.string.toast_asked, key, i));
                    return;
                }
            }
        });
    }

    private void applyScreenOn(boolean on) {
        if (on) {
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        } else {
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        }
    }

    private String aboutText() {
        return getString(R.string.about_line,
                BuildConfig.VERSION_NAME,
                cameraIp == null ? "-" : cameraIp,
                decoder.decoderName().isEmpty() ? "-" : decoder.decoderName());
    }

    // -- status line ---------------------------------------------------------

    private void tick() {
        if (session != null && streaming) {
            long now = System.currentTimeMillis();
            if (lastStatsAt > 0 && now > lastStatsAt) {
                measuredFps = (session.videoFrames - lastFrameCount) / ((now - lastStatsAt) / 1000.0);
            }
            lastStatsAt = now;
            lastFrameCount = session.videoFrames;

            sizePill.setText(getString(R.string.pill_size, videoWidth, videoHeight));
            fpsPill.setText(getString(R.string.pill_fps, (float) measuredFps));

            if (showDiagnostics) {
                diagnostics.setText(getString(R.string.diagnostics_line,
                        decoder.lastLatencyMs(), decoder.droppedFrames(),
                        sessionState, decoder.decoderName()));
            }
            if (recorder.isRecording()) {
                long seconds = (now - recordingStartedAt) / 1000;
                recordTime.setText(getString(R.string.rec_time, seconds / 60, seconds % 60));
            }
        }
        main.postDelayed(this::tick, 1000);
    }

    // -- small helpers -------------------------------------------------------

    private void say(String text) {
        Snackbar bar = Snackbar.make(root, text, Snackbar.LENGTH_SHORT);
        bar.setAnchorView(controls);
        bar.show();
    }

    private void sayWithAction(String text, String action, Runnable onAction) {
        Snackbar bar = Snackbar.make(root, text, Snackbar.LENGTH_LONG);
        bar.setAnchorView(controls);
        bar.setAction(action, v -> onAction.run());
        bar.show();
    }

    private void view(Uri uri, String type) {
        try {
            Intent intent = new Intent(Intent.ACTION_VIEW);
            intent.setDataAndType(uri, type);
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            startActivity(intent);
        } catch (Exception e) {
            say("No app available to open it");
        }
    }

    /** Open the gallery at everything this app has saved. */
    private void viewLibrary(boolean video) {
        view(video ? MediaStore.Video.Media.EXTERNAL_CONTENT_URI
                   : MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
             video ? "video/*" : "image/*");
    }

    private String stamp() {
        return new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(new Date());
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
