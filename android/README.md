# eteq for Android

The camera, straight on the phone. No computer in the path.

The desktop tool relays video from a laptop that is holding the camera's WiFi.
That works, but every hop adds delay. This app removes the laptop: the phone joins
the camera's hotspot, speaks the same protocol, and hands the frames to the
phone's own hardware decoder. The delay is then the camera's encoder plus one WiFi
hop.

## Installing the built app

1. Download `eteq.apk` from the [releases page](https://github.com/HankFordham/eteq-gardner-bender-wifi-borescope/releases/latest).
2. Open it on the phone. Android will ask whether to allow installing from
   whatever app you downloaded it with; say yes.
3. Join the camera's WiFi, usually `WIFICAMERA` with password `88888888`. Android
   will warn that the network has no internet. **Stay connected anyway.** If it
   asks, choose to keep using it.
4. Open eteq and press Connect.

No account, no permissions prompt, no network access beyond the camera.

## Why the app asks for nothing

It declares four permissions, all of them granted automatically at install:
internet access, the ability to read network state, and the ability to choose
which network a socket uses. It never asks for the camera, the microphone,
location or files. Photos and recordings are written through the system media
store, which needs no permission for an app's own files.

## The one hard part

A phone connected to a network with no internet does not route traffic there.
Android notices the hotspot has no internet, marks it unvalidated, and quietly
sends every socket over mobile data instead, so packets to the camera disappear
while WiFi still shows as connected.

The app asks for the WiFi network explicitly with the internet requirement
removed, then binds each socket to that network. See `CameraNetwork.java`. This is
the single most common reason a home-made client for one of these cameras appears
to do nothing at all.

It also means the camera's address does not need discovering: once the app holds
the network, the camera is simply its default gateway.

## Building it yourself

Needs the Android SDK and a JDK 17 or newer. Android Studio provides both.

```bash
cd android
./gradlew assembleDebug            # Linux and macOS
.\gradlew.bat assembleDebug        # Windows
```

The APK lands in `app/build/outputs/apk/debug/`. Create `local.properties` with
your SDK path if Gradle cannot find it:

```
sdk.dir=C:/Users/you/AppData/Local/Android/Sdk
```

Install it over a cable with `adb install -r app/build/outputs/apk/debug/app-debug.apk`.

## How it is put together

| file | what it does |
| --- | --- |
| `net/Protocol.java` | the wire format: commands, acknowledgements, stream chunks |
| `net/Transport.java` | reliable UDP, acknowledgements and retransmission |
| `net/H264Framer.java` | reassembles chunks into complete H.264 access units |
| `net/CameraSession.java` | the handshake, the heartbeat, reconnection |
| `CameraNetwork.java` | holds the camera's WiFi and binds sockets to it |
| `VideoDecoder.java` | hardware decode straight onto the screen |
| `Recorder.java` | writes MP4 without re-encoding |
| `MainActivity.java` | the screen |

Everything under `net/` is plain Java with no Android imports, ported from the
Python in `src/eteq/` and sharing its behaviour exactly, including the awkward
parts the real hardware forced on us. Those are explained in
[docs/PROTOCOL.md](../docs/PROTOCOL.md).

## Known limits

The reference camera only ever produces 640x240, refuses most settings changes
while streaming, and jams until its batteries are pulled if asked for a picture
size it cannot make. The app therefore does not offer those controls. If your
camera is more capable, say so in an issue and they can be added.
