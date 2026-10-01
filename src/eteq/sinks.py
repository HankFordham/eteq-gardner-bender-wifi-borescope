"""Places a decoded video frame can go.

A sink takes :class:`eteq.mp4.Frame` objects and does something with them. The
two that matter most need nothing installed:

* :class:`RawFileSink` writes the camera's H.264 byte stream straight to disk.
* :class:`Mp4FileSink` wraps it into a playable ``.mp4`` using our own muxer.

The remaining two shell out to ffmpeg and are optional extras. Everything degrades
to a warning if ffmpeg is missing, because the browser player does not need it.
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import threading

from .mp4 import Frame, MP4FileWriter

log = logging.getLogger(__name__)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def have(tool: str) -> bool:
    """Is this helper executable on PATH?"""
    return shutil.which(tool) is not None


def ffmpeg_hint() -> str:
    return (
        "ffmpeg was not found on PATH. It is optional: the browser player and "
        "recording work without it. To install it on Windows: winget install Gyan.FFmpeg"
    )


class Sink:
    """Interface every sink implements."""

    name = "sink"

    def write_frame(self, frame: Frame) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def running(self) -> bool:
        return True

    def close(self) -> None:  # pragma: no cover - interface
        pass


class RawFileSink(Sink):
    """The raw Annex B stream, exactly as the camera sent it.

    Replay it later with ``ffplay -f h264 file.h264``, or hand it to ``eteq
    --convert`` to turn it into an mp4 without ffmpeg.
    """

    name = "raw file"

    def __init__(self, path: str) -> None:
        self.path = path
        self.fh = open(path, "wb")
        self.frames = 0

    def write_frame(self, frame: Frame) -> None:
        self.fh.write(frame.data)
        self.frames += 1

    def close(self) -> None:
        try:
            self.fh.close()
        except Exception:
            pass
        log.info("wrote %d frames to %s", self.frames, self.path)


class Mp4FileSink(Sink):
    """A playable fragmented MP4, muxed in-process with no ffmpeg."""

    name = "mp4 file"

    def __init__(self, path: str, default_fps: float = 30.0) -> None:
        self.path = path
        self.writer = MP4FileWriter(path, default_fps=default_fps)
        self.frames = 0

    def write_frame(self, frame: Frame) -> None:
        self.writer.write_frame(frame)
        self.frames += 1

    def close(self) -> None:
        try:
            self.writer.close()
        except Exception:
            log.exception("failed to finalise %s", self.path)
            return
        size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        log.info("wrote %d frames (%.1f MB) to %s", self.frames, size / 1e6, self.path)


class SubprocessPipeSink(Sink):
    """Feeds frames to a child process's stdin through a bounded queue.

    The queue keeps a slow or stalled player from blocking the network loop; if it
    fills, frames are dropped rather than buffered forever.
    """

    def __init__(self, name: str, argv: list[str], maxsize: int = 2000) -> None:
        self.name = name
        self.q: queue.Queue[bytes | None] = queue.Queue(maxsize=maxsize)
        self.alive = True
        self.dropped = 0
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, creationflags=_NO_WINDOW)
        threading.Thread(target=self._pump, name=f"{name}-writer", daemon=True).start()

    def _pump(self) -> None:
        while self.alive:
            data = self.q.get()
            if data is None:
                break
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                log.warning("%s closed its input; that sink is now disabled", self.name)
                self.alive = False
                break

    def write_frame(self, frame: Frame) -> None:
        self.write_bytes(frame.data)

    def write_bytes(self, data: bytes) -> None:
        if not self.alive:
            return
        try:
            self.q.put_nowait(data)
        except queue.Full:
            self.dropped += 1

    def running(self) -> bool:
        return self.alive and self.proc.poll() is None

    def close(self) -> None:
        self.alive = False
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass
        # Kill before closing stdin: the writer thread may be blocked inside
        # write() holding the pipe lock, and ffplay does not exit on EOF anyway.
        try:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait(timeout=2)
        except Exception:
            pass
        try:
            self.proc.stdin.close()
        except Exception:
            pass


class FFplaySink(SubprocessPipeSink):
    """A native low-latency window, if ffplay is installed."""

    def __init__(self, scale: float | None = None, title: str = "eteq camera") -> None:
        argv = [
            "ffplay",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-framedrop",
            "-probesize",
            "32",
            "-analyzeduration",
            "0",
            "-autoexit",
            "-window_title",
            title,
            "-f",
            "h264",
        ]
        if scale:
            argv += ["-vf", f"scale=iw:ih*{scale}"]
        argv += ["-i", "pipe:0"]
        super().__init__("ffplay", argv)


class MjpegTranscoder(Sink):
    """H.264 in, JPEG frames out, via ffmpeg.

    Only needed for the ``/mjpeg`` compatibility endpoint and for server-side
    snapshots. The browser player does not use this.

    The camera's H.264 carries no container timing, so ffmpeg's MJPEG muxer drops
    almost every frame unless an input frame rate is declared. Passing ``-r`` as an
    *input* option makes the raw h264 demuxer stamp frames itself, which recovers
    essentially all of them.
    """

    name = "mjpeg"

    def __init__(self, quality: int = 4, fps: float = 30.0) -> None:
        argv = [
            "ffmpeg",
            "-loglevel",
            "error",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-probesize",
            "65536",
            "-analyzeduration",
            "200000",
            "-r",
            str(int(fps) or 30),
            "-f",
            "h264",
            "-i",
            "pipe:0",
            "-an",
            "-f",
            "mjpeg",
            "-q:v",
            str(quality),
            "pipe:1",
        ]
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
        self.q: queue.Queue[bytes | None] = queue.Queue(maxsize=2000)
        self.latest: bytes | None = None
        self.frame_no = 0
        self.cond = threading.Condition()
        self.alive = True
        threading.Thread(target=self._writer, name="mjpeg-writer", daemon=True).start()
        threading.Thread(target=self._reader, name="mjpeg-reader", daemon=True).start()

    def _writer(self) -> None:
        while self.alive:
            data = self.q.get()
            if data is None:
                break
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                self.alive = False
                break

    def _reader(self) -> None:
        buf = bytearray()
        while self.alive:
            chunk = self.proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                if start < 0:
                    buf.clear()
                    break
                end = buf.find(b"\xff\xd9", start + 2)
                if end < 0:
                    if start > 0:
                        del buf[:start]
                    break
                jpg = bytes(buf[start : end + 2])
                del buf[: end + 2]
                with self.cond:
                    self.latest = jpg
                    self.frame_no += 1
                    self.cond.notify_all()

    def write_frame(self, frame: Frame) -> None:
        if not self.alive:
            return
        try:
            self.q.put_nowait(frame.data)
        except queue.Full:
            pass

    def wait_frame(self, last_no: int, timeout: float = 2.0):
        """Block until a JPEG newer than ``last_no`` exists."""
        with self.cond:
            if self.frame_no == last_no:
                self.cond.wait(timeout)
            return self.frame_no, self.latest

    def running(self) -> bool:
        return self.alive

    def close(self) -> None:
        self.alive = False
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass
        with self.cond:
            self.cond.notify_all()
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.kill()
        except Exception:
            pass


def make_recorder(path: str, default_fps: float = 30.0) -> Sink:
    """Pick a recording sink from the file extension."""
    lower = path.lower()
    if lower.endswith((".h264", ".264", ".bin")):
        return RawFileSink(path)
    if not lower.endswith(".mp4"):
        log.warning("unrecognised recording extension for %s; writing fragmented MP4", path)
    return Mp4FileSink(path, default_fps=default_fps)
