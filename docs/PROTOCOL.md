# eTEQ WIC-100 (Gardner Bender) WiFi inspection camera: network protocol

Reverse engineered on 2026-10-01 from the Android app **WIFI TOOL** (`gbk.cam.wifi` 2.2.3,
`WIFI+TOOL_2.2.3_APKPure.apk`). Everything below comes from static analysis:
Java decompiled with jadx, the native library `libgoswifitool.so` (x86 build)
disassembled with objdump and read by hand. **Confirmed live on 2026-10-01**: the
beacon, the handshake (GET ack, SET ack `Ret1`), and the H.264 stream all matched
this write-up; live-only details (the `Type` item, `Info` meanings, the `Video0`
poll reply, the zero-prefix retransmission quirk) are marked **live** below.
Items still marked **UNVERIFIED** were not exercised yet (audio, other sizes, Infrared).

The working files behind this (the vendor APK, its decompiled Java and the
annotated disassembly of its native library) are deliberately **not** in this
repository: they are the manufacturer's copyrighted material. What follows is a
factual description of a network protocol, written from scratch, which is what
you need in order to interoperate with the device.

---

## 0. Big picture

* The app is **not** a Vitamio/URL player. It is OmniVision's "OV780 WiFi" SDK
  (log tag `ov780wifi`, source path `.../wifi-tools/ov87xx/wifitool-native-8723/jni/`),
  with FFmpeg statically linked only to **decode H.264** and to write MP4 recordings.
* The whole network protocol lives in native code: `udpprotocol.c`, `netsock.c`,
  `thread_stream.c`. Java only hardcodes the camera IP, calls JNI entry points,
  sends a 1-second poll, and draws RGB565 frames the native side hands it.
* Transport: a small **reliable UDP** protocol on **camera port 1000**. The app
  uses one UDP socket, `connect()`ed to `192.168.2.103:1000`, ephemeral local port.
  Every datagram carries a 4-byte header `[type][seq][ack]['v']`.
* Commands are **ASCII text** inside those datagrams:
  `"0010" + <4-char code> + <6 hex digit body length> + body`, where the body is a
  list of items `"%02x%06x%s%s"` = (key length, value length, key, value).
* Video is **H.264 Annex B**, delivered as "0011" messages that each carry an
  `Info` item (7 big-endian uint32) and a `Data` item (up to about 950 bytes).
  Audio (if enabled) comes the same way with stream type 3 (ADPCM).
* There is no RTSP, no HTTP, no MJPEG. The camera does not speak TCP at all.

---

## 1. Discovery beacon (camera -> everyone, UDP 1000 -> 255.255.255.255:2000)

32 bytes, once per second. Observed on the wire:

```
38 37 31 33  c0 a8 02 67  57 49 46 49 43 41 4d 00  00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00
"8713"       192.168.2.103  "WIFICAM" + NUL pad (16 bytes)                   avol  --  width height
```

