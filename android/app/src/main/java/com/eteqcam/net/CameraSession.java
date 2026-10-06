package com.eteqcam.net;

import java.io.IOException;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.SocketException;
import java.net.UnknownHostException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.BooleanSupplier;

/**
 * Driving one camera: handshake, streaming, reconnection, control. A port of
 * {@code eteq/session.py}, minus the parts that only make sense on a desktop
 * (discovery, recording sinks, HTTP status).
 *
 * <p>The sequence mirrors what the vendor app does, because that is the only sequence
 * the firmware is known to accept:
 *
 * <ol>
 *   <li>{@code GET AllInfo} and wait briefly for the acknowledgement.
 *   <li>{@code SET} with {@code Video=1} plus the picture parameters. The camera answers
 *       {@code Ret=1} and starts sending.
 *   <li>Acknowledge every packet, and send the {@code GetSnapPhoto} user command once a
 *       second, which is also how the camera reports its physical snapshot button.
 *   <li>On exit, {@code SET Video=0} and close the socket.
 * </ol>
 *
 * <p>If the camera stops talking we tear the session down and start a new one, which is
 * what the phone app effectively does through its own one-second timer.
 *
 * <p>{@link #run()} blocks until {@link #stop()}; run it on its own thread. Every
 * callback on {@link Listener} is made from that thread, so a UI must hop to its own
 * looper itself. Plain Java 17, standard library only: no {@code android.*} here.
 */
public final class CameraSession implements Runnable {

    /** Everything the caller learns about the session. */
    public interface Listener {
        /** One complete access unit. Called from the session thread; do not block long. */
        void onFrame(H264Framer.Frame frame);

        /** Human-readable one-line state, e.g. "streaming", "connecting", "camera silent". */
        void onState(String state);

        /** Something the user must act on. */
        void onError(String message);

        /** The camera's own snapshot button was pressed. */
        void onSnapshotButton();
    }

    /** What we ask the camera for when a session starts. */
    public static final class Settings {
        public int width = 640, height = 240, fps = 20, bitrate = 2048, zoom = 0,
                   brightness = 128, contrast = 4, saturation = 4, flipMirror = 3;
        /** Null means "do not mention Infrared at all", which is what the app does. */
        public Integer infrared = null;
        public boolean audio = true;
        /** Send only {@code Video=1}, for cameras that reject the full parameter list. */
        public boolean minimal = false;
        /** Any further key/value parameters to send alongside the standard ones. */
        public final Map<String, Integer> extra = new LinkedHashMap<>();

        /**
         * The items of the start-up {@code SET}, in the order the vendor app sends them.
         *
         * <p>Order matters: this is byte-for-byte the 195-byte packet that makes the
         * reference camera stream. Values are lowercase hex with no padding, like the
         * vendor's {@code "%x"}.
         */
        public List<String[]> startItems() {
            List<String[]> pairs = new ArrayList<>();
            if (minimal) {
                pairs.add(new String[] {"Video", "1"});
                return pairs;
            }
            pairs.add(new String[] {"Audio", audio ? "1" : "0"});
            pairs.add(new String[] {"Video", "1"});
            pairs.add(new String[] {"FrameSize", Protocol.hex(Protocol.frameSizeValue(width, height))});
            pairs.add(new String[] {"FrameRate", Protocol.hex(fps)});
            pairs.add(new String[] {"BitRate", Protocol.hex(bitrate)});
            pairs.add(new String[] {"Zoom", Protocol.hex(zoom)});
            pairs.add(new String[] {"Brightness", Protocol.hex(brightness)});
            pairs.add(new String[] {"Contrast", Protocol.hex(contrast)});
            pairs.add(new String[] {"Saturation", Protocol.hex(saturation)});
            pairs.add(new String[] {"FlipMirror", Protocol.hex(flipMirror)});
            if (infrared != null) {
                pairs.add(new String[] {"Infrared", Protocol.hex(infrared)});
            }
            for (Map.Entry<String, Integer> e : extra.entrySet()) {
                pairs.add(new String[] {e.getKey(), Protocol.hex(e.getValue())});
            }
            return pairs;
        }

