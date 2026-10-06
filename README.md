# eteq

**Gardner Bender eTEQ WIC-100 and other WiFi borescopes, on your computer, without the phone app.**

These cheap WiFi inspection cameras make their own hotspot and speak a proprietary
UDP protocol, so no normal player can open them. This one can.

`eteq` speaks that protocol and hands you the picture in your browser, in a
native window, or as a file.

Developed against a **Gardner Bender eTEQ WIC-100**. The same OmniVision OV780
module is sold under many other names, so other units have a fair chance of
working. If yours does, or does not, please
[tell us](https://github.com/HankFordham/eteq-gardner-bender-wifi-borescope/issues/new/choose).

```
                 WiFi hotspot                  localhost
   [ camera ] ──── UDP :1000 ────▶ [ eteq ] ──── :8090 ────▶ [ your browser ]
```

## What you get

- **Live picture in a browser**, with no transcoding. The camera's own H.264 is
  rewrapped into fragmented MP4 in a few hundred lines of Python and played by the
  browser's own decoder.
- **No required dependencies.** Python standard library only. ffmpeg is optional
  and only needed for two extra output modes.
- **Recording** straight to `.mp4`, or to raw `.h264`.
- **Camera controls**: brightness, contrast, saturation, zoom, flip/mirror, the
  LED, picture size, frame rate and bitrate.
- **Snapshots** as PNG, taken in the browser.
- **Automatic reconnection** when the camera drops out, which it does.
- **A probe mode** that measures which settings your particular camera actually
  honours, because they all claim to accept everything.

## Quick start

### Windows, no Python

1. Download `eteq.exe` from the [latest release](https://github.com/HankFordham/eteq-gardner-bender-wifi-borescope/releases/latest).
2. Open PowerShell where you saved it and allow the camera through the firewall,
   once:

   ```powershell
   .\eteq.exe --install-firewall-rule
   ```

3. Join the camera's WiFi network. On the WIC-100 it is called `WIFICAMERA` and
   the password is `88888888`. Windows will warn that there is no internet. That is
   expected.
4. Run it:

   ```powershell
   .\eteq.exe
   ```

   Your browser opens at <http://127.0.0.1:8090/> and the picture appears.

### Any platform, with Python

```bash
pip install eteq-cam        # or: pip install -e . from a clone
eteq
```

Python 3.10 or newer. On Linux and macOS no firewall change is usually needed.

## Usage

```
eteq                             find the camera and open it in a browser
eteq --player ffplay             a native low-latency window instead (needs ffmpeg)
eteq --record clip.mp4           watch and record at the same time
eteq --record clip.h264          record the camera's bytes untouched
eteq --list-cameras              show every camera that is beaconing
eteq --lan                       also let a phone on your network watch
eteq --probe                     measure which settings this camera honours
eteq --convert old.h264 new.mp4  repackage an old capture, no ffmpeg needed
eteq --size 320x240 --fps 10     ask for a different picture
eteq --help                      everything else
```

While the browser page is open:

| key | action |
| --- | --- |
| `s` | save a PNG snapshot |
| `r` | start or stop recording |
| `f` | fullscreen |

Other endpoints on the same port, useful for scripting or for VLC:

| URL | what it is |
| --- | --- |
| `/stream.mp4` | live fragmented MP4, plays in VLC directly |
| `/stream.h264` | the raw camera bytes |
| `/api/status` | counters as JSON |
| `/mjpeg` | MJPEG for old clients, needs `--mjpeg` and ffmpeg |

```powershell
& "C:\Program Files\VideoLAN\VLC\vlc.exe" http://127.0.0.1:8090/stream.mp4
```

## Watching on a phone or tablet

The camera accepts one client at a time and speaks only this protocol, so a phone
cannot talk to it directly. The computer stays connected to the camera and passes
the picture on:

```powershell
.\eteq.exe --lan
```

That prints a link and a short access key. Open the link on the phone. The page is
built for a phone screen, and the snapshot button saves to the phone.

The phone needs a way to reach the computer that is not the camera's own network.
Either of these works:

- **At home:** plug the computer into your router with an Ethernet cable and put
  the phone on your normal WiFi.
- **Anywhere, no router needed:** plug the phone into the computer with a USB
  cable and turn on USB tethering on the phone. That makes a private network
  between just those two devices. The phone does not need mobile data for this.

Run `eteq --install-firewall-rule` once, which also opens the viewer port.

Anyone on that network who has the key can watch and control the camera. Use
`--key` to choose your own, or `--no-key` to drop the check entirely, which is
only sensible on a USB tether where nothing else is connected.

## Does it work with my camera?

If your camera creates a WiFi network and its app is one of the "WiFi Tool",
"WiFi Borescope" or "WiFi Endoscope" family, try it. Run `eteq --list-cameras`
while connected to its network: if a camera answers, the protocol matches.

Known to work:

| Camera | Picture | Notes |
| --- | --- | --- |
| Gardner Bender eTEQ WIC-100 | 640x240 H.264, ~30 fps | the reference device. 640x240 only. Asking for any other size or frame rate jams the encoder until the batteries are pulled |

Please add yours by opening a
[camera report](https://github.com/HankFordham/eteq-gardner-bender-wifi-borescope/issues/new/choose) with the
output of `eteq --probe`.

## Troubleshooting

**Nothing is found.** Confirm you are on the camera's WiFi and that no phone is
connected: these cameras accept one client at a time. On Windows run
`eteq --install-firewall-rule`, because the hotspot is treated as a public network
and inbound UDP is blocked by default. You can always skip discovery with
`eteq --ip 192.168.2.103`.

**The camera is found but no picture arrives.** Try `eteq --minimal`, which sends
only the start command without any picture settings. Some firmware rejects the
full list. Then try `eteq --no-allinfo`, and `eteq --connect`.

**The picture looks squashed or stretched.** The WIC-100 sends 640x240 and its
phone app stretches that to 4:3. Use the Aspect button on the page to switch.

**The picture froze and never came back.** Fixed in 1.0.0: the camera keeps
answering the heartbeat after its encoder stops, so an older build waited for
ever. It now notices that video specifically has stopped and restarts the stream.
If you still see it, raise `--video-timeout`.

**Changing a setting blanks the picture for a second.** That is deliberate. These
cameras refuse most settings changed while they are streaming, and stop encoding
altogether on some, so every change restarts the stream. `--live-settings` sends
changes in place instead, if your camera is one that accepts them.

**It stalls every few seconds.** Check `rx_duplicates` on `/api/status`. Heavy
duplication means a weak radio link; move the camera closer.

**The page says it could not play the video.** The player tries three things in
turn: Media Source Extensions, ordinary progressive playback, then MJPEG. The
`mode` chip in the header shows which one is in use. If all three fail, the
browser cannot decode H.264 at all (some builds ship without it). Open
`http://127.0.0.1:8090/stream.mp4` in VLC instead, or use `--player ffplay`.
Recording is unaffected either way.

Every run also writes `eteq.log` next to wherever you started it, with hex dumps
of the first packets. That is the single most useful thing to attach to an issue.

## How it works

The camera broadcasts a beacon once a second and otherwise waits on UDP port 1000
for a client that speaks its own small reliable-UDP protocol. Commands and
responses are plain text; video comes back as H.264 in roughly 1 KB chunks.

The full wire format, including every byte of the handshake, is in
[docs/PROTOCOL.md](docs/PROTOCOL.md). It was recovered by reading the vendor's
Android app, then confirmed against real hardware.

## Development

```bash
git clone https://github.com/HankFordham/eteq-gardner-bender-wifi-borescope
cd eteq-gardner-bender-wifi-borescope
pip install -e ".[dev]"
pytest
```

No camera needed: there is a simulator that speaks the same protocol.

```bash
python -m eteq.simulator          # terminal 1
eteq --ip 127.0.0.1 --no-discover # terminal 2
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Legal

MIT licensed, see [LICENSE](LICENSE).

This project contains no vendor code. The protocol description was produced by
analysing a freely downloadable copy of the manufacturer's own Android app for
the purpose of interoperability, and is a factual description of a network
protocol. Please do not attach vendor APKs, decompiled vendor source or firmware
images to issues or pull requests.

Not affiliated with, endorsed by, or supported by Gardner Bender, nVent,
OmniVision or any camera manufacturer.