Layout, from `OVBroadcast.DatagramWorker.run()` (`OVBroadcast.java`, the app's discovery service):

| offset | size | meaning |
|---|---|---|
| 0 | 4 | magic ASCII `8713` (the service checks `'8','7','1','3'`) |
| 4 | 4 | camera IPv4 address, network byte order |
| 8 | 16 | camera name, NUL padded (`WIFICAM`) |
| 24 | 2 | `avol` big-endian 16 (audio volume/alert; 0 here) |
| 26 | 2 | unused |
| 28 | 2 | frame width BE16 (0 on this camera) |
| 30 | 2 | frame height BE16 (0 on this camera) |

If width/height are 0 the app substitutes **640x240** (line 304-307). That is the
only place the camera "tells" the app its picture size and it is 640x240, not 640x480.

**The app does not need the beacon.** `PlayerActivity.checkonline()` (line 1091-1096)
runs every second and unconditionally sets `devinfo[0].ipaddr = "192.168.2.103"`,
name `WIFICAM`, so the stream starts even if no beacon is ever received. The beacon
listener only exists to keep an "online" counter fresh. The app never replies to the
beacon and never broadcasts anything itself.

---

## 2. Transport layer (udpprotocol.c + netsock.c)

### 2.1 Socket

`udp_socket_open(ip, port)` (`netsock.c`, at 0x32270 in the x86 build):
`socket(AF_INET, SOCK_DGRAM)`, `SO_REUSEADDR=1`, `SO_RCVBUF=0x20000`,
`connect(ip, port)` with **port = 1000** (`access_open(ctx, ip, 0x3e8)` at
`dis_thread_stream_ann.txt` 0x2d8c3), then `O_NONBLOCK`. No `bind()`, so the
local port is ephemeral and the camera must reply to whatever source port we used.
Sends are plain `write()`, receives `recv()` / `recv(MSG_PEEK)`.

A TCP `socket_open()` with `TCP_NODELAY` also exists in the library but nothing
calls it for this camera (and all TCP ports are closed), so ignore it.

### 2.2 Datagram header (4 bytes)

Every datagram in both directions:

| byte | meaning |
|---|---|
| 0 | type: `0x00` = DATA (payload follows), `0x01` = ACK, `0x02` = NACK (resend request) |
| 1 | sequence number of this DATA packet (8-bit, wraps), or for ACK/NACK the seq being referred to |
| 2 | ack field: the sender's **next expected** incoming sequence number (cumulative ack) |
| 3 | literal `0x76` `'v'`. Packets without it (or shorter than 4 bytes) are read and discarded |

Maximum datagram is `0x404` = 1028 bytes (4 header + 1024 payload); the receiver
keeps 32 slots of 1028 bytes indexed by `seq & 0x1f`.

### 2.3 Sender rules (what the app does, and what the camera presumably mirrors)

* Sequence numbers start at **0** for a new session (state struct is `malloc`ed and zeroed).
* A DATA packet is kept until an incoming packet (DATA or ACK) carries an ack
  field that has moved past it. The ack field of incoming **DATA** packets is
  honoured too (piggyback acks), not only type-1 packets.
* Retransmission: if the oldest unacked packet is older than the RTO it is resent
  with a fresh ack field. Initial RTO 20 ms, smoothed RTT starts at 10 ms, RTO
  doubles per retry (`rto = (srtt*2) << retries`). The app only retransmits when
  exactly one packet is outstanding, because it sends one command and waits.
* Waiting for a reply polls the socket every 1 ms, gives up after 1000 polls
  (about 1 s) with "packet recv none".

### 2.4 Receiver rules

* Expected next seq starts at 0, window end at 32.
* In-order DATA packet: deliver payload, `expected++`, then send
  `ACK = [0x01][seq][expected]['v']` (4 bytes).
* Out-of-order DATA packet inside the window: store in its slot, send the same ACK
  form, and for every empty slot between `expected` and that seq send
  `NACK = [0x02][missing_seq][expected]['v']`.
* Duplicate/old packet: re-send ACK.
* The camera must ACK every packet we send and we must ACK every packet it
  sends, otherwise retransmissions pile up and the side that stops hearing acks
  eventually drops the session ("connection no data, goto exit!!!, lost" after
  9 s without payload bytes, checked once per second in `thread_stream`).

---

## 3. Message layer (text inside DATA payloads)

### 3.1 Framing

```
"0010" <code:4 ASCII> <len:6 hex ASCII> <body:len bytes>
```

`code` identifies the message and its direction. Codes seen in the binary:

| code | direction | meaning |
|---|---|---|
| `0006` | app -> camera | **SET** parameters (also starts/stops the stream via `Video`) |
| `0007` | camera -> app | SET ack. Body item `Ret` = `1` means OK (`COMMAND_D_S_SET_ACK`) |
| `0008` | app -> camera | **GET** (`AllInfo`) |
| `0009` | camera -> app | GET ack carrying `AllInfo` (`COMMAND_D_S_GET_ACK`) |
| `0011` | camera -> app | **stream chunk**: items `Info` + `Data` (and `Ret`) |
| `0015` | app -> camera | user-defined command, body is raw bytes (`nativeUDC2`) |
| `0016` | camera -> app | user-defined reply, body raw bytes (`COMMAND_D_S_USR_ACK`) |
| `0014` | ? | checked in the ack parser but never generated by the app |

### 3.2 Items

Body = concatenation of items, each built by `sprintf("%02x%06x%s%s", strlen(key), strlen(value), key, value)`
(`udpprotocol.c`, at 0x30717). Integer values are formatted with `"%x"`
(lowercase hex, no padding). Up to 16 items per message. Binary values (`Info`,
`Data`, `AllInfo`) use the same length-prefixed form, so the body is not NUL safe
text, only length-delimited.

Parameter keys (`.data` table at 0x359020, index = JNI parameter index):

| idx | key | set by | value |
|---|---|---|---|
| 0 | `Video` | start/stop | `1` = stream on, `0` = off |
| 1 | `Audio` | start | `1` / `0` |
| 2 | `FrameSize` | `nativeSetRes2(w,h)` | `(w << 16) | h` in hex, e.g. 640x240 = `28000f0`, 320x240 = `14000f0` |
| 3 | `FrameRate` | `nativeSetFrmrate2` | fps in hex (app offers 3,5,10,15,20,25,30) |
| 4 | `BitRate` | `nativeSetBitrate2` | kbit/s in hex (app offers 128..3072) |
| 5 | `Zoom` | `nativeSetZoom2` | 0..3 |
| 6 | `Brightness` | `nativeSetBrightness2` | app default 128 (`80`), buttons step 0..7 |
| 7 | `Contrast` | `nativeSetContrast2` | 0..7, default 4 |
| 8 | `Saturation` | `nativeSetSaturation2` | 0..7, default 4 |
| 9 | `FlipMirror` | `nativeSetFlipmirror2` | 0..3, default 3 |
| 10 | `LightCond` | not used by this app | |
| 11 | `LightFreq` | not used by this app | |
| 12 | `AlertMode` | not used by this app | |
| 13 | `AudioAlertV` | not used by this app | |
| 14 | `Infrared` | `nativeSetInfrared2` | 0..2 (LED / IR illumination, likely) |
| 15 | (raw user bytes) | `nativeUDC2` | sent as code `0015`, not as an item |

### 3.3 Camera -> app message contents

* `0007` SET ack: `03000001Ret1`
* `0009` GET ack: `07<len>AllInfo<0x2b4 bytes>`. The app copies exactly 0x2b4 = 692
  bytes into a struct and byte-swaps it as big-endian 32-bit words: first word =
  count N, then N pairs of words at offset 4, then four 0x5c-byte sub-structs at
  offset 0x144 each starting with a count and a list of words. Most likely the
  supported FrameSize / FrameRate combinations. **UNVERIFIED** beyond the layout.
* `0011` stream chunk, **as observed live** (1028-byte datagrams, 1024-byte payload):
  `0010 0011 0003f2 03000001Ret1 04000005TypeVideo 0400001cInfo<28 bytes> 040003a1Data<929 bytes>`.
  Items: `Ret`=`1`, `Type`=`Video` (presumably `Audio` for sound), `Info` (7 x BE uint32),
  `Data` (929 bytes, less in the last chunk of a frame). Observed `Info` values:

  ```
  [0, 7072, 0, 0, 0, 132, 0]   I-frame, 7072 bytes total, chunk 0
  [2, 0,    1, 1, 0, 132, 0]   ... chunk 1 of the same frame
  [2, 0,    1, 7, 0, 132, 0]   chunk 7 (last, 569 bytes)
  [2, 64,   1, 0, 0, 165, 0]   tiny 64-byte P-frame
  [2, 15200,2, 0, 0, 199, 0]   P-frame, 15200 bytes, chunk 0
  ```

  | Info word | meaning (live) | use in the app's access_read |
  |---|---|---|
  | [0] | frame type: `0` = I-frame, `2` = P-frame (`3` = audio per the SDK) | `<= 1` sets flag 8 (keyframe) |
  | [1] | total bytes of this frame, only in chunk 0, else 0 | not used |
  | [2] | running counter, increments roughly per frame | `== 0` sets flag 4 |
  | [3] | chunk index inside the frame, 0 = first | `== 0` sets flag 1 (frame start) |
  | [4] | always 0 | not used |
  | [5] | timestamp (ms, restarts per session) | passed to the decoder FIFO |
  | [6] | always 0 | not used |

  The app concatenates `Data` of consecutive chunks and feeds the result to
  `avcodec_find_decoder(28)` = **AV_CODEC_ID_H264**. Because H.264 Annex B is
  self-delimiting, concatenating all video `Data` in sequence order and piping it to
  `ffplay -f h264 -` works without interpreting `Info` at all. Live rate at
  640x240 / 20 fps / BitRate 2048: about 150 datagrams/s, 1.1 to 1.4 Mbit/s,
  roughly 30 frame starts per second of which many are tiny 16 to 96 byte P-frames.
* `0016` user reply: raw body. **Live:** the camera answers every `GetSnapPhoto`
  poll, with `05000001Video0` normally; `05000001Video1` is what the app treats as
  "the hardware snapshot button on the camera was pressed" (it then saves the
  current frame locally).
* **Retransmission quirk (live):** when the camera resends a data packet it has
  the `0010` prefix replaced by `00 00 00 00`; everything after is identical.
  The SDK's parser would reject such a packet ("buf_to_ack error"), kill the
  stream thread, and the Java timer would silently restart the session a second
  later. A client must either accept the zero prefix or expect to reconnect.
* **AllInfo (live):** the camera returns 700 bytes that are almost entirely zero
  (about 55 non-zero bytes on the reference unit, varying between sessions), with
  no structure we could decode. Treat the request as a liveness check, not a
  source of information.
* **``Ret`` is a verdict, not a receipt (live).** The ``0007`` acknowledgement
  carries ``Ret=1`` when the camera took the setting and ``Ret=0`` when it threw
  it away. This matters: a client that treats any acknowledgement as success will
  silently believe it changed something it did not. Observed on the reference
  camera, with the stream already running:

  | setting changed mid-stream | answer | what actually happened |
  |---|---|---|
  | `Brightness`, `Contrast`, `Saturation`, `FlipMirror` | `Ret=0` | ignored |
  | `Zoom` | `Ret=1` | accepted, and the encoder stopped for good |
  | `FrameSize`, `FrameRate`, `BitRate` | `Ret=1` | encoder stopped |

  So `Ret=1` means the value was taken, not that the camera still works. Only
  `FrameSize=640x240`, the size it was already using, survived.
* **Settings are a start-up matter (live).** The practical conclusion is that
  these cameras expect their parameters with the ``Video=1`` command and not
  afterwards. Changing anything reliably means stopping and starting the stream.
  One oddity worth recording: asking for 1280x480 produced a sequence parameter
  set describing **256x480**, so the camera does partially act on sizes it cannot
  deliver.
* **A dead encoder still answers (live).** After the encoder stops, the camera
  keeps acknowledging packets and keeps answering the once-a-second
  ``GetSnapPhoto`` user command. Nothing in the transport indicates a problem.
  A client that watches for "no packets" will wait for ever; it has to watch for
  "no video" specifically.
* **Frame timing (live):** ``Info[5]`` is a millisecond presentation clock that
  restarts with each session. Successive frames are 33 to 34 ms apart, so the
  camera encodes at about 30 fps regardless of the ``FrameRate`` it was asked for.
  Nothing in the H.264 itself carries timing, which matters for any consumer: a
  player handed the raw stream has to invent a frame rate, and a muxer handed it
  without one will silently drop almost every frame.

---

## 4. Exact packets the app sends, in order

Session start (`PlayerActivity.devBeginDecoding()` -> `nativeInit2`, parameter setters,
`nativeStart2("192.168.2.103", idx)` -> native `thread_stream` -> `access_open`).
Before `nativeStart2` the setters only store values, they do not send anything.
The native thread then sends the following to **192.168.2.103:1000**.
Hex below is produced by `eteq --dry-run`; the 4-byte header shows
seq 0,1,2,... and ack 0 (ack gets updated to whatever the camera has sent us).

**Packet 1: GET AllInfo** (34 bytes). Body = `item("AllInfo", 1)`.

```
00 00 00 76 30 30 31 30 30 30 30 38 30 30 30 30   ...v001000080000
31 30 30 37 30 30 30 30 30 31 41 6c 6c 49 6e 66   1007000001AllInf
6f 31                                             o1
```
Text: `0010 0008 000010 07000001AllInfo1`. Expected reply: code `0009` with `AllInfo`.
The app waits up to ~1 s and continues even if there is no reply (it only logs the result).

**Packet 2: SET start stream** (195 bytes). Items in this order: `Audio=1`, `Video=1`,
then every parameter whose "changed" bit is set, in index order 2..9 (the app sets
all of these before starting; `Infrared` is only sent if the user changed it).
Values below are what the app uses on this camera after its startup `doSwitch()`:
640x240, 20 fps, 2048 kbit/s, zoom 0, brightness 128, contrast 4, saturation 4, flip 3.

```
00 01 00 76 30 30 31 30 30 30 30 36 30 30 30 30   ...v001000060000
62 31 30 35 30 30 30 30 30 31 41 75 64 69 6f 31   b105000001Audio1
30 35 30 30 30 30 30 31 56 69 64 65 6f 31 30 39   05000001Video109
30 30 30 30 30 37 46 72 61 6d 65 53 69 7a 65 32   000007FrameSize2
38 30 30 30 66 30 30 39 30 30 30 30 30 32 46 72   8000f009000002Fr
61 6d 65 52 61 74 65 31 34 30 37 30 30 30 30 30   ameRate140700000
33 42 69 74 52 61 74 65 38 30 30 30 34 30 30 30   3BitRate80004000
30 30 31 5a 6f 6f 6d 30 30 61 30 30 30 30 30 32   001Zoom00a000002
42 72 69 67 68 74 6e 65 73 73 38 30 30 38 30 30   Brightness800800
30 30 30 31 43 6f 6e 74 72 61 73 74 34 30 61 30   0001Contrast40a0
30 30 30 30 31 53 61 74 75 72 61 74 69 6f 6e 34   00001Saturation4
30 61 30 30 30 30 30 31 46 6c 69 70 4d 69 72 72   0a000001FlipMirr
6f 72 33                                          or3
```
Text: `0010 0006 0000b1 05000001Audio1 05000001Video1 09000007FrameSize28000f0 09000002FrameRate14 07000003BitRate800 04000001Zoom0 0a000002Brightness80 08000001Contrast4 0a000001Saturation4 0a000001FlipMirror3`
Expected reply: `0010 0007 00000c 03000001Ret1`, then `0011` stream chunks start.

A minimal variant is just `0010 0006 00000e 05000001Video1` (`--plain`).

**Packet 3 onward: heartbeat / snapshot-button poll, every 1 s** (38 bytes).
`PlayerActivity.hardGetSnapPhoto()` -> `nativeUDC2("0C000000GetSnapPhoto")` -> code `0015` raw body:

```
00 02 00 76 30 30 31 30 30 30 31 35 30 30 30 30   ...v001000150000
31 34 30 43 30 30 30 30 30 30 47 65 74 53 6e 61   140C000000GetSna
70 50 68 6f 74 6f                                 pPhoto
```
Text: `0010 0015 000014 0C000000GetSnapPhoto`. Reply only when the camera's button
was pressed: `0010 0016 00000e 05000001Video1`. It is not known whether the camera
needs this as a keepalive; the ACKs we send for its video packets are the real
keepalive. **UNVERIFIED**, hence `--no-heartbeat` in the tool.

**Every received DATA packet** is answered with a 4-byte ACK `01 <seq> <next expected> 76`.

**Changing a setting while streaming** (e.g. LED): `access_read` notices a changed
parameter and sends a SET with just that item, e.g. `0010 0006 00000012 08000001Infrared1`
(`08` = len("Infrared"), `000001` = len("1")). Reply `0007 ... Ret1`.

**Changing SSID/password** (`PlayerActivity.java:290-300`): a user command, code `0015`, raw body
`"0400000<n>SSID<ssid>0800000<m>PASSWORD<pw>"` where n/m are single hex digits of
the lengths (two digits when exactly 16). Example for `WIFICAMERA`/`88888888`:
`0010 0015 00002a 0400000aSSIDWIFICAMERA08000008PASSWORD88888888`. Not sent by the tool.

**Stopping**: the app has no stop command. `nativeExit2` asks the threads to exit;
`thread_stream` calls `access_close()` which just `close()`s the socket. The camera
notices the missing ACKs/heartbeats and stops on its own (that is also why the
iOS blurb says only one client at a time). The relay tool additionally sends
`0010 0006 00000e 05000001Video0` on exit, which is the logical "off" value of the
same parameter the app uses to start. **UNVERIFIED** but harmless.

---

## 5. Timing summary

| when | what |
|---|---|
| t=0 | GET AllInfo, wait up to ~1 s (ignores failure) |
| then | SET Audio1 Video1 FrameSize FrameRate BitRate Zoom Brightness Contrast Saturation FlipMirror, wait up to ~1 s |
| continuous | read stream, ACK every packet immediately |
| every 1 s | UDC `0C000000GetSnapPhoto` |
| every 1 s | link check: if no payload bytes for > 9 s, declare link lost and tear down |
| exit | close socket (no message) |

---

## 6. Live test results (2026-10-01, five sessions, longest 79 s)

Confirmed: beacon from 192.168.2.103:1000 exactly as captured; GET AllInfo
acked (`01 00 01 76`) and answered with 700 zero bytes; the 195-byte SET acked
and answered `0010 0007 00000c 03000001Ret1`; H.264 chunks started immediately
and ffplay showed smooth video; the camera replies from port 1000; every
`GetSnapPhoto` poll is answered with `05000001Video0`.

Observed problems, all on the client side: the camera occasionally retransmits
packets the client already acked (likely a lost ACK over WiFi), and those
retransmissions carry the zero prefix. The first relay version rejected the zero
prefix and, worse, treated the duplicates as a "stale session" and renumbered
itself, which flooded the camera with wrong acks and nacks until it went silent
about 16 s later. Fixed by accepting the zero prefix, never renumbering after the
first in-order packet, and reconnecting automatically after 4 s of silence
(which is what the Android app does too, through its 1-second timer).

Still **UNVERIFIED**: audio chunk format (none seen; `Audio1` was sent but the camera
sent no `TypeAudio` chunks, so there may be no microphone), the other `FrameSize`
values including 640x480 (`28001e0`), `Infrared`/`LightCond`, and `Video0` as a
stop command (the camera acked it with `Ret1`, but it was always followed by closing
the socket, so its effect alone is unknown).
