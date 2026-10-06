package com.eteqcam.net;

import java.io.IOException;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.SocketTimeoutException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.locks.ReentrantLock;

/**
 * Reliable UDP transport to the camera. A faithful port of {@code eteq/transport.py}.
 *
 * <p>Every datagram in both directions starts with four bytes:
 *
 * <table>
 *   <caption>Transport header</caption>
 *   <tr><th>byte</th><th>meaning</th></tr>
 *   <tr><td>0</td><td>type: 0 data, 1 ack, 2 nack</td></tr>
 *   <tr><td>1</td><td>sequence number of this data packet, or the one being referred to</td></tr>
 *   <tr><td>2</td><td>the sender's next expected incoming sequence number (a cumulative ack)</td></tr>
 *   <tr><td>3</td><td>the literal byte {@code 'v'}; anything else is not ours</td></tr>
 * </table>
 *
 * <p>Both sides must acknowledge everything, or the other end eventually gives up.
 * Sequence numbers are 8 bits and wrap, and the receive window is 32 packets.
 *
 * <p>Two behaviours here were learned the hard way against real hardware and matter
 * more than they look:
 *
 * <ul>
 *   <li>The camera retransmits a packet whenever one of our acks goes missing. Those
 *       duplicates are normal and must simply be re-acked. Treating them as a sign of
 *       a desynchronised session and renumbering in response produces an ack storm
 *       that kills the stream within a couple of minutes (about 75 s, measured).
 *       See {@link #handleOldPacket}.
 *   <li>An ICMP port-unreachable on a UDP socket surfaces as an IOException from
 *       receive, long after the datagram that caused it. It must be swallowed, not
 *       treated as a fatal socket error.
 * </ul>
 *
 * <p>The socket is supplied by the caller, already created and (on Android) already
 * bound to the right network, so this class never decides which interface to use.
 */
public final class Transport {

    /** Called for every in-order message payload, from whichever thread received it. */
    public interface MessageHandler {
        void onMessage(byte[] payload);
    }

    private static final class Pending {
        byte[] pkt;
        long sentAtMs;
        int tries;

        Pending(byte[] pkt, long sentAtMs) {
            this.pkt = pkt;
            this.sentAtMs = sentAtMs;
            this.tries = 0;
        }
    }

    private final DatagramSocket sock;
    private volatile InetAddress peerAddr;
    private volatile int peerPort;

    private final ReentrantLock lock = new ReentrantLock();

    // sender
    private int sendSeq;
    private int sendBase;
    private final Map<Integer, Pending> unacked = new HashMap<>();
    private final long baseRtoMs = 20;
    private long rtoMs = 20;
    private final long maxRtoMs = 1000;
    /** Give up after this many retries of the same packet, then raise {@link #linkLost}. */
    public int maxRetries = 60;

    // receiver
    private int recvExpected = 0;
    private final Map<Integer, byte[]> recvBuf = new HashMap<>();
    private boolean synced = false;
    private int badAckCount = 0;
    private int badSeqCount = 0;

    // counters, read by the UI
    public volatile int rxPackets = 0;
    public volatile long rxBytes = 0;
    public volatile int txPackets = 0;
    public volatile int rxDuplicates = 0;
    public volatile int rxUnknown = 0;
    public volatile int sockErrors = 0;
    public volatile boolean linkLost = false;
    /** Monotonic milliseconds of the last datagram received, or -1 if none ever was. */
    public volatile long lastRxTimeMs = -1;

    /** Settable: whoever wants the decoded payloads. */
    public volatile MessageHandler handler;

    private final byte[] rxBuffer = new byte[4096];

    /** The socket must already be created and, on Android, bound to the right network. */
    public Transport(DatagramSocket socket, InetAddress peer, int peerPort) {
        this(socket, peer, peerPort, 0);
    }

    public Transport(DatagramSocket socket, InetAddress peer, int peerPort, int seqStart) {
        this.sock = socket;
        this.peerAddr = peer;
        this.peerPort = peerPort;
        this.sendSeq = seqStart & 0xFF;
        this.sendBase = seqStart & 0xFF;
    }

    public void setHandler(MessageHandler h) {
        this.handler = h;
    }

    public InetAddress peerAddress() {
        return peerAddr;
    }

    public int peerPort() {
        return peerPort;
    }

    public int localPort() {
        return sock.getLocalPort();
    }

    /** How many of our packets are still waiting to be acknowledged. */
    public int unackedCount() {
        lock.lock();
        try {
            return unacked.size();
        } finally {
            lock.unlock();
        }
    }

    /** Monotonic clock in milliseconds; never jumps when the wall clock does. */
    static long nowMs() {
        return System.nanoTime() / 1_000_000L;
    }

    // -- plumbing -------------------------------------------------------------

