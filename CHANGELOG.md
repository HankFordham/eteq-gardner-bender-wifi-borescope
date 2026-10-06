# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- An Android app under `android/`. The phone joins the camera's WiFi and speaks
  the protocol itself, decoding in hardware straight to the screen, so no computer
  is involved and the delay is as low as the hardware allows. It records to MP4
  without re-encoding and saves photos, and asks for no sensitive permissions.
- `--lan` serves the viewer to other devices, so a phone or tablet can watch while
  the computer stays connected to the camera. Protected by a generated access key,
  and the firewall helper now opens the viewer port too.

### Fixed

- Several causes of delay and judder in the Android app: frames are now completed
  from the size the camera declares rather than by waiting for the next frame to
  start, the decoder runs on callbacks instead of blocking, the picture goes to a
  SurfaceView, the WiFi radio is held out of power saving, and motion is paced to
  the camera's cadence with a button to turn that off.
- The probe no longer reports dozens of false negatives after a camera jams. It
  stops at that point, says so, and tests the picture settings before the sweeps
  that are known to jam the reference camera.

## [1.0.0] - 2026-10-01

First public release.

### Added

- Full implementation of the OmniVision OV780 camera protocol: handshake,
  reliable-UDP transport with acknowledgements and retransmission, and
  reassembly of fragmented H.264 frames.
- Built-in browser player. The tool wraps the live H.264 stream in fragmented
  MP4 in pure Python and serves it, so watching the camera needs nothing but a
  browser. The page tries Media Source Extensions first, falls back to ordinary
  progressive playback, then to MJPEG, and explains itself if none of them work.
- `ffplay` output mode and an MJPEG output mode (both optional; they are the
  only features that use ffmpeg if it is installed).
- Recording live video straight to `.mp4` or raw `.h264`, with the MP4 written
  by the same built-in muxer, so recording needs no ffmpeg either.
- `--convert` to repackage an existing raw capture as a playable MP4 offline.
- Camera discovery on the local network, so the camera's address does not have
  to be supplied by hand.
- Automatic reconnection when the camera drops the link or power-cycles.
- Parameter probe mode (`eteq --probe`) that measures which settings a camera
  actually honours, by checking the requested picture size against the H.264
  sequence parameter set and the requested rate against delivered frames. These
  cameras acknowledge almost anything, so measuring is the only way to tell.
  Its report is what to attach when reporting a new model.
- Camera controls from the browser page: brightness, contrast, saturation, zoom,
  flip/mirror, LED/infrared, picture size, frame rate and bitrate, plus PNG
  snapshots taken in the browser.
- Windows firewall helper that adds the inbound rule the camera's UDP stream
  needs.

[Unreleased]: https://github.com/HankFordham/eteq-gardner-bender-wifi-borescope/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/HankFordham/eteq-gardner-bender-wifi-borescope/releases/tag/v1.0.0