        /** Record a change made at runtime so the UI and the next session stay in step. */
        public void apply(Map<String, Integer> params) {
            for (Map.Entry<String, Integer> e : params.entrySet()) {
                String key = e.getKey();
                int value = e.getValue();
                switch (key) {
                    case "FrameSize" -> {
                        int[] wh = Protocol.parseFrameSize(value);
                        width = wh[0];
                        height = wh[1];
                    }
                    case "FrameRate" -> fps = value;
                    case "BitRate" -> bitrate = value;
                    case "Zoom" -> zoom = value;
                    case "Brightness" -> brightness = value;
                    case "Contrast" -> contrast = value;
                    case "Saturation" -> saturation = value;
                    case "FlipMirror" -> flipMirror = value;
                    case "Infrared" -> infrared = value;
                    case "Audio" -> audio = value != 0;
                    default -> extra.put(key, value);
                }
            }
        }

        /** The current parameters, for a UI that wants to show them. */
        public Map<String, Integer> asMap() {
            Map<String, Integer> out = new LinkedHashMap<>();
            out.put("FrameSize", Protocol.frameSizeValue(width, height));
            out.put("FrameRate", fps);
            out.put("BitRate", bitrate);
            out.put("Zoom", zoom);
            out.put("Brightness", brightness);
            out.put("Contrast", contrast);
            out.put("Saturation", saturation);
            out.put("FlipMirror", flipMirror);
            if (infrared != null) {
                out.put("Infrared", infrared);
            }
            out.putAll(extra);
            return out;
        }
    }

    /** Supply a socket factory so Android can hand over a network-bound socket. */
    public interface SocketFactory {
        DatagramSocket create() throws IOException;
    }

    private enum Outcome { DONE, RECONNECT, RESTART, GIVE_UP }

    // -- configuration --------------------------------------------------------

    private final String cameraIp;
    public final Settings settings;
    private final Listener listener;

    public int camPort = Protocol.CAM_PORT;
    /** How long to wait for the GET and SET acknowledgements before carrying on. */
    public int ackTimeoutMs = 3000;
    public int heartbeatMs = 1000;
    public boolean sendHeartbeat = true;
    public boolean skipAllInfo = false;
    /** Restart the session if nothing at all arrives for this long. */
    public int idleTimeoutMs = 4000;
    /**
     * Restart if the video stops for this long, even while the camera still talks.
     *
     * <p>This timer is the whole point of the "dead encoder" quirk: the heartbeat is
     * answered once a second whether or not video is flowing, so a plain "heard
     * nothing" timer never fires when the encoder dies. On the WIC-100 that happens
     * the moment certain settings are changed.
     */
    public int videoTimeoutMs = 5000;
    public int reconnectDelayMs = 1000;
    public boolean reconnect = true;
    public boolean sendStop = true;
    /** Set when the IP was a guess rather than a beacon, which changes the advice given. */
    public boolean addressGuessed = false;

    // -- counters -------------------------------------------------------------

    public volatile int videoFrames = 0;
    public volatile int keyFrames = 0;
    public volatile long videoBytes = 0;
    public volatile int sessions = 0;
    public volatile int settingsRefused = 0;
    /** The {@code Ret} of the last SET acknowledgement: "1" accepted, "0" thrown away. */
    public volatile String lastSetRet = null;
    /** Monotonic milliseconds of the last complete frame, or -1 if none this session. */
    public volatile long lastFrameAtMs = -1;
    public volatile int streamPackets = 0;
    public volatile int audioChunks = 0;
    public volatile long audioBytes = 0;
    public volatile int snapshotPresses = 0;
    public volatile boolean setAcked = false;
    public volatile boolean getAcked = false;

    // -- state ----------------------------------------------------------------

    private volatile Transport transport;
    private H264Framer framer = new H264Framer();
    private long tsOffset = 0;
    private long lastTs = -1;
    private boolean announcedStreaming = false;
    private int silentSessions = 0;

    private final Map<String, Integer> pending = new LinkedHashMap<>();
    private final Object pendingLock = new Object();
    private volatile boolean liveSettings = false;

    private final Object stopLock = new Object();
    private boolean stopRequested = false;

    private SocketFactory socketFactory = CameraSession::defaultSocket;

    public CameraSession(String cameraIp, Settings settings, Listener listener) {
        if (cameraIp == null || settings == null || listener == null) {
            throw new NullPointerException("cameraIp, settings and listener are all required");
        }
        this.cameraIp = cameraIp;
        this.settings = settings;
        this.listener = listener;
    }

