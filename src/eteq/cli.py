"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser

from . import __version__
from . import protocol as P
from .discovery import listen_beacons
from .session import CameraSession, CameraSettings, SessionOptions

log = logging.getLogger("eteq")

EPILOG = """\
examples:
  eteq                          find the camera, open the picture in your browser
  eteq --player ffplay          use a native ffplay window instead
  eteq --record clip.mp4        watch and record at the same time
  eteq --probe                  report which settings this camera really honours
  eteq --convert in.h264 out.mp4    repackage an old capture, no ffmpeg needed
  eteq --install-firewall-rule  let the camera's video through Windows Firewall

The camera makes its own WiFi network. Join it first (often "WIFICAMERA",
password 88888888), then run this. Only one client may be connected at a time,
so close the phone app before starting.
"""


def parse_size(text: str) -> tuple[int, int]:
    try:
        width, _, height = text.lower().partition("x")
        return int(width), int(height)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected something like 640x240, got {text!r}") from None


def parse_set(text: str) -> tuple[str, int]:
    key, sep, value = text.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError(f"expected Key=Value, got {text!r}")
    try:
        return key.strip(), int(value, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eteq",
        description="Live video from OV780-family WiFi inspection cameras.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"eteq {__version__}")

    g = p.add_argument_group("finding the camera")
    g.add_argument("--ip", help="camera address; skips discovery")
    g.add_argument("--no-discover", dest="discover", action="store_false", help="do not wait for the beacon")
    g.add_argument("--discover-timeout", type=float, default=4.0, metavar="SEC")
    g.add_argument("--cam-port", type=int, default=P.CAM_PORT_DEFAULT, metavar="PORT")
    g.add_argument(
        "--local-port",
        type=int,
        default=50000,
        metavar="PORT",
        help="local UDP port to bind, 0 for any (default 50000, matching the firewall rule)",
    )
    g.add_argument("--list-cameras", action="store_true", help="list every camera that beacons, then exit")

    g = p.add_argument_group("watching")
    g.add_argument("--http", dest="http", action="store_true", default=None, help="serve the browser player (default)")
    g.add_argument("--no-http", dest="http", action="store_false", help="do not start the web server")
    g.add_argument("--port", type=int, default=8090, metavar="PORT", help="web server port (default 8090)")
    g.add_argument("--no-open", dest="open_browser", action="store_false", help="do not open a browser window")
    g.add_argument("--player", choices=["none", "ffplay"], default="none", help="also open a native window")
    g.add_argument("--scale", type=float, metavar="N", help="stretch the ffplay window vertically, e.g. 2")
    g.add_argument("--mjpeg", action="store_true", help="also run the ffmpeg MJPEG endpoint for old clients")
    g.add_argument("--mjpeg-quality", type=int, default=4, metavar="N", help="2 best, 31 worst")

    g = p.add_argument_group("recording")
    g.add_argument("--record", metavar="FILE", help="record to .mp4 (built in) or .h264 (raw)")
    g.add_argument("--convert", nargs=2, metavar=("IN.h264", "OUT.mp4"), help="repackage a capture and exit")

    g = p.add_argument_group("picture settings")
    g.add_argument("--size", type=parse_size, default=(640, 240), metavar="WxH")
    g.add_argument("--fps", type=int, default=20, metavar="N")
    g.add_argument("--bitrate", type=int, default=2048, metavar="KBPS")
    g.add_argument("--zoom", type=int, default=0, choices=range(4), metavar="0-3")
    g.add_argument("--brightness", type=int, default=128, metavar="0-255")
    g.add_argument("--contrast", type=int, default=4, metavar="0-7")
    g.add_argument("--saturation", type=int, default=4, metavar="0-7")
    g.add_argument("--flipmirror", type=int, default=3, choices=range(4), metavar="0-3")
    g.add_argument("--infrared", type=int, metavar="0-2", help="LED or infrared, if the camera has one")
    g.add_argument("--set", action="append", default=[], type=parse_set, metavar="Key=Value")
    g.add_argument("--no-audio", action="store_true", help="ask the camera not to send audio")
    g.add_argument("--minimal", action="store_true", help="send only Video=1, for fussy firmware")

    g = p.add_argument_group("protocol tweaks for troubleshooting")
    g.add_argument("--connect", action="store_true", help="use a connected UDP socket, like the vendor app")
    g.add_argument("--seq-start", type=int, default=0, metavar="N")
    g.add_argument("--no-allinfo", action="store_true", help="skip the opening AllInfo request")
    g.add_argument("--no-heartbeat", action="store_true")
    g.add_argument("--heartbeat", type=float, default=1.0, metavar="SEC")
    g.add_argument("--ack-timeout", type=float, default=3.0, metavar="SEC")
    g.add_argument("--idle-timeout", type=float, default=4.0, metavar="SEC")
    g.add_argument(
        "--video-timeout",
        type=float,
        default=5.0,
        metavar="SEC",
        help="restart if the picture stops this long while the camera still answers",
    )
    g.add_argument(
        "--live-settings",
        action="store_true",
        help="change settings without restarting the stream (most cameras refuse)",
    )
    g.add_argument("--no-reconnect", action="store_true")
    g.add_argument("--reconnect-delay", type=float, default=1.0, metavar="SEC")
    g.add_argument("--no-stop", action="store_true", help="do not send Video=0 when quitting")

    g = p.add_argument_group("diagnostics")
    g.add_argument("--probe", action="store_true", help="measure which settings the camera honours")
    g.add_argument("--probe-quick", action="store_true", help="shorter probe, sizes and toggles only")
    g.add_argument("--probe-out", default="eteq-probe.json", metavar="FILE")
    g.add_argument("--dry-run", action="store_true", help="print the packets that would be sent, then exit")
    g.add_argument("--duration", type=float, default=0, metavar="SEC", help="quit after this long")
    g.add_argument("--stats", type=float, default=5.0, metavar="SEC", help="0 disables the periodic summary")
    g.add_argument("--dump-packets", type=int, default=8, metavar="N", help="hex dump the first N packets each way")
    g.add_argument("--dump-info", type=int, default=6, metavar="N")
    g.add_argument("--log", default="eteq.log", metavar="FILE", help="log file, empty string to disable")
    g.add_argument("-v", "--verbose", action="store_true")

    if sys.platform == "win32":
        p.add_argument(
            "--install-firewall-rule",
            action="store_true",
            help="add the Windows Firewall rule the camera needs, then exit",
        )
    return p


def setup_logging(args: argparse.Namespace) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if args.log:
        try:
            handlers.append(logging.FileHandler(args.log, encoding="utf-8"))
        except OSError as exc:
            print(f"cannot write {args.log}: {exc}", file=sys.stderr)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
    logging.getLogger("eteq.server").setLevel(logging.INFO if args.verbose else logging.WARNING)


# -- subcommands --------------------------------------------------------------


def do_firewall() -> int:
    """Allow the camera's UDP traffic through Windows Firewall.

    The hotspot has no internet, so Windows files it under the Public profile and
    silently drops the beacon and anything else unsolicited. The rule is scoped to
    the two ports involved and to private address ranges.
    """
    rule = "eteq camera UDP in"
    command = (
        f'New-NetFirewallRule -DisplayName "{rule}" -Direction Inbound -Protocol UDP '
        f"-LocalPort {P.BEACON_PORT},50000 "
        "-RemoteAddress 192.168.0.0/16,10.0.0.0/8,172.16.0.0/12 -Profile Any -Action Allow"
    )
    print("Adding a Windows Firewall rule. You will see a prompt from Windows.\n")
    print(f"  {command}\n")
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Start-Process powershell -Verb RunAs -Wait -ArgumentList "
                f"'-NoProfile','-Command','{command}'",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        print(f"Could not run it: {exc}")
        return 1
    if completed.returncode != 0:
        print(completed.stderr.strip() or "the elevation prompt was declined")
        print("\nYou can add it by hand from an administrator PowerShell with the command above.")
        return 1
    print("Rule added. Remove it later with:")
    print(f'  Remove-NetFirewallRule -DisplayName "{rule}"')
    return 0


def do_list_cameras(timeout: float) -> int:
    print(f"Listening for camera beacons for {timeout:.0f} seconds...")
    found = listen_beacons(timeout)
    if not found:
        print("\nNothing found. Check that you joined the camera's WiFi, that the camera is on,")
        print("and that the firewall allows UDP 2000 (eteq --install-firewall-rule).")
        return 3
    print()
    for beacon in found:
        print(f"  {beacon.describe()}")
    print(f"\nStart one with:  eteq --ip {found[0].ip}")
    return 0


def do_convert(src: str, dst: str, fps: float) -> int:
    from .mp4 import H264Framer, MP4FileWriter

    if not os.path.exists(src):
        print(f"{src} does not exist")
        return 2
    framer = H264Framer()
    frames = 0
    with open(src, "rb") as fh, MP4FileWriter(dst, default_fps=fps) as writer:
        while True:
            chunk = fh.read(1 << 16)
            if not chunk:
                break
            for frame in framer.push(chunk):
                writer.write_frame(frame)
                frames += 1
        for frame in framer.flush():
            writer.write_frame(frame)
            frames += 1
    size = os.path.getsize(dst)
    print(f"Wrote {frames} frames to {dst} ({size / 1e6:.1f} MB). Play it with any video player.")
    return 0


def do_dry_run(settings: CameraSettings, options: SessionOptions) -> int:
    from .protocol import build_get_allinfo, build_set, build_stop, build_user_command, hexdump

    ip = options.ip or "192.168.2.103"
    packets = [
        ("AllInfo request", build_get_allinfo()),
        ("start stream", build_set(settings.start_items())),
        ("heartbeat", build_user_command(P.HEARTBEAT_UDC)),
        ("stop stream", build_stop()),
    ]
    print(f"Packets that would be sent to {ip}:{options.cam_port}, with sequence numbers 0 upwards.\n")
    for seq, (name, payload) in enumerate(packets):
        pkt = bytes([P.PKT_DATA, seq, 0, P.PKT_MAGIC]) + payload
        print(f"[{name}] {len(pkt)} bytes")
        print(hexdump(pkt, 512))
        print()
    return 0


# -- the normal path ----------------------------------------------------------


def run_live(args: argparse.Namespace) -> int:
    from .server import CameraHTTPServer, StreamHub
    from .sinks import FFplaySink, MjpegTranscoder, ffmpeg_hint, have, make_recorder

    settings = CameraSettings(
        width=args.size[0],
        height=args.size[1],
        fps=args.fps,
        bitrate=args.bitrate,
        zoom=args.zoom,
        brightness=args.brightness,
        contrast=args.contrast,
        saturation=args.saturation,
        flipmirror=args.flipmirror,
        infrared=args.infrared,
        audio=not args.no_audio,
        extra=dict(args.set),
        minimal=args.minimal,
    )
    options = SessionOptions(
        ip=args.ip,
        cam_port=args.cam_port,
        local_port=args.local_port,
        discover=args.discover,
        discover_timeout=args.discover_timeout,
        use_connect=args.connect,
        seq_start=args.seq_start,
        skip_allinfo=args.no_allinfo,
        heartbeat=args.heartbeat,
        send_heartbeat=not args.no_heartbeat,
        ack_timeout=args.ack_timeout,
        idle_timeout=args.idle_timeout,
        reconnect=not args.no_reconnect,
        reconnect_delay=args.reconnect_delay,
        send_stop=not args.no_stop,
        duration=args.duration,
        stats_interval=args.stats,
        dump_packets=args.dump_packets,
        dump_info=args.dump_info,
        video_timeout=args.video_timeout,
        live_settings=args.live_settings,
    )

    if args.dry_run:
        return do_dry_run(settings, options)

    if args.probe or args.probe_quick:
        from .probe import run_probe

        return run_probe(options, settings, quick=args.probe_quick, out_path=args.probe_out)

    want_http = args.http if args.http is not None else (args.player == "none")
    hub = StreamHub(default_fps=float(args.fps or 30)) if want_http else None
    session = CameraSession(options, settings, sinks=[], hub=hub)

    transcoder = None
    if args.mjpeg:
        if have("ffmpeg"):
            transcoder = MjpegTranscoder(quality=args.mjpeg_quality, fps=float(args.fps or 30))
            session.sinks.append(transcoder)
        else:
            log.warning("--mjpeg ignored. %s", ffmpeg_hint())

    player = None
    if args.player == "ffplay":
        if have("ffplay"):
            player = FFplaySink(scale=args.scale)
            session.sinks.append(player)
            log.info("ffplay window opened; closing it stops the relay")
        else:
            log.warning("--player ffplay ignored. %s", ffmpeg_hint())

    if args.record:
        session.sinks.append(make_recorder(args.record, default_fps=float(args.fps or 30)))
        log.info("recording to %s", args.record)

    server = None
    if want_http:
        try:
            server = CameraHTTPServer(
                args.port,
                hub,
                status_fn=session.status,
                set_fn=session.request_set,
                record_fn=session.toggle_record,
                mjpeg_fn=(lambda: transcoder),
            )
        except OSError as exc:
            log.error("cannot serve on port %d: %s", args.port, exc)
            return 1
        url = f"http://127.0.0.1:{server.port}/"
        print(f"\n  Watch the camera at: {url}\n")
        if args.open_browser:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    # Stopping when a native player window is closed only makes sense if that was
    # the only output.
    if player is not None and server is None and not args.record:
        def watch_player() -> None:
            while not session._stop.is_set():
                if not player.running():
                    log.info("the ffplay window was closed")
                    session.stop()
                    return
                time.sleep(0.5)

        threading.Thread(target=watch_player, name="player-watch", daemon=True).start()

    try:
        return session.run()
    except KeyboardInterrupt:
        return 0
    finally:
        session.close()
        if server is not None:
            server.close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args)

    if getattr(args, "install_firewall_rule", False):
        return do_firewall()
    if args.list_cameras:
        return do_list_cameras(args.discover_timeout)
    if args.convert:
        return do_convert(args.convert[0], args.convert[1], float(args.fps or 30))

    try:
        return run_live(args)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