    private void rawSend(byte[] pkt) {
        try {
            sock.send(new DatagramPacket(pkt, pkt.length, peerAddr, peerPort));
            txPackets++;
        } catch (IOException exc) {
            // Same reasoning as in receiveOnce: an ICMP unreachable from an earlier
            // datagram can surface here. Losing one packet is what the retransmit
            // timer exists for.
            sockErrors++;
        }
    }

    /** Send a payload reliably. Returns its sequence number. */
    public int sendData(byte[] payload) {
        lock.lock();
        try {
            int seq = sendSeq;
            byte[] pkt = header(Protocol.PKT_DATA, seq, recvExpected, payload);
            unacked.put(seq, new Pending(pkt, nowMs()));
            sendSeq = (seq + 1) & 0xFF;
            rawSend(pkt);
            return seq;
        } finally {
            lock.unlock();
        }
    }

    public void sendAck(int seq) {
        rawSend(header(Protocol.PKT_ACK, seq & 0xFF, recvExpected, null));
    }

    public void sendNack(int seq) {
        rawSend(header(Protocol.PKT_NACK, seq & 0xFF, recvExpected, null));
    }

    private static byte[] header(int type, int seq, int ack, byte[] payload) {
        int n = payload == null ? 0 : payload.length;
        byte[] pkt = new byte[4 + n];
        pkt[0] = (byte) type;
        pkt[1] = (byte) seq;
        pkt[2] = (byte) ack;
        pkt[3] = (byte) Protocol.PKT_MAGIC;
        if (n > 0) {
            System.arraycopy(payload, 0, pkt, 4, n);
        }
        return pkt;
    }

    // -- sender side ----------------------------------------------------------

    /** Retire everything the camera says it has received. */
    private void processAckField(int ack) {
        if (unacked.isEmpty()) {
            return;
        }
        if (!Protocol.between(sendBase, ack, (sendSeq + 1) & 0xFF)) {
            badAckCount++;
            if (badAckCount >= 3) {
                resyncSender(ack);
            }
            return;
        }
        badAckCount = 0;
        int steps = 0;
        while (sendBase != ack && steps < 256) {
            unacked.remove(sendBase);
            sendBase = (sendBase + 1) & 0xFF;
            steps++;
        }
        if (unacked.isEmpty()) {
            rtoMs = baseRtoMs;
        }
    }

    /** Adopt the camera's numbering when it is clearly continuing an old session. */
    private void resyncSender(int ack) {
        List<Integer> order = new ArrayList<>(unacked.keySet());
        final int base = sendBase;
        order.sort((x, y) -> Integer.compare((x - base) & 0xFF, (y - base) & 0xFF));
        List<byte[]> pending = new ArrayList<>(order.size());
        for (int seq : order) {
            byte[] pkt = unacked.get(seq).pkt;
            pending.add(Arrays.copyOfRange(pkt, 4, pkt.length));
        }
        unacked.clear();
        sendBase = sendSeq = ack;
        badAckCount = 0;
        for (byte[] payload : pending) {
            int seq = sendSeq;
            byte[] pkt = header(Protocol.PKT_DATA, seq, recvExpected, payload);
            unacked.put(seq, new Pending(pkt, nowMs()));
            sendSeq = (seq + 1) & 0xFF;
            rawSend(pkt);
        }
    }

    /** Resend the oldest unacknowledged packet once its timer expires. */
    public void retransmitTick() {
        lock.lock();
        try {
            if (unacked.isEmpty()) {
                return;
            }
            Pending entry = unacked.get(sendBase);
            if (entry == null) {
                int best = sendBase;
                int bestDistance = Integer.MAX_VALUE;
                for (int seq : unacked.keySet()) {
                    int distance = (seq - sendBase) & 0xFF;
                    if (distance < bestDistance) {
                        bestDistance = distance;
                        best = seq;
                    }
                }
                sendBase = best;
                entry = unacked.get(sendBase);
            }
            long now = nowMs();
            if (now - entry.sentAtMs < rtoMs) {
                return;
            }
            if (entry.tries >= maxRetries) {
                linkLost = true;
                return;
            }
            byte[] pkt = entry.pkt.clone();
            pkt[2] = (byte) recvExpected; // refresh the piggybacked ack
            entry.pkt = pkt;
            entry.sentAtMs = now;
            entry.tries++;
            rtoMs = Math.min(rtoMs * 2, maxRtoMs);
            rawSend(entry.pkt);
        } finally {
            lock.unlock();
        }
    }

    // -- receiver side --------------------------------------------------------

