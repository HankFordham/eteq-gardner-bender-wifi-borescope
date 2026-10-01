"""Find out which parameters a particular camera actually honours.

A camera's answer tells you something but not everything. ``Ret=0`` is a flat
refusal, but ``Ret=1`` only means it took the value, not that anything changed,
and on the reference camera some accepted values stop the encoder outright. So
this probe records the verdict *and* measures the stream while it changes one
parameter at a time:

* ``FrameSize`` is checked against the dimensions in the H.264 sequence parameter
  set, which is the camera's own description of the picture it is encoding.
* ``FrameRate`` is checked against frames actually delivered per second.
* ``BitRate`` is checked against bytes actually delivered per second.
* Anything else is reported as accepted or rejected, with the measured effect on
  average frame size, which is a weak but real signal for things like the LED.

The result is a table for the terminal and a JSON blob to paste into a
compatibility report, so the project can build up a list of what works on which
rebrand.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from . import protocol as P
from .mp4 import Frame, parse_sps
from .session import CameraSession, CameraSettings, SessionOptions

log = logging.getLogger(__name__)


def _find_sps(annexb: bytes) -> bytes | None:
    """Return the SPS NAL payload from an access unit, if it carries one."""
    i = 0
    n = len(annexb)
    while i < n - 4:
        if annexb[i] == 0 and annexb[i + 1] == 0:
            if annexb[i + 2] == 1:
                start, nal = i + 3, annexb[i + 3] if i + 3 < n else 0
            elif annexb[i + 2] == 0 and i + 3 < n and annexb[i + 3] == 1:
                start, nal = i + 4, annexb[i + 4] if i + 4 < n else 0
            else:
                i += 1
                continue
            if (nal & 0x1F) == 7:
                end = start
                while end < n - 3:
                    if annexb[end] == 0 and annexb[end + 1] == 0 and annexb[end + 2] in (0, 1):
                        break
                    end += 1
                else:
                    end = n
                return annexb[start:end]
            i = start
            continue
        i += 1
    return None


class Collector:
    """Counts what arrives, so the probe can measure instead of guess."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.frames = 0
        self.bytes = 0
        self.keyframes = 0
        self.width = 0
        self.height = 0
        self.codec = ""
        self.sps_changes = 0
        self.muxer = None  # the session looks for this attribute; we have no muxer

    def add_frame(self, frame: Frame) -> None:
        with self.lock:
            self.frames += 1
            self.bytes += len(frame.data)
            if frame.is_keyframe:
                self.keyframes += 1
        sps = _find_sps(frame.data)
        if sps is None:
            return
        try:
            info = parse_sps(sps)
        except Exception:
            return
        if (info["width"], info["height"]) != (self.width, self.height):
            with self.lock:
                if self.width:
                    self.sps_changes += 1
                self.width = info["width"]
                self.height = info["height"]
                self.codec = info.get("codec_string", "")

    @property
    def client_count(self) -> int:
        return 0

    def snapshot(self) -> tuple[int, int, int, int]:
        with self.lock:
            return self.frames, self.bytes, self.width, self.height

    def measure(self, seconds: float) -> dict[str, Any]:
        """Watch the stream for a while and report what it is doing."""
        f0, b0, _, _ = self.snapshot()
        t0 = time.monotonic()
        time.sleep(seconds)
        f1, b1, w, h = self.snapshot()
        dt = max(time.monotonic() - t0, 1e-6)
        frames = f1 - f0
        return {
            "fps": round(frames / dt, 2),
            "kbps": round((b1 - b0) * 8 / dt / 1000, 1),
            "width": w,
            "height": h,
            "frames": frames,
            "avg_frame_bytes": round((b1 - b0) / frames) if frames else 0,
        }


@dataclass
class ProbeResult:
    parameter: str
    value: int
    accepted: bool
    """The camera's own verdict: ``Ret=1`` accepted, ``Ret=0`` thrown away."""
    ret: str = ""
    measured: dict[str, Any] = field(default_factory=dict)
    verdict: str = ""
    note: str = ""