    /** Default socket: an ephemeral local port with the receive buffer the SDK uses. */
    private static DatagramSocket defaultSocket() throws IOException {
        DatagramSocket s = new DatagramSocket(null);
        s.setReuseAddress(true);
        try {
            s.setReceiveBufferSize(0x20000);
        } catch (SocketException ignored) {
            // A smaller buffer only risks dropped packets, which the transport handles.
        }
        s.bind(new InetSocketAddress(0));
        return s;
    }

    public void setSocketFactory(SocketFactory factory) {
        this.socketFactory = factory == null ? CameraSession::defaultSocket : factory;
    }

    public Transport transport() {
        return transport;
    }

    public String cameraIp() {
        return cameraIp;
    }

    // -- control surface ------------------------------------------------------

    /**
     * Queue a parameter change; the session loop sends it.
     *
     * <p>By default this restarts the session, because the reference camera refuses most
     * mid-stream changes with {@code Ret=0} and stops encoding altogether on the ones it
     * accepts. See {@link #setLiveSettings}.
     */
    public void requestSet(Map<String, Integer> params) {
        if (params == null || params.isEmpty()) {
            return;
        }
        synchronized (pendingLock) {
            pending.putAll(params);
        }
    }

    /**
     * Try to change settings without restarting the stream.
     *
     * <p>Off by default on purpose. The reference camera answers {@code Ret=0} to
     * {@code Brightness}, {@code Contrast}, {@code Saturation} and {@code FlipMirror}
     * mid-stream and ignores them; it answers {@code Ret=1} to {@code Zoom},
     * {@code FrameSize}, {@code FrameRate} and {@code BitRate} and then stops encoding
     * for good. A restart is the only thing that reliably works.
     */
    public void setLiveSettings(boolean live) {
        this.liveSettings = live;
    }

    public boolean liveSettings() {
        return liveSettings;
    }

    /** Ask {@link #run()} to finish. Safe from any thread. */
    public void stop() {
        synchronized (stopLock) {
            stopRequested = true;
            stopLock.notifyAll();
        }
    }

    private boolean isStopped() {
        synchronized (stopLock) {
            return stopRequested;
        }
    }

    /** Wait, unless stop() has been or gets called. Returns true if we should quit. */
    private boolean sleepOrStop(long ms) {
        long deadline = Transport.nowMs() + ms;
        synchronized (stopLock) {
            while (!stopRequested) {
                long remaining = deadline - Transport.nowMs();
                if (remaining <= 0) {
                    return false;
                }
                try {
                    stopLock.wait(remaining);
                } catch (InterruptedException exc) {
                    Thread.currentThread().interrupt();
                    return true;
                }
            }
            return true;
        }
    }

    // -- the loop -------------------------------------------------------------

    /** Blocks until {@link #stop()}; drives everything. */
    @Override
    public void run() {
        try {
            while (!isStopped()) {
                Outcome outcome = runOnce();
                if (outcome == Outcome.RESTART) {
                    // Deliberate: stop cleanly, then start again straight away.
                    closeTransport(true);
                    continue;
                }
                if (outcome != Outcome.RECONNECT) {
                    return; // DONE or GIVE_UP
                }
                if (!reconnect || isStopped()) {
                    return;
                }
                closeTransport(false);
                if (sleepOrStop(reconnectDelayMs)) {
                    return;
                }
            }
        } finally {
            closeTransport(sendStop);
            safeState("stopped");
        }
    }