    void handleDatagram(byte[] pkt, InetAddress from, int fromPort) {
        rxPackets++;
        rxBytes += pkt.length;
        lastRxTimeMs = nowMs();

        if (pkt.length < 4 || (pkt[3] & 0xFF) != Protocol.PKT_MAGIC) {
            rxUnknown++;
            return;
        }

        int ptype = pkt[0] & 0xFF;
        int seq = pkt[1] & 0xFF;
        int ack = pkt[2] & 0xFF;

        lock.lock();
        try {
            if (ptype == Protocol.PKT_DATA || ptype == Protocol.PKT_ACK) {
                // The ack field of incoming DATA packets counts too (piggyback acks),
                // not only type-1 packets.
                processAckField(ack);
            }

            if (ptype == Protocol.PKT_NACK) {
                Pending entry = unacked.get(seq);
                if (entry != null) {
                    entry.sentAtMs = nowMs();
                    entry.tries++;
                    rawSend(entry.pkt);
                }
                return;
            }

            if (ptype != Protocol.PKT_DATA) {
                return;
            }

            byte[] payload = Arrays.copyOfRange(pkt, 4, pkt.length);
            if (seq == recvExpected) {
                deliver(payload);
                recvExpected = (recvExpected + 1) & 0xFF;
                byte[] buffered;
                while ((buffered = recvBuf.remove(recvExpected)) != null) {
                    deliver(buffered);
                    recvExpected = (recvExpected + 1) & 0xFF;
                }
                sendAck(seq);
                badSeqCount = 0;
                synced = true;
            } else if (((seq - recvExpected) & 0xFF) < Protocol.WINDOW) {
                // Ahead of us: hold it, ack it, and ask for the gap.
                recvBuf.put(seq, payload);
                sendAck(seq);
                int missing = recvExpected;
                while (missing != seq) {
                    if (!recvBuf.containsKey(missing)) {
                        sendNack(missing);
                    }
                    missing = (missing + 1) & 0xFF;
                }
            } else {
                handleOldPacket(seq, payload);
            }
        } finally {
            lock.unlock();
        }
    }

    /**
     * A duplicate, or a camera that never stopped its previous session.
     *
     * <p>Before the first in-order packet of a session either is possible, so after a
     * few we adopt the camera's numbering. Afterwards it is always just a duplicate,
     * and the only correct response is another ack.
     *
     * <p>This is the second of the two real failures recorded in
     * {@code docs/PROTOCOL.md} section 6: the first client version treated these
     * duplicates as a stale session and renumbered itself, which flooded the camera
     * with wrong acks and nacks until it went silent about 75 s in. Renumbering is
     * therefore gated on {@code !synced} and can only ever happen before the session
     * is really established.
     */
    private void handleOldPacket(int seq, byte[] payload) {
        rxDuplicates++;
        if (!synced) {
            badSeqCount++;
            if (badSeqCount >= 3) {
                badSeqCount = 0;
                recvBuf.clear();
                recvExpected = seq;
                deliver(payload);
                recvExpected = (recvExpected + 1) & 0xFF;
                synced = true;
            }
        }
        sendAck(seq);
    }

    private void deliver(byte[] payload) {
        MessageHandler h = handler;
        if (h == null) {
            return;
        }
        try {
            h.onMessage(payload);
        } catch (RuntimeException exc) {
            // A broken handler must not kill the link.
        }
    }

    // -- lifecycle ------------------------------------------------------------

    /**
     * Wait up to {@code timeoutMs} for one datagram. Returns false on timeout.
     *
     * <p>A timeout of zero or less is clamped to 1 ms: Java reads
     * {@code setSoTimeout(0)} as "block for ever", which is never what a caller
     * polling a socket means.
     */
    public boolean receiveOnce(int timeoutMs) {
        try {
            sock.setSoTimeout(Math.max(1, timeoutMs));
        } catch (IOException exc) {
            sockErrors++;
            return false;
        }
        DatagramPacket packet = new DatagramPacket(rxBuffer, rxBuffer.length);
        try {
            sock.receive(packet);
        } catch (SocketTimeoutException exc) {
            return false;
        } catch (IOException exc) {
            // Windows and Linux both surface ICMP unreachable here, often for a
            // datagram we sent seconds ago. Not fatal.
            sockErrors++;
            try {
                Thread.sleep(50);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
            }
            return false;
        }

        InetAddress from = packet.getAddress();
        int fromPort = packet.getPort();
        if (from == null || !from.equals(peerAddr)) {
            return false; // a datagram from an unexpected host
        }
        if (fromPort != peerPort) {
            // The camera replied from a different port; adopt it.
            peerPort = fromPort;
        }
        byte[] data = Arrays.copyOfRange(packet.getData(), packet.getOffset(),
                                         packet.getOffset() + packet.getLength());
        handleDatagram(data, from, fromPort);
        return true;
    }

    public void close() {
        try {
            sock.close();
        } catch (RuntimeException exc) {
            // closing twice is not interesting
        }
    }
}