class Prober:
    """Changes one parameter at a time and measures what actually happens.

    Each change restarts the stream, which is the only way these cameras reliably
    apply one, and is also what keeps the probe honest: a setting that kills the
    encoder cannot poison every later measurement.
    """

    def __init__(
        self,
        session: CameraSession,
        collector: Collector,
        settle: float = 2.5,
        restart_timeout: float = 20.0,
    ) -> None:
        self.session = session
        self.collector = collector
        self.settle = settle
        self.restart_timeout = restart_timeout
        self.results: list[ProbeResult] = []
        self.baseline: dict[str, Any] = {}

    def _wait_for_video(self, since: int) -> bool:
        """Wait for the restarted session to deliver fresh frames."""
        deadline = time.monotonic() + self.restart_timeout
        while time.monotonic() < deadline:
            if self.collector.frames > since + 5:
                return True
            time.sleep(0.1)
        return False

    def _set_and_measure(self, key: str, value: int) -> ProbeResult:
        before = self.collector.frames
        self.session.set_acked = False
        self.session.last_set_ret = None
        self.session.request_set({key: value})

        alive = self._wait_for_video(before)
        ret = self.session.last_set_ret
        accepted = ret != b"0"
        measured = self.collector.measure(self.settle) if alive else {
            "fps": 0.0, "kbps": 0.0, "width": self.collector.width,
            "height": self.collector.height, "frames": 0, "avg_frame_bytes": 0,
        }
        return ProbeResult(
            parameter=key,
            value=value,
            accepted=accepted,
            ret=(ret or b"?").decode(errors="replace"),
            measured=measured,
        )

    def probe_frame_size(self) -> None:
        for width, height in P.FRAME_SIZES:
            value = P.frame_size_value(width, height)
            res = self._set_and_measure("FrameSize", value)
            got = (res.measured.get("width"), res.measured.get("height"))
            if got == (width, height):
                res.verdict = "applied"
            elif not res.measured.get("frames"):
                res.verdict = "stream stopped"
                res.note = "the camera stopped sending; this size is probably unsupported"
            else:
                res.verdict = "ignored"
                res.note = f"still encoding {got[0]}x{got[1]}"
            log.info("FrameSize %dx%d -> %s (%s)", width, height, res.verdict, res.note or "ok")
            self.results.append(res)

    def probe_frame_rate(self) -> None:
        for rate in P.FRAME_RATES:
            res = self._set_and_measure("FrameRate", rate)
            fps = res.measured.get("fps", 0)
            if not res.measured.get("frames"):
                res.verdict = "stream stopped"
            elif abs(fps - rate) <= max(2.0, rate * 0.25):
                res.verdict = "applied"
            else:
                res.verdict = "ignored"
                res.note = f"asked for {rate}, measured {fps}"
            log.info("FrameRate %d -> %s (measured %.1f fps)", rate, res.verdict, fps)
            self.results.append(res)

    def probe_bit_rate(self) -> None:
        for rate in P.BIT_RATES:
            res = self._set_and_measure("BitRate", rate)
            kbps = res.measured.get("kbps", 0)
            if not res.measured.get("frames"):
                res.verdict = "stream stopped"
            elif abs(kbps - rate) <= max(150.0, rate * 0.5):
                res.verdict = "applied"
            else:
                res.verdict = "unclear"
                res.note = f"asked for {rate} kb/s, measured {kbps} kb/s"
            log.info("BitRate %d -> %s (measured %.0f kb/s)", rate, res.verdict, kbps)
            self.results.append(res)

    def probe_simple(self, key: str, values: list[int]) -> None:
        """Parameters whose effect we cannot see from the bitstream alone."""
        base = self.baseline.get("avg_frame_bytes") or 1
        for value in values:
            res = self._set_and_measure(key, value)
            avg = res.measured.get("avg_frame_bytes", 0)
            if not res.accepted:
                res.verdict = "refused"
                res.note = "the camera answered Ret=0"
            elif not res.measured.get("frames"):
                res.verdict = "stream stopped"
                res.note = "accepted, but the picture did not come back"
            else:
                change = abs(avg - base) / base
                res.verdict = "accepted"
                res.note = (
                    f"average frame {avg} bytes, {change * 100:.0f}% off baseline"
                    + ("; picture probably changed" if change > 0.15 else "; no measurable change")
                )
            log.info("%s %d -> %s (%s)", key, value, res.verdict, res.note or "")
            self.results.append(res)

    def run(self, quick: bool = False) -> dict[str, Any]:
        # Snapshot before probing: every _set_and_measure mutates the session's
        # settings, so this is the only chance to remember what to put back.
        original = dict(self.session.settings.as_dict())

        log.info("measuring the camera as it is now")
        self.baseline = self.collector.measure(max(self.settle, 3.0))
        log.info(
            "baseline: %sx%s, %.1f fps, %.0f kb/s",
            self.baseline["width"],
            self.baseline["height"],
            self.baseline["fps"],
            self.baseline["kbps"],
        )

        self.probe_frame_size()
        if not quick:
            self.probe_frame_rate()
            self.probe_bit_rate()
        self.probe_simple("Zoom", [0, 1, 2, 3])
        self.probe_simple("FlipMirror", [0, 1, 2, 3])
        self.probe_simple("Infrared", [0, 1, 2])
        if not quick:
            self.probe_simple("Brightness", [0, 128, 255])
            self.probe_simple("Contrast", [0, 4, 7])
            self.probe_simple("LightCond", [0, 1])
            self.probe_simple("LightFreq", [0, 1])

        log.info("restoring the settings the session started with")
        self.session.request_set(original)
        time.sleep(1.0)

        return {
            "baseline": self.baseline,
            "codec": self.collector.codec,
            "restored": original,
            "results": [asdict(r) for r in self.results],
        }


