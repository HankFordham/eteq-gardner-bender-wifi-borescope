package com.eteqcam.net;

import java.io.IOException;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetSocketAddress;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;

/**
 * The camera's discovery beacon. Ported from the beacon half of {@code eteq/discovery.py}.
 *
 * <p>The camera broadcasts a 32-byte datagram from its port 1000 to
 * {@code 255.255.255.255:2000} once per second. The vendor phone app listens for it but
 * does not actually need it: it hardcodes {@code 192.168.2.103}. We prefer the beacon
 * because other units of the same family hand out different addresses.
 *
 * <p>Layout: magic {@code 8713}, 4-byte IPv4 address, 16-byte NUL-padded name,
 * big-endian audio level, two spare bytes, then width and height. Real hardware sends
 * zeros for the size, in which case the vendor app assumes 640x240.
 *
 * <p>Plain Java 17, standard library only. On Android, bind the socket to the camera's
 * network yourself and use {@link #listen(DatagramSocket, int)}: a phone that still has
 * mobile data up will otherwise not see the broadcast.
 */
public final class Beacon {

    /** The camera's own IPv4 address, as it advertises it. */
    public final String ip;
    /** The camera's name, {@code WIFICAM} on the reference unit. */
    public final String name;
    /** Audio volume / alert level; 0 on the reference unit. */
    public final int avol;
    /** Advertised picture width, or 0 when the camera does not say (it never does). */
    public final int width;
    /** Advertised picture height, or 0 when the camera does not say. */
    public final int height;
    /** The address the datagram actually came from, or "" when not known. */
    public final String source;

    public Beacon(String ip, String name, int avol, int width, int height, String source) {
        this.ip = ip;
        this.name = name;
        this.avol = avol;
        this.width = width;
        this.height = height;
        this.source = source;
    }

    /** What the vendor app hardcodes, and a reasonable last resort. */
    public static final String DEFAULT_IP = "192.168.2.103";

    /** One line for a UI: "WIFICAM at 192.168.2.103 (audio level 0, picture size ...)". */
    public String describe() {
        String size = (width != 0 && height != 0) ? (width + "x" + height) : "not advertised";
        return name + " at " + ip + " (audio level " + avol + ", picture size " + size + ")";
    }

    /** Decode a beacon datagram, or return null if it is not one. */
    public static Beacon parse(byte[] datagram) {
        return parse(datagram, "");
    }

    static Beacon parse(byte[] data, String source) {
        if (data == null || data.length != Protocol.BEACON_SIZE) {
            return null;
        }
        if (!Protocol.startsWith(data, Protocol.BEACON_MAGIC)) {
            return null;
        }
        String ip = (data[4] & 0xFF) + "." + (data[5] & 0xFF) + "."
                  + (data[6] & 0xFF) + "." + (data[7] & 0xFF);
        int nameEnd = 8;
        while (nameEnd < 24 && data[nameEnd] != 0) {
            nameEnd++;
        }
        String name = new String(Arrays.copyOfRange(data, 8, nameEnd), StandardCharsets.US_ASCII);
        int avol = ((data[24] & 0xFF) << 8) | (data[25] & 0xFF);
        int width = ((data[28] & 0xFF) << 8) | (data[29] & 0xFF);
        int height = ((data[30] & 0xFF) << 8) | (data[31] & 0xFF);
        return new Beacon(ip, name, avol, width, height, source);
    }

    /**
     * Listens on UDP 2000. Returns null on timeout.
     *
     * <p>Throws if the port cannot be bound, which almost always means something else
     * already holds UDP 2000. A silent wait almost always means the network in use is
     * not the camera's, or a firewall is dropping the broadcast.
     */
    public static Beacon listen(int timeoutMs) throws IOException {
        DatagramSocket sock = new DatagramSocket(null);
        try {
            sock.setReuseAddress(true);
            sock.bind(new InetSocketAddress(Protocol.BEACON_PORT));
            return listen(sock, timeoutMs);
        } finally {
            sock.close();
        }
    }

    /**
     * Same, but on a socket already bound to a particular network (Android).
     *
     * <p>The socket is not closed; the caller owns it. Datagrams that are not beacons
     * are skipped and the wait continues until the timeout.
     */
    public static Beacon listen(DatagramSocket socket, int timeoutMs) throws IOException {
        long deadline = Transport.nowMs() + Math.max(0, timeoutMs);
        byte[] buffer = new byte[1024];
        while (true) {
            long remaining = deadline - Transport.nowMs();
            if (remaining <= 0) {
                return null;
            }
            socket.setSoTimeout((int) Math.max(1, Math.min(remaining, Integer.MAX_VALUE)));
            DatagramPacket packet = new DatagramPacket(buffer, buffer.length);
            try {
                socket.receive(packet);
            } catch (SocketTimeoutException exc) {
                return null;
            }
            byte[] data = Arrays.copyOfRange(packet.getData(), packet.getOffset(),
                                             packet.getOffset() + packet.getLength());
            String source = packet.getAddress() == null ? "" : packet.getAddress().getHostAddress();
            Beacon beacon = parse(data, source);
            if (beacon != null) {
                return beacon;
            }
        }
    }
}
