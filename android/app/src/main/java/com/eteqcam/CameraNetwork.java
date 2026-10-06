package com.eteqcam;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.net.RouteInfo;
import android.net.wifi.WifiManager;
import android.os.Handler;
import android.os.Looper;

import java.io.IOException;
import java.net.DatagramSocket;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;

/**
 * Holds on to the camera's WiFi network and hands out sockets bound to it.
 *
 * <p>This class exists because of one Android behaviour that otherwise makes the
 * whole app fail in a confusing way. The camera's hotspot has no internet. Android
 * notices that, marks the network unvalidated, and quietly routes every socket in
 * the app over mobile data instead. Packets addressed to the camera then leave
 * through the cellular interface and vanish, while WiFi still shows as connected.
 *
 * <p>The cure is to ask for the WiFi network explicitly, with the internet
 * capability removed from the request so the system does not disqualify it, and
 * then bind each socket to that specific network. We bind individual sockets
 * rather than the whole process so the rest of the phone keeps working normally.
 *
 * <p>Finding the camera is a bonus: on these hotspots the camera is the DHCP
 * server and the default gateway, so the network's own routing table names it.
 * That is more reliable than waiting for the discovery beacon, which a phone may
 * never see.
 */
public final class CameraNetwork {

    /** Reported on the main thread. */
    public interface Callback {
        /**
         * The WiFi network is available and sockets can be bound to it.
         *
         * @param gatewayIp the default gateway, which on a camera hotspot is the
         *                  camera itself, or null if it could not be determined
         */
        void onAvailable(String gatewayIp);

        /** The network went away. */
        void onLost(String reason);
    }

    private final ConnectivityManager manager;
    private final WifiManager wifiManager;
    private final Handler main = new Handler(Looper.getMainLooper());
    private WifiManager.WifiLock wifiLock;
    private ConnectivityManager.NetworkCallback callback;
    private volatile Network network;

    public CameraNetwork(Context context) {
        Context app = context.getApplicationContext();
        this.manager = (ConnectivityManager) app.getSystemService(Context.CONNECTIVITY_SERVICE);
        this.wifiManager = (WifiManager) app.getSystemService(Context.WIFI_SERVICE);
    }

    /**
     * Ask the radio to stop saving power while we are watching.
     *
     * <p>WiFi normally batches and sleeps between beacons, which is sensible for
     * email and ruinous for a live picture: it shows up as the stream arriving in
     * bursts. Low-latency mode turns that off for as long as the lock is held. It
     * costs battery, which is the right trade while staring at a video.
     */
    private void holdRadio() {
        if (wifiManager == null || wifiLock != null) {
            return;
        }
        try {
            wifiLock = wifiManager.createWifiLock(
                    WifiManager.WIFI_MODE_FULL_LOW_LATENCY, "eteq:stream");
            wifiLock.setReferenceCounted(false);
            wifiLock.acquire();
        } catch (Exception ignored) {
            wifiLock = null;
        }
    }

    private void freeRadio() {
        if (wifiLock != null) {
            try {
                wifiLock.release();
            } catch (Exception ignored) {
                // nothing useful to do
            }
            wifiLock = null;
        }
    }

    /** Request the WiFi network. The callback fires once it is usable. */
    public void acquire(final Callback cb) {
        release();
        NetworkRequest request = new NetworkRequest.Builder()
                .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                // Without these two removals the request never matches a hotspot
                // that has no internet, which is exactly what the camera is.
                .removeCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                .removeCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
                .build();

        callback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(Network available) {
                network = available;
                holdRadio();
                final String gateway = gatewayOf(available);
                main.post(() -> cb.onAvailable(gateway));
            }

            @Override
            public void onLost(Network lost) {
                if (lost.equals(network)) {
                    network = null;
                    main.post(() -> cb.onLost("the WiFi network went away"));
                }
            }

            @Override
            public void onUnavailable() {
                main.post(() -> cb.onLost("no WiFi network became available"));
            }
        };

        try {
            manager.requestNetwork(request, callback);
        } catch (SecurityException e) {
            main.post(() -> cb.onLost("not allowed to choose a network: " + e.getMessage()));
        }
    }

    public void release() {
        freeRadio();
        if (callback != null) {
            try {
                manager.unregisterNetworkCallback(callback);
            } catch (IllegalArgumentException ignored) {
                // already gone
            }
            callback = null;
        }
        network = null;
    }

    public boolean isReady() {
        return network != null;
    }

    /**
     * A UDP socket that really will send over the camera's WiFi.
     *
     * @param localPort port to bind, or 0 for any
     */
    public DatagramSocket createSocket(int localPort) throws IOException {
        DatagramSocket socket = new DatagramSocket(null);
        socket.setReuseAddress(true);
        socket.setReceiveBufferSize(1 << 17);
        socket.bind(new InetSocketAddress(localPort));
        Network bound = network;
        if (bound == null) {
            socket.close();
            throw new IOException("the camera's WiFi is not available");
        }
        try {
            bound.bindSocket(socket);
        } catch (IOException e) {
            socket.close();
            throw new IOException("could not attach the socket to the camera's WiFi: " + e.getMessage(), e);
        }
        return socket;
    }

    /** The default gateway of a network, which on these hotspots is the camera. */
    private String gatewayOf(Network target) {
        LinkProperties properties = manager.getLinkProperties(target);
        if (properties == null) {
            return null;
        }
        for (RouteInfo route : properties.getRoutes()) {
            if (!route.isDefaultRoute()) {
                continue;
            }
            InetAddress gateway = route.getGateway();
            if (gateway instanceof Inet4Address && !gateway.isAnyLocalAddress()) {
                return gateway.getHostAddress();
            }
        }
        return null;
    }
}
