"""Local HTTP server: browser player, compatibility streams and a control API.

Bound to 127.0.0.1 only. The interesting endpoint is ``/stream.mp4``, which emits
a fragmented-MP4 init segment followed by one media segment per frame, for ever.
A browser plays that through Media Source Extensions with no transcoding and no
ffmpeg: the bytes on the wire are the camera's own H.264, rewrapped.

Endpoints
    ``/``                the player page
    ``/stream.mp4``      live fMP4, also openable directly in VLC
    ``/stream.h264``     the raw Annex B stream
    ``/mjpeg``           multipart JPEG, needs ffmpeg, for old clients
    ``/snapshot.jpg``    most recent JPEG, needs ffmpeg
    ``/api/status``      counters as JSON
    ``/api/stream-info`` codec string, size and the selectable parameter values
    ``/api/set``         POST {"Brightness": 160} to change camera parameters
    ``/api/record``      POST to toggle recording
"""

from __future__ import annotations

import json
import logging
import queue
import secrets
import threading
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .mp4 import FragmentedMP4Muxer, Frame
from .protocol import BIT_RATES, FRAME_RATES, FRAME_SIZES, frame_size_value

log = logging.getLogger(__name__)

MAX_CLIENT_BACKLOG = 90
"""Segments held for one client before old ones are dropped.

About three seconds at thirty frames a second. A live picture that is further
behind than that is worthless, and queueing more only converts a slow consumer
into permanent delay. When the queue fills we discard the oldest, not the newest,
because the newest is the one the viewer wants.
"""