    private Outcome runOnce() {
        sessions++;
        setAcked = false;
        getAcked = false;
        lastFrameAtMs = -1;
        announcedStreaming = false;
        // A new framer per session: the camera's byte stream restarts, and a half-built
        // access unit from the old one would corrupt the first frame of the new.
        framer = new H264Framer();

        safeState(sessions == 1 ? "connecting" : "reconnecting");

        InetAddress peer;
        try {
            peer = InetAddress.getByName(cameraIp);
        } catch (UnknownHostException exc) {
            safeError("Cannot make sense of the camera address " + cameraIp + ".");
            return Outcome.GIVE_UP;
        }

        DatagramSocket socket;
        try {
            socket = socketFactory.create();
        } catch (IOException exc) {
            safeError("Cannot open a UDP socket: " + exc.getMessage());
            return Outcome.GIVE_UP;
        }

        Transport t = new Transport(socket, peer, camPort);
        t.handler = this::onMessage;
        transport = t;
        long started = Transport.nowMs();

        if (!skipAllInfo) {
            t.sendData(Protocol.buildGetAllInfo());
            pump(() -> getAcked, ackTimeoutMs);
        }

        t.sendData(Protocol.buildSet(settings.startItems()));
        pump(() -> setAcked, ackTimeoutMs);

        if (t.rxPackets == 0) {
            silentSessions++;
            // Say it once when it starts, and again when we give up, so a user who
            // only looks at the end still gets the checklist.
            if (silentSessions == 1 || silentSessions >= 3) {
                safeError(explainSilence());
            }
            safeState("camera silent");
            return silentSessions >= 3 ? Outcome.GIVE_UP : Outcome.RECONNECT;
        }
        silentSessions = 0;

        long lastHeartbeat = Transport.nowMs();
        while (!isStopped()) {
            pump(() -> false, 250);
            long now = Transport.nowMs();

            if (t.linkLost) {
                safeError("The camera stopped acknowledging anything; reconnecting.");
                return Outcome.RECONNECT;
            }

            if (flushPending()) {
                return Outcome.RESTART;
            }

            if (sendHeartbeat && now - lastHeartbeat >= heartbeatMs) {
                lastHeartbeat = now;
                t.sendData(Protocol.buildUserCommand(Protocol.HEARTBEAT_UDC));
            }

            long quiet = t.lastRxTimeMs >= 0 ? now - t.lastRxTimeMs : now - started;
            if (quiet > idleTimeoutMs) {
                safeState("camera silent");
                return Outcome.RECONNECT;
            }

            // The camera keeps answering the heartbeat after its encoder stops, so
            // silence alone is not enough to notice a dead picture. Watch for the
            // absence of *video* specifically, and restart when it stops.
            long sinceFrame = lastFrameAtMs;
            if (sinceFrame >= 0 && now - sinceFrame > videoTimeoutMs) {
                safeState("picture stopped, restarting");
                return Outcome.RESTART;
            }
        }
        return Outcome.DONE;
    }

    /**
     * Say why nothing answered, in terms of what to actually do about it.
     *
     * <p>Retrying a wrong address forever looks identical to a broken camera, and the
     * commonest cause by far is simply not being on the camera's WiFi.
     */
    private String explainSilence() {
        StringBuilder sb = new StringBuilder();
        sb.append("Nothing at ").append(cameraIp).append(" answered. No packets at all came back.");
        if (addressGuessed) {
            sb.append("\nThat address was a guess, because no camera announced itself. "
                      + "This device is almost certainly not on the camera's WiFi network.");
        }
        sb.append("\nCheck, in this order:");
        sb.append("\n  1. The camera is switched on and its light is lit.");
        sb.append("\n  2. This device is joined to the camera's own WiFi (often WIFICAMERA).");
        sb.append("\n  3. No phone or other app is connected to it; it allows only one at a time.");
        sb.append("\n  4. This app is allowed to use the local network.");
        return sb.toString();
    }

    /** Apply queued parameter changes. Returns true if the session must restart. */
    private boolean flushPending() {
        Map<String, Integer> params;
        synchronized (pendingLock) {
            if (pending.isEmpty()) {
                return false;
            }
            params = new LinkedHashMap<>(pending);
            pending.clear();
        }
        settings.apply(params);
        if (liveSettings) {
            Transport t = transport;
            if (t != null) {
                List<String[]> pairs = new ArrayList<>();
                for (Map.Entry<String, Integer> e : params.entrySet()) {
                    pairs.add(new String[] {e.getKey(), Protocol.hex(e.getValue())});
                }
                t.sendData(Protocol.buildSet(pairs));
            }
            return false;
        }
        safeState("applying settings");
        return true;
    }

    private void closeTransport(boolean withStop) {
        Transport t = transport;
        if (t == null) {
            return;
        }
        if (withStop) {
            try {
                t.sendData(Protocol.buildStop());
                pump(() -> false, 300);
            } catch (RuntimeException exc) {
                // Best effort: the socket is about to go away anyway.
            }
        }
        t.close();
        transport = null;
    }

