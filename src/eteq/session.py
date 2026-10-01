"""Driving one camera: handshake, streaming, reconnection, control.

The sequence mirrors what the vendor app does, because that is the only sequence
the firmware is known to accept:

1. ``GET AllInfo`` and wait briefly for the acknowledgement.
2. ``SET`` with ``Video=1`` plus the picture parameters. The camera answers
   ``Ret=1`` and starts sending.
3. Acknowledge every packet, and send the ``GetSnapPhoto`` user command once a
   second, which is also how the camera reports its physical snapshot button.
4. On exit, ``SET Video=0`` and close the socket.

If the camera stops talking we tear the session down and start a new one, which
is what the phone app effectively does through its own one-second timer.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import protocol as P
from .discovery import resolve_camera
from .mp4 import Frame, H264Framer
from .sinks import Sink, make_recorder
from .transport import Transport

log = logging.getLogger(__name__)


@dataclass
class CameraSettings:
    """What we ask the camera for when a session starts."""

    width: int = 640
    height: int = 240
    fps: int = 20
    bitrate: int = 2048
    zoom: int = 0
    brightness: int = 128
    contrast: int = 4
    saturation: int = 4
    flipmirror: int = 3
    infrared: int | None = None
    audio: bool = True
    extra: dict[str, int] = field(default_factory=dict)
    minimal: bool = False
    """Send only ``Video=1``, for cameras that reject the full parameter list."""

    def start_items(self) -> list[tuple[str, int]]:
        if self.minimal:
            return [("Video", 1)]
        pairs: list[tuple[str, int]] = [
            ("Audio", 1 if self.audio else 0),
            ("Video", 1),
            ("FrameSize", P.frame_size_value(self.width, self.height)),
            ("FrameRate", self.fps),
            ("BitRate", self.bitrate),
            ("Zoom", self.zoom),
            ("Brightness", self.brightness),
            ("Contrast", self.contrast),
            ("Saturation", self.saturation),
            ("FlipMirror", self.flipmirror),
        ]
        if self.infrared is not None:
            pairs.append(("Infrared", self.infrared))
        pairs.extend(self.extra.items())
        return pairs

    def as_dict(self) -> dict[str, int]:
        out = {
            "FrameSize": P.frame_size_value(self.width, self.height),
            "FrameRate": self.fps,
            "BitRate": self.bitrate,
            "Zoom": self.zoom,
            "Brightness": self.brightness,
            "Contrast": self.contrast,
            "Saturation": self.saturation,
            "FlipMirror": self.flipmirror,
        }
        if self.infrared is not None:
            out["Infrared"] = self.infrared
        out.update(self.extra)
        return out

    def apply(self, params: dict[str, int]) -> None:
        """Record a change made at runtime so the UI and reconnects stay in step."""
        for key, value in params.items():
            if key == "FrameSize":
                self.width, self.height = P.parse_frame_size(value)
            elif key == "FrameRate":
                self.fps = value
            elif key == "BitRate":
                self.bitrate = value
            elif key == "Zoom":
                self.zoom = value
            elif key == "Brightness":
                self.brightness = value
            elif key == "Contrast":
                self.contrast = value
            elif key == "Saturation":
                self.saturation = value
            elif key == "FlipMirror":
                self.flipmirror = value
            elif key == "Infrared":
                self.infrared = value
            else:
                self.extra[key] = value


@dataclass
class SessionOptions:
    ip: str | None = None
    cam_port: int = P.CAM_PORT_DEFAULT
    local_port: int = 50000
    discover: bool = True
    discover_timeout: float = 4.0
    use_connect: bool = False
    seq_start: int = 0
    skip_allinfo: bool = False
    heartbeat: float = 1.0
    send_heartbeat: bool = True
    ack_timeout: float = 3.0
    idle_timeout: float = 4.0
    reconnect: bool = True
    reconnect_delay: float = 1.0
    send_stop: bool = True
    duration: float = 0.0
    stats_interval: float = 5.0
    dump_packets: int = 8
    dump_info: int = 6
    video_timeout: float = 5.0
    """Restart if the video stops for this long, even while the camera still talks.

    The heartbeat is answered once a second whether or not video is flowing, so a
    plain "heard nothing" timer never fires when the encoder dies. On the WIC-100
    that happens the moment certain settings are changed.
    """
    live_settings: bool = False
    """Try to change settings without restarting the stream.

    The reference camera refuses most mid-stream changes with ``Ret=0`` and stops
    encoding altogether on others, so by default a change restarts the session.
    """


class CameraSession:
    """Owns the transport, the frame assembly and every sink."""

    def __init__(
        self,
        options: SessionOptions,
        settings: CameraSettings,
        sinks: list[Sink] | None = None,
        hub: Any | None = None,
    ) -> None:
        self.options = options
        self.settings = settings
        self.sinks: list[Sink] = sinks or []
        self.hub = hub

        self.transport: Transport | None = None
        self.camera_ip: str | None = None
        self.beacon = None

        self.framer = H264Framer()
        self._ts_offset = 0
        self._last_ts = -1

        # counters
        self.sessions = 0
        self.stream_packets = 0
        self.video_frames = 0
        self.keyframes = 0
        self.video_bytes = 0
        self.audio_chunks = 0
        self.audio_bytes = 0
        self.snapshot_presses = 0
        self.set_acked = False
        self.get_acked = False
        self.last_set_ret: bytes | None = None
        self.settings_refused = 0
        self.last_frame_at: float | None = None
        self.allinfo: bytes | None = None
        self.user_acks: list[bytes] = []
        self.started_at = time.monotonic()
        self._info_dumps_left = options.dump_info

        # control from the HTTP thread
        self._pending: dict[str, int] = {}
        self._pending_lock = threading.Lock()
        self._recorder: Sink | None = None
        self._recorder_path: str | None = None
        self._stop = threading.Event()

    # -- control surface used by the HTTP server -----------------------------

    def request_set(self, params: dict[str, int]) -> None:
        """Queue a parameter change; the session loop sends it."""
        with self._pending_lock:
            self._pending.update(params)

    def toggle_record(self, path_hint: str | None = None) -> dict[str, Any]:
        """Start or stop recording. Returns the new state."""
        if self._recorder is not None:
            rec, self._recorder = self._recorder, None
            path, self._recorder_path = self._recorder_path, None
            try:
                rec.close()
            except Exception:
                log.exception("closing the recording failed")
            self.sinks = [s for s in self.sinks if s is not rec]
            return {"recording": False, "path": path}

        path = path_hint or time.strftime("eteq-%Y%m%d-%H%M%S.mp4")
        try:
            rec = make_recorder(path, default_fps=float(self.settings.fps or 30))
        except Exception as exc:
            log.error("cannot start recording to %s: %s", path, exc)
            return {"recording": False, "error": str(exc)}
        self._recorder = rec
        self._recorder_path = path
        self.sinks.append(rec)
        log.info("recording to %s", path)
        return {"recording": True, "path": path}

    def status(self) -> dict[str, Any]:
        t = self.transport
        now = time.monotonic()
        muxer = getattr(self.hub, "muxer", None)
        return {
            "camera": list(t.peer) if t else None,
            "sessions": self.sessions,
            "uptime_s": round(now - self.started_at, 1),
            "rx_packets": t.rx_packets if t else 0,
            "rx_bytes": t.rx_bytes if t else 0,
            "rx_duplicates": t.rx_duplicates if t else 0,
            "tx_packets": t.tx_packets if t else 0,
            "unacked": len(t.unacked) if t else 0,
            "last_rx_age_s": round(now - t.last_rx_time, 2) if t and t.last_rx_time else None,
            "stream_packets": self.stream_packets,
            "video_frames": self.video_frames,
            "keyframes": self.keyframes,
            "video_bytes": self.video_bytes,
            "audio_chunks": self.audio_chunks,
            "snapshot_button_presses": self.snapshot_presses,
            "set_acked": self.set_acked,
            "get_acked": self.get_acked,
            "settings_refused": self.settings_refused,
            "last_frame_age_s": round(now - self.last_frame_at, 2) if self.last_frame_at else None,
            "recording": self._recorder is not None,
            "recording_path": self._recorder_path,
            "width": muxer.width if muxer is not None and muxer.ready else 0,
            "height": muxer.height if muxer is not None and muxer.ready else 0,
            "clients": self.hub.client_count if self.hub is not None else 0,
            "params": self.settings.as_dict(),
        }

    def stop(self) -> None:
        self._stop.set()

    # -- incoming data -------------------------------------------------------

    def _on_message(self, payload: bytes) -> None:
        msg = P.parse_message(payload)
        if msg is None:
            log.warning("payload that is not a message (%d bytes):\n%s", len(payload), P.hexdump(payload))
            return

        if msg.code == P.CODE_STREAM:
            chunk = P.parse_stream_chunk(msg)
            if chunk is not None:
                self._on_chunk(chunk)
        elif msg.code == P.CODE_SET_ACK:
            # Ret is a verdict, not a receipt: 1 means the camera took the
            # setting, 0 means it threw it away. The reference camera answers 0
            # for most changes made while it is already streaming.
            ret = msg.get("Ret")
            self.set_acked = True
            self.last_set_ret = ret
            if ret == b"0":
                self.settings_refused += 1
                log.warning("the camera refused that setting (Ret=0)")
            else:
                log.info("the camera accepted the setting (Ret=%s)", (ret or b"?").decode(errors="replace"))
        elif msg.code == P.CODE_GET_ACK:
            self.get_acked = True
            value = msg.get("AllInfo")
            if value is not None:
                self.allinfo = value
                nonzero = sum(1 for b in value if b)
                log.info(
                    "AllInfo: %d bytes, %d of them non-zero%s",
                    len(value),
                    nonzero,
                    "" if nonzero else " (this camera reports nothing here)",
                )
        elif msg.code == P.CODE_USR_ACK:
            self.user_acks.append(msg.body)
            if msg.body.strip() == P.SNAPSHOT_PRESSED:
                self.snapshot_presses += 1
                log.info("the camera's snapshot button was pressed")
            else:
                log.debug("user ack: %r", msg.body)
        else:
            log.info("message %s: %s", msg.code.decode(errors="replace"), msg.items[:4])

    def _on_chunk(self, chunk: P.StreamChunk) -> None:
        self.stream_packets += 1
        if self._info_dumps_left > 0 and chunk.info is not None:
            self._info_dumps_left -= 1
            log.info(
                "stream chunk: %s Info=%s Data=%d bytes",
                chunk.media_type.decode(errors="replace"),
                chunk.info.as_list(),
                len(chunk.data),
            )
        if not chunk.data:
            return
        if chunk.is_audio:
            self.audio_chunks += 1
            self.audio_bytes += len(chunk.data)
            return

        self.video_bytes += len(chunk.data)
        ts = chunk.info.timestamp_ms if chunk.info is not None else None
        for frame in self.framer.push(chunk.data, ts):
            self._emit(frame)

    def _emit(self, frame: Frame) -> None:
        """Hand one access unit to every sink on a monotonic timeline.

        The camera restarts its millisecond clock each session, so reconnecting
        would otherwise rewind time and stall a browser's media buffer.
        """
        ts = frame.timestamp_ms + self._ts_offset
        if ts <= self._last_ts:
            self._ts_offset += self._last_ts - ts + 33
            ts = frame.timestamp_ms + self._ts_offset
        self._last_ts = ts
        frame = Frame(data=frame.data, timestamp_ms=ts, is_keyframe=frame.is_keyframe)

        self.video_frames += 1
        self.last_frame_at = time.monotonic()
        if frame.is_keyframe:
            self.keyframes += 1

        if self.hub is not None:
            try:
                self.hub.add_frame(frame)
            except Exception:
                log.exception("the streaming hub rejected a frame")
        for sink in list(self.sinks):
            try:
                sink.write_frame(frame)
            except Exception:
                log.exception("sink %s failed; removing it", sink.name)
                self.sinks.remove(sink)

    # -- the loop ------------------------------------------------------------

    def run(self) -> int:
        opts = self.options
        self.camera_ip, self.beacon = resolve_camera(opts.ip, opts.discover, opts.discover_timeout)
        log.info(
            "camera %s:%d, local UDP port %s",
            self.camera_ip,
            opts.cam_port,
            opts.local_port or "automatic",
        )
        try:
            while not self._stop.is_set():
                outcome = self._run_once()
                if outcome == "restart":
                    # Deliberate: stop cleanly, then start again straight away.
                    self._close_transport(send_stop=True)
                    continue
                if outcome != "reconnect":
                    return int(outcome)
                if not opts.reconnect or self._stop.is_set():
                    return 3
                self._close_transport(send_stop=False)
                log.warning("camera went quiet; reconnecting in %.1fs", opts.reconnect_delay)
                if self._stop.wait(opts.reconnect_delay):
                    return 0
            return 0
        except KeyboardInterrupt:
            log.info("interrupted")
            return 0
        finally:
            self._close_transport(send_stop=opts.send_stop)

    def _close_transport(self, send_stop: bool) -> None:
        t = self.transport
        if t is None:
            return
        if send_stop:
            try:
                log.info("sending Video=0 to stop the stream")
                t.send_data(P.build_stop())
                self._pump(lambda: False, 0.3)
            except Exception:
                pass
        t.close()
        self.transport = None

    def _run_once(self):
        opts = self.options
        self.sessions += 1
        self.set_acked = self.get_acked = False
        self.last_frame_at = None
        self.framer = H264Framer()

        self.transport = Transport(
            self.camera_ip,
            opts.cam_port,
            opts.local_port,
            use_connect=opts.use_connect,
            seq_start=opts.seq_start,
            dump_packets=opts.dump_packets if self.sessions == 1 else 1,
        )
        t = self.transport
        t.on_message = self._on_message
        started = time.monotonic()

        if not opts.skip_allinfo:
            log.info("asking the camera for AllInfo")
            t.send_data(P.build_get_allinfo())
            self._pump(lambda: self.get_acked, opts.ack_timeout)
            if not self.get_acked:
                log.warning("no answer to AllInfo in %.1fs; continuing anyway", opts.ack_timeout)

        start_items = self.settings.start_items()
        log.info("starting the stream: %s", ", ".join(f"{k}={v}" for k, v in start_items))
        t.send_data(P.build_set(start_items))
        self._pump(lambda: self.set_acked, opts.ack_timeout)
        if not self.set_acked:
            log.warning("the camera did not acknowledge the start command; listening anyway")

        last_hb = last_stats = time.monotonic()
        while not self._stop.is_set():
            self._pump(lambda: False, 0.25)
            now = time.monotonic()

            if t.link_lost:
                log.error("no acknowledgement for %d retries; the link is gone", t.max_retries)
                return "reconnect"

            if self._flush_pending():
                return "restart"

            if opts.send_heartbeat and now - last_hb >= opts.heartbeat:
                last_hb = now
                t.send_data(P.build_user_command(P.HEARTBEAT_UDC))

            if opts.stats_interval and now - last_stats >= opts.stats_interval:
                last_stats = now
                self._log_stats()

            if opts.duration and now - self.started_at >= opts.duration:
                log.info("requested duration reached")
                return 0

            quiet = (now - t.last_rx_time) if t.last_rx_time else (now - started)
            if quiet > opts.idle_timeout:
                log.error("nothing from the camera for %.0fs", opts.idle_timeout)
                return "reconnect"

            # The camera keeps answering the heartbeat after its encoder stops,
            # so silence alone is not enough to notice a dead picture.
            if self.last_frame_at and now - self.last_frame_at > opts.video_timeout:
                log.error(
                    "the picture stopped %.0fs ago although the camera is still answering; restarting",
                    now - self.last_frame_at,
                )
                return "restart"
        return 0

    def _flush_pending(self) -> bool:
        """Apply queued parameter changes. Returns True if the session must restart."""
        with self._pending_lock:
            if not self._pending:
                return False
            params = self._pending
            self._pending = {}
        log.info("applying %s", ", ".join(f"{k}={v}" for k, v in params.items()))
        self.settings.apply(params)
        if self.options.live_settings:
            if self.transport is not None:
                self.transport.send_data(P.build_set(list(params.items())))
            return False
        log.info("restarting the stream so the change takes effect")
        return True

    def _log_stats(self) -> None:
        t = self.transport
        if t is None:
            return
        age = (time.monotonic() - t.last_rx_time) if t.last_rx_time else None
        log.info(
            "rx %d packets / %.1f MB (%d repeats), tx %d, frames %d (%d key), audio %d, "
            "unacked %d, last packet %s ago",
            t.rx_packets,
            t.rx_bytes / 1e6,
            t.rx_duplicates,
            t.tx_packets,
            self.video_frames,
            self.keyframes,
            self.audio_chunks,
            len(t.unacked),
            f"{age:.1f}s" if age is not None else "never",
        )

    def _pump(self, done, timeout: float) -> bool:
        """Service the socket until ``done()`` or the timeout expires."""
        t = self.transport
        if t is None:
            return False
        deadline = time.monotonic() + timeout
        while True:
            if done():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return done()
            t.receive_once(min(0.02, remaining))
            t.retransmit_tick()

    def close(self) -> None:
        for sink in self.sinks:
            try:
                sink.close()
            except Exception:
                pass
        self.sinks = []
