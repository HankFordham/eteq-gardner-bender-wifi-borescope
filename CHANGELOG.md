# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