class _Client:
    """One connected browser or player."""

    __slots__ = ("dropped", "q", "started", "wants_init")

    def __init__(self, wants_init: bool) -> None:
        self.q: queue.Queue[bytes | None] = queue.Queue(maxsize=MAX_CLIENT_BACKLOG)
        self.started = False
        self.wants_init = wants_init
        self.dropped = 0

    def put(self, data: bytes) -> None:
        try:
            self.q.put_nowait(data)
        except queue.Full:
            # Make room by throwing away the stalest frames, then keep this one.
            for _ in range(MAX_CLIENT_BACKLOG // 3):
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    break
            self.dropped += 1
            try:
                self.q.put_nowait(data)
            except queue.Full:
                pass


class StreamHub:
    """Fans frames out to connected clients, as fMP4 or as raw H.264.

    A client that joins mid-stream has to start at a keyframe or the decoder shows
    garbage, so each one is held back until the next one arrives. The camera sends
    a keyframe roughly every seven frames, so the wait is imperceptible.
    """

    def __init__(self, default_fps: float = 30.0) -> None:
        self.muxer = FragmentedMP4Muxer(default_fps=default_fps)
        self.lock = threading.Lock()
        self.mp4_clients: list[_Client] = []
        self.raw_clients: list[_Client] = []
        self.segments = 0

    # -- producer side -------------------------------------------------------

    def add_frame(self, frame: Frame) -> None:
        segment = self.muxer.add_frame(frame)
        with self.lock:
            raw = list(self.raw_clients)
            mp4 = list(self.mp4_clients)
        for client in raw:
            client.put(frame.data)
        if segment is None:
            return
        self.segments += 1
        init = self.muxer.init_segment()
        for client in mp4:
            if client.started:
                client.put(segment)
            elif frame.is_keyframe and init is not None:
                client.put(init + segment if client.wants_init else segment)
                client.started = True

    # -- consumer side -------------------------------------------------------

    def subscribe_mp4(self, wants_init: bool = True) -> _Client:
        client = _Client(wants_init)
        with self.lock:
            self.mp4_clients.append(client)
        return client

    def subscribe_raw(self) -> _Client:
        client = _Client(False)
        client.started = True
        with self.lock:
            self.raw_clients.append(client)
        return client

    def unsubscribe(self, client: _Client) -> None:
        with self.lock:
            for lst in (self.mp4_clients, self.raw_clients):
                if client in lst:
                    lst.remove(client)

    def close(self) -> None:
        with self.lock:
            everyone = self.mp4_clients + self.raw_clients
            self.mp4_clients.clear()
            self.raw_clients.clear()
        for client in everyone:
            try:
                client.q.put_nowait(None)
            except queue.Full:
                pass

    @property
    def client_count(self) -> int:
        with self.lock:
            return len(self.mp4_clients) + len(self.raw_clients)


def _load_page() -> bytes:
    """Read the player page, both when installed and when frozen by PyInstaller."""
    try:
        from importlib.resources import files

        return (files("eteq") / "web" / "index.html").read_bytes()
    except Exception:
        import os

        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
        with open(here, "rb") as fh:
            return fh.read()


class CameraHTTPServer:
    """Serves one camera session on 127.0.0.1."""

    def __init__(
        self,
        port: int,
        hub: StreamHub,
        status_fn: Callable[[], dict[str, Any]],
        set_fn: Callable[[dict[str, int]], None] | None = None,
        record_fn: Callable[[], dict[str, Any]] | None = None,
        mjpeg_fn: Callable[[], Any] | None = None,
        host: str = "127.0.0.1",
        token: str | None = None,
    ) -> None:
        self.hub = hub
        self.token = token
        self.allow_lan = False
        self.status_fn = status_fn
        self.set_fn = set_fn
        self.record_fn = record_fn
        self.mjpeg_fn = mjpeg_fn

        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "eteq"
            sys_version = ""
            # Each media segment is one small write; without this they would wait
            # for Nagle's algorithm and add latency to a live picture.
            disable_nagle_algorithm = True

            def log_message(self, fmt: str, *args: Any) -> None:
                log.info("http %s: " + fmt, self.client_address[0], *args)

            # -- helpers ----------------------------------------------------

            def _send(self, code: int, ctype: str, body: bytes, extra: dict[str, str] | None = None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, payload: dict[str, Any], code: int = 200) -> None:
                self._send(code, "application/json", json.dumps(payload).encode())

            def _is_loopback(self) -> bool:
                return self.client_address[0] in ("127.0.0.1", "::1")

            def _authorised(self) -> bool:
                """This machine is always allowed; anything else needs the key.

                Serving to a phone means serving to the whole network the phone is
                on, so the key stops the neighbours watching down your drain.
                """
                if self._is_loopback():
                    return True
                if server.token is None:
                    return server.allow_lan
                supplied = self.headers.get("X-Eteq-Key")
                if supplied is None:
                    query = urllib.parse.urlparse(self.path).query
                    supplied = urllib.parse.parse_qs(query).get("k", [None])[0]
                return bool(supplied) and secrets.compare_digest(supplied, server.token)

            def _csrf_ok(self) -> bool:
                """A custom header cannot be set by a cross-origin form post.

                Combined with the loopback bind this keeps a random web page you
                happen to have open from driving the camera.
                """
                if self.headers.get("X-Eteq") != "1":
                    return False
                origin = self.headers.get("Origin")
                if origin and not origin.startswith(("http://127.0.0.1", "http://localhost")):
                    return False
                return True

            def _read_json(self) -> dict[str, Any] | None:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return None
                if length <= 0 or length > 64 * 1024:
                    return {}
                try:
                    return json.loads(self.rfile.read(length) or b"{}")
                except (ValueError, OSError):
                    return None

            def _stream(self, client: _Client, ctype: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    while True:
                        data = client.q.get()
                        if data is None:
                            break
                        self.wfile.write(data)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass
                finally:
                    server.hub.unsubscribe(client)

            # -- routes ------------------------------------------------------

            def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
                if not self._authorised():
                    self.send_error(403, "wrong or missing access key")
                    return
                path = self.path.split("?", 1)[0]

                if path in ("/", "/index.html"):
                    # Read it each time: the file is small, and caching it in
                    # memory makes editing the page during development confusing.
                    self._send(200, "text/html; charset=utf-8", _load_page())

                elif path == "/stream.mp4":
                    self._stream(server.hub.subscribe_mp4(), "video/mp4")

                elif path == "/stream.h264":
                    self._stream(server.hub.subscribe_raw(), "video/H264")

                elif path == "/api/status":
                    self._json(server.status_fn())

                elif path == "/api/stream-info":
                    muxer = server.hub.muxer
                    status = server.status_fn()
                    self._json(
                        {
                            "codec": muxer.codec_string if muxer.ready else "avc1.42001E",
                            "width": muxer.width if muxer.ready else 0,
                            "height": muxer.height if muxer.ready else 0,
                            "ready": muxer.ready,
                            "frame_sizes": [frame_size_value(w, h) for w, h in FRAME_SIZES],
                            "frame_rates": list(FRAME_RATES),
                            "bit_rates": list(BIT_RATES),
                            "params": status.get("params", {}),
                        }
                    )

                elif path == "/mjpeg":
                    self._mjpeg()

                elif path in ("/snapshot.jpg", "/snapshot"):
                    self._snapshot()

                else:
                    self.send_error(404)

            def do_POST(self) -> None:  # noqa: N802
                # Always consume the body, even when rejecting the request: an
                # unread body left on a keep-alive connection desynchronises it
                # and the next request on that socket dies.
                payload = self._read_json()
                if not self._authorised():
                    self.send_error(403, "wrong or missing access key")
                    return
                if not self._csrf_ok():
                    self._json({"error": "missing X-Eteq header"}, 403)
                    return
                path = self.path.split("?", 1)[0]
                if payload is None:
                    self._json({"error": "bad json"}, 400)
                    return

                if path == "/api/set":
                    if server.set_fn is None:
                        self._json({"error": "no camera session"}, 503)
                        return
                    clean: dict[str, int] = {}
                    for key, value in payload.items():
                        try:
                            clean[str(key)] = int(value)
                        except (TypeError, ValueError):
                            self._json({"error": f"{key} must be an integer"}, 400)
                            return
                    server.set_fn(clean)
                    self._json({"queued": clean})

                elif path == "/api/record":
                    if server.record_fn is None:
                        self._json({"error": "recording unavailable"}, 503)
                        return
                    self._json(server.record_fn())

                else:
                    self.send_error(404)

            # -- ffmpeg-backed extras ----------------------------------------

            def _mjpeg(self) -> None:
                transcoder = server.mjpeg_fn() if server.mjpeg_fn else None
                if transcoder is None:
                    self.send_error(503, "MJPEG needs ffmpeg; use / or /stream.mp4 instead")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                last = 0
                try:
                    while True:
                        last, jpg = transcoder.wait_frame(last, timeout=5)
                        if jpg is None:
                            if not transcoder.running():
                                break
                            continue
                        self.wfile.write(
                            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg)
                        )
                        self.wfile.write(jpg)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass

            def _snapshot(self) -> None:
                transcoder = server.mjpeg_fn() if server.mjpeg_fn else None
                jpg = getattr(transcoder, "latest", None) if transcoder else None
                if jpg is None:
                    self.send_error(503, "no JPEG yet; the browser page can save a PNG without ffmpeg")
                    return
                self._send(200, "image/jpeg", jpg, {"Content-Disposition": 'inline; filename="eteq.jpg"'})

        self.allow_lan = host not in ("127.0.0.1", "localhost", "::1")
        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="http", daemon=True)
        self.thread.start()
        log.info("player ready at http://%s:%d/", host, self.port)

    def urls(self, addresses: list[str]) -> list[str]:
        """The addresses a browser elsewhere on the network should use."""
        suffix = f"/?k={self.token}" if self.token else "/"
        return [f"http://{addr}:{self.port}{suffix}" for addr in addresses]

    def close(self) -> None:
        self.hub.close()
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