def format_table(report: dict[str, Any]) -> str:
    """A compact summary suitable for pasting into an issue."""
    lines = []
    base = report["baseline"]
    lines.append(
        f"Baseline: {base['width']}x{base['height']}, {base['fps']} fps, "
        f"{base['kbps']} kb/s, codec {report.get('codec') or 'unknown'}"
    )
    lines.append("")
    lines.append(f"| {'parameter':<12} | {'value':>10} | {'ack':^3} | {'verdict':<14} | measured")
    lines.append(f"|{'-' * 14}|{'-' * 12}|{'-' * 5}|{'-' * 16}|{'-' * 34}")
    for r in report["results"]:
        m = r["measured"]
        if r["parameter"] == "FrameSize":
            shown = f"{(r['value'] >> 16)}x{r['value'] & 0xFFFF}"
            detail = f"{m.get('width')}x{m.get('height')} @ {m.get('fps')} fps"
        elif r["parameter"] == "BitRate":
            shown = str(r["value"])
            detail = f"{m.get('kbps')} kb/s"
        else:
            shown = str(r["value"])
            detail = f"{m.get('fps')} fps, {m.get('avg_frame_bytes')} B/frame"
        lines.append(
            f"| {r['parameter']:<12} | {shown:>10} | {r.get('ret', '?'):^3} "
            f"| {r['verdict']:<14} | {detail}"
        )
    return "\n".join(lines)


def run_probe(
    options: SessionOptions,
    settings: CameraSettings,
    quick: bool = False,
    settle: float = 2.5,
    out_path: str | None = None,
) -> int:
    """Start a session, sweep the parameters, print and optionally save a report."""
    collector = Collector()
    session = CameraSession(options, settings, sinks=[], hub=collector)

    thread = threading.Thread(target=session.run, name="probe-session", daemon=True)
    thread.start()

    log.info("waiting for the first frames")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and collector.frames < 5:
        time.sleep(0.2)
    if collector.frames < 5:
        log.error("no video arrived; nothing to probe")
        session.stop()
        thread.join(timeout=5)
        return 3

    try:
        report = Prober(session, collector, settle=settle).run(quick=quick)
    finally:
        session.stop()
        thread.join(timeout=5)
        session.close()

    table = format_table(report)
    print()
    print(table)
    print()

    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        log.info("wrote the full report to %s", out_path)
        print(f"Full report: {out_path}")
    print("Please attach that report if you open a camera compatibility issue.")
    return 0