    /** Service the socket until {@code done} or the timeout expires. */
    private boolean pump(BooleanSupplier done, long timeoutMs) {
        Transport t = transport;
        if (t == null) {
            return false;
        }
        long deadline = Transport.nowMs() + timeoutMs;
        while (true) {
            if (done.getAsBoolean()) {
                return true;
            }
            long remaining = deadline - Transport.nowMs();
            if (remaining <= 0) {
                return done.getAsBoolean();
            }
            t.receiveOnce((int) Math.min(20, remaining));
            t.retransmitTick();
        }
    }

    // -- incoming data --------------------------------------------------------

    private void onMessage(byte[] payload) {
        Protocol.Message msg = Protocol.parse(payload);
        if (msg == null) {
            return; // not a message; the zero-prefix form is accepted by parse()
        }

        switch (msg.code) {
            case Protocol.CODE_STREAM -> {
                Protocol.StreamChunk chunk = Protocol.parseStreamChunk(msg);
                if (chunk != null) {
                    onChunk(chunk);
                }
            }
            case Protocol.CODE_SET_ACK -> {
                // Ret is a verdict, not a receipt: 1 means the camera took the setting,
                // 0 means it threw it away. The reference camera answers 0 for most
                // changes made while it is already streaming, so a client that treats
                // any acknowledgement as success silently believes it changed something
                // it did not.
                String ret = msg.getText("Ret");
                setAcked = true;
                lastSetRet = ret;
                if ("0".equals(ret)) {
                    settingsRefused++;
                    safeError("The camera refused that setting (Ret=0).");
                }
            }
            case Protocol.CODE_GET_ACK -> getAcked = true;
            case Protocol.CODE_USR_ACK -> {
                if (isSnapshotPressed(msg.body)) {
                    snapshotPresses++;
                    try {
                        listener.onSnapshotButton();
                    } catch (RuntimeException exc) {
                        // A listener must not kill the session.
                    }
                }
            }
            default -> {
                // 0014 and anything else: nothing useful to do with it.
            }
        }
    }

    private static boolean isSnapshotPressed(byte[] body) {
        int start = 0, end = body.length;
        while (start < end && isSpace(body[start])) {
            start++;
        }
        while (end > start && isSpace(body[end - 1])) {
            end--;
        }
        int n = end - start;
        if (n != Protocol.SNAPSHOT_PRESSED.length) {
            return false;
        }
        for (int i = 0; i < n; i++) {
            if (body[start + i] != Protocol.SNAPSHOT_PRESSED[i]) {
                return false;
            }
        }
        return true;
    }

    private static boolean isSpace(byte b) {
        return b == ' ' || b == '\t' || b == '\r' || b == '\n' || b == 0x0b || b == 0x0c;
    }

    private void onChunk(Protocol.StreamChunk chunk) {
        streamPackets++;
        if (chunk.data.length == 0) {
            return;
        }
        if (chunk.isAudio()) {
            audioChunks++;
            audioBytes += chunk.data.length;
            return;
        }

        videoBytes += chunk.data.length;
        long ts = chunk.info != null ? (chunk.info.timestampMs & 0xFFFFFFFFL) : -1;
        for (H264Framer.Frame frame : framer.push(chunk.data, ts)) {
            emit(frame);
        }
    }

    /**
     * Hand one access unit to the listener on a monotonic timeline.
     *
     * <p>The camera restarts its millisecond clock each session, so reconnecting would
     * otherwise rewind time and stall any decoder that cares about presentation order.
     */
    private void emit(H264Framer.Frame frame) {
        long ts = frame.timestampMs + tsOffset;
        if (ts <= lastTs) {
            tsOffset += lastTs - ts + 33;
            ts = frame.timestampMs + tsOffset;
        }
        lastTs = ts;
        H264Framer.Frame out = new H264Framer.Frame(frame.data, ts, frame.keyframe);

        videoFrames++;
        lastFrameAtMs = Transport.nowMs();
        if (out.keyframe) {
            keyFrames++;
        }
        if (!announcedStreaming) {
            announcedStreaming = true;
            safeState("streaming");
        }
        try {
            listener.onFrame(out);
        } catch (RuntimeException exc) {
            // A listener must not kill the session.
        }
    }

    private void safeState(String state) {
        try {
            listener.onState(state);
        } catch (RuntimeException exc) {
            // ignored on purpose
        }
    }

    private void safeError(String message) {
        try {
            listener.onError(message);
        } catch (RuntimeException exc) {
            // ignored on purpose
        }
    }
}
