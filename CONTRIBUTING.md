# Contributing to eteq

Thanks for helping out. The most valuable contribution is usually a **camera
report** -- see below -- because it is how the compatibility list grows.

## Setting up

eteq is pure standard-library Python; the only dependencies are development
tools.

```sh
git clone https://github.com/HankFordham/eteq-gardner-bender-wifi-borescope
cd eteq-gardner-bender-wifi-borescope
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -e ".[dev]"
```

## Running the tests

```sh
pytest
```

The test suite is entirely offline: it needs no camera, no network and no
ffmpeg.

## Linting

```sh
ruff check .
ruff check --fix .     # apply the automatic fixes
```

CI runs `ruff check .` and `pytest -q` on Linux and Windows across Python 3.9,
3.11 and 3.13. Please make sure both pass locally first.

## Developing without hardware

A simulator replays the camera side of the protocol, so you can work on
transport, reassembly and the browser player without owning a camera.

In one terminal:

```sh
python -m eteq.simulator
```

In another:

```sh
eteq --ip 127.0.0.1
```

## Reporting a new camera model

The OV780 WiFi module is sold under many brands. If you have a device that is
not on the compatibility list -- **whether it works or not** -- please open a
*Camera report* issue. Include:

1. The output of:

   ```sh
   eteq --probe
   ```

2. Your log. Run with logging turned up and attach the file (or the relevant
   excerpt):

   ```sh
   eteq --verbose --log eteq.log
   ```

3. The brand and model printed on the device, and the Wi-Fi SSID it
   broadcasts.

Reports of cameras that *fail* are just as useful as reports of cameras that
work -- a probe dump from a device that never streams is often enough to add
support.

## What not to contribute

Please **do not** attach or commit:

- Vendor Android apps (`.apk` files) or any other vendor binaries.
- Decompiled or disassembled vendor code (for example jadx output, Ghidra or
  IDA exports, Java sources recovered from an APK).
- Camera firmware images or firmware update payloads.

That material is copyrighted by the vendor and the project cannot host or
redistribute it. Protocol *descriptions* -- field layouts, message sequences,
observed behaviour, packet hexdumps from your own device -- are welcome and
belong in `docs/PROTOCOL.md`. Issues or pull requests carrying vendor
artefacts will be closed without review.

## Pull requests

- Keep the runtime dependency list empty. ffmpeg stays optional.
- Add or update a test when you change protocol or parsing behaviour.
- Add a line to `CHANGELOG.md` under `## [Unreleased]`.
- By contributing you agree your work is released under the MIT license (see
  `LICENSE`).
