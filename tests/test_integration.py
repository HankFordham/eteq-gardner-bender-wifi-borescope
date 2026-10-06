"""End to end over a real socket, against the simulated camera.

No hardware, no ffmpeg and no network access beyond loopback, so this runs in CI
on every platform. It is the test that would have caught the session-killing
acknowledgement bug and the silent MP4 muxing bug.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

from eteq.server import CameraHTTPServer, StreamHub
from eteq.session import CameraSession, CameraSettings, SessionOptions
from eteq.simulator import SimulatedCamera

SAMPLE = os.path.join(os.path.dirname(__file__), "data", "sample.h264")
pytestmark = pytest.mark.skipif(not os.path.exists(SAMPLE), reason="tests/data/sample.h264 is missing")


def wait_for(predicate, timeout=20.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def camera():
    """A simulated camera on an ephemeral loopback port."""
    cam = SimulatedCamera(bind="127.0.0.1", port=0, beacon_to=("127.0.0.1", 59999), clip=SAMPLE, fps=30.0)
    port = cam.sock.getsockname()[1]
    thread = threading.Thread(target=cam.run, daemon=True)
    thread.start()
    yield cam, port
    cam.running = False
    thread.join(timeout=3)


def make_session(port, hub=None, **overrides):
    options = SessionOptions(
        ip="127.0.0.1",
        cam_port=port,
        local_port=0,
        discover=False,
        ack_timeout=5.0,
        idle_timeout=8.0,
        reconnect=False,
        stats_interval=0,
        dump_packets=0,
        dump_info=0,
        send_stop=False,
        **overrides,
    )
    return CameraSession(options, CameraSettings(fps=30), sinks=[], hub=hub)


class Collector:
    """Minimal hub: counts frames and keeps the last one."""

    def __init__(self):
        self.frames = []
        self.muxer = None

    def add_frame(self, frame):
        self.frames.append(frame)

    @property
    def client_count(self):
        return 0


class TestHandshake:
    def test_camera_starts_streaming(self, camera):
        _, port = camera
        collector = Collector()
        session = make_session(port, collector)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        try:
            assert wait_for(lambda: len(collector.frames) >= 10), "no video arrived"
            assert session.get_acked, "the camera never answered AllInfo"
            assert session.set_acked, "the camera never acknowledged the start command"
            assert any(f.is_keyframe for f in collector.frames)
        finally:
            session.stop()
            thread.join(timeout=5)

    def test_timestamps_increase(self, camera):
        _, port = camera
        collector = Collector()
        session = make_session(port, collector)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        try:
            assert wait_for(lambda: len(collector.frames) >= 15)
            stamps = [f.timestamp_ms for f in collector.frames[:15]]
            assert stamps == sorted(stamps)
            assert stamps[-1] > stamps[0], "the presentation clock never advanced"
        finally:
            session.stop()
            thread.join(timeout=5)

    def test_frames_are_annex_b_access_units(self, camera):
        _, port = camera
        collector = Collector()
        session = make_session(port, collector)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        try:
            assert wait_for(lambda: len(collector.frames) >= 5)
            for frame in collector.frames[:5]:
                assert frame.data.startswith(b"\x00\x00\x00\x01") or frame.data.startswith(b"\x00\x00\x01")
        finally:
            session.stop()
            thread.join(timeout=5)

    def test_survives_packet_loss(self, camera):
        """The simulator drops every 7th packet; the transport must recover."""
        cam, port = camera
        cam.loss = 7
        collector = Collector()
        session = make_session(port, collector)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        try:
            assert wait_for(lambda: len(collector.frames) >= 20, timeout=30), "loss recovery failed"
        finally:
            session.stop()
            thread.join(timeout=5)


class TestHttp:
    @pytest.fixture
    def live(self, camera):
        _, port = camera
        hub = StreamHub(default_fps=30.0)
        session = make_session(port, hub)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        server = CameraHTTPServer(
            0,
            hub,
            status_fn=session.status,
            set_fn=session.request_set,
            record_fn=session.toggle_record,
        )
        assert wait_for(lambda: session.video_frames >= 10), "no video before serving"
        yield session, server
        session.stop()
        thread.join(timeout=5)
        server.close()

    def get(self, server, path, timeout=10):
        return urllib.request.urlopen(f"http://127.0.0.1:{server.port}{path}", timeout=timeout)

    def test_player_page_is_served(self, live):
        _, server = live
        body = self.get(server, "/").read()
        assert b"<title>eteq camera</title>" in body
        assert b"MediaSource" in body, "the page must contain the player"

    def test_status_json(self, live):
        session, server = live
        status = json.loads(self.get(server, "/api/status").read())
        assert status["video_frames"] > 0
        assert status["set_acked"] is True
        assert status["camera"][0] == "127.0.0.1"
        assert status["sessions"] == 1

    def test_stream_info_reports_a_codec(self, live):
        _, server = live
        info = json.loads(self.get(server, "/api/stream-info").read())
        assert info["ready"] is True
        assert info["codec"].startswith("avc1.")
        assert info["width"] > 0 and info["height"] > 0
        assert info["frame_sizes"], "the UI needs the selectable sizes"

    def test_stream_mp4_starts_with_an_init_segment(self, live):
        """This is what a browser consumes; it must begin ftyp then moov."""
        _, server = live
        with self.get(server, "/stream.mp4") as response:
            assert response.headers["Content-Type"] == "video/mp4"
            head = response.read(4096)
        assert head[4:8] == b"ftyp", f"expected ftyp, got {head[:16]!r}"
        assert b"moov" in head
        assert b"moof" in head or b"mdat" in head, "no media followed the init segment"

    def test_stream_h264_is_raw(self, live):
        _, server = live
        with self.get(server, "/stream.h264") as response:
            head = response.read(512)
        assert head.startswith(b"\x00\x00\x00\x01") or head.startswith(b"\x00\x00\x01")

    def test_control_requires_the_header(self, live):
        _, server = live
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/api/set",
            data=b'{"Brightness": 200}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=5)
        assert excinfo.value.code == 403

    def test_control_applies_a_parameter(self, live):
        session, server = live
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/api/set",
            data=b'{"Brightness": 200}',
            headers={"Content-Type": "application/json", "X-Eteq": "1"},
            method="POST",
        )
        assert urllib.request.urlopen(request, timeout=5).status == 200
        assert wait_for(lambda: session.settings.brightness == 200, timeout=5)

    def test_recording_round_trip(self, live, tmp_path):
        session, server = live
        target = str(tmp_path / "clip.mp4")
        assert session.toggle_record(target)["recording"] is True
        assert wait_for(lambda: session.video_frames > 20)
        time.sleep(0.5)
        assert session.toggle_record()["recording"] is False
        assert os.path.getsize(target) > 1000
        with open(target, "rb") as fh:
            assert fh.read(12)[4:8] == b"ftyp"

    def test_unknown_path_is_404(self, live):
        _, server = live
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            self.get(server, "/nope")
        assert excinfo.value.code == 404


class TestSettingsAndStalls:
    """Behaviour learned from the reference camera on 2026-10-01.

    It answers ``Ret=0`` to most settings changed while it is streaming, keeps
    answering the heartbeat after its encoder has stopped, and on some values
    stops sending video for good. All three are reproduced by the simulator.
    """

    def run_session(self, port, collector, **overrides):
        session = make_session(port, collector, **overrides)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        return session, thread

    def test_refusal_is_noticed(self, camera):
        """Ret is a verdict. Ret=0 means the camera threw the setting away.

        Only reachable with --live-settings, because the default restarts the
        stream instead of changing it in place, which is exactly why that is the
        default.
        """
        cam, port = camera
        cam.refuse_settings = True
        collector = Collector()
        session, thread = self.run_session(port, collector, live_settings=True)
        try:
            assert wait_for(lambda: session.video_frames >= 5)
            session.request_set({"Brightness": 200})
            assert wait_for(lambda: session.settings_refused >= 1, timeout=25), (
                "a Ret=0 answer must be counted as a refusal, not treated as success"
            )
            assert session.last_set_ret == b"0"
        finally:
            session.stop()
            thread.join(timeout=5)

    def test_a_setting_change_restarts_the_session(self, camera):
        _, port = camera
        collector = Collector()
        session, thread = self.run_session(port, collector)
        try:
            assert wait_for(lambda: session.video_frames >= 5)
            first = session.sessions
            session.request_set({"Zoom": 2})
            assert wait_for(lambda: session.sessions > first, timeout=25), (
                "the default is to restart, because mid-stream changes are refused"
            )
            before = session.video_frames
            assert wait_for(lambda: session.video_frames > before + 5, timeout=25), (
                "video must come back after the restart"
            )
            assert session.settings.zoom == 2
        finally:
            session.stop()
            thread.join(timeout=5)

    def test_live_settings_mode_does_not_restart(self, camera):
        _, port = camera
        collector = Collector()
        session, thread = self.run_session(port, collector, live_settings=True)
        try:
            assert wait_for(lambda: session.video_frames >= 5)
            first = session.sessions
            session.request_set({"Zoom": 1})
            time.sleep(2.0)
            assert session.sessions == first, "--live-settings must leave the stream alone"
        finally:
            session.stop()
            thread.join(timeout=5)

    def test_dead_encoder_is_detected_while_the_camera_still_answers(self, camera):
        """The failure that froze the picture for minutes on real hardware."""
        cam, port = camera
        collector = Collector()
        session, thread = self.run_session(port, collector, video_timeout=3.0)
        try:
            assert wait_for(lambda: session.video_frames >= 10)
            first = session.sessions
            cam.streaming = False        # encoder dies, heartbeat keeps answering
            cam.ignore_start = True
            assert wait_for(lambda: session.sessions > first, timeout=25), (
                "a silent encoder must trigger a restart even though packets keep arriving"
            )
            assert session.transport is not None
            assert session.transport.rx_packets > 0, "the camera was still talking throughout"
        finally:
            session.stop()
            thread.join(timeout=5)


class TestSharingToOtherDevices:
    """Serving the picture to a phone means serving it to a whole network."""

    @pytest.fixture
    def shared(self, camera):
        _, port = camera
        hub = StreamHub(default_fps=30.0)
        session = make_session(port, hub)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        server = CameraHTTPServer(
            0, hub, status_fn=session.status, set_fn=session.request_set,
            host="0.0.0.0", token="abc123",  # noqa: S104 - that is the feature
        )
        assert wait_for(lambda: session.video_frames >= 10)
        yield session, server
        session.stop()
        thread.join(timeout=5)
        server.close()

    def test_loopback_never_needs_a_key(self, shared):
        _, server = shared
        body = urllib.request.urlopen(f"http://127.0.0.1:{server.port}/", timeout=10).read()
        assert b"<title>eteq camera</title>" in body

    def test_urls_carry_the_key(self, shared):
        _, server = shared
        urls = server.urls(["192.168.1.20"])
        assert urls == [f"http://192.168.1.20:{server.port}/?k=abc123"]

    def test_a_key_is_required_off_machine(self, shared):
        """The authorisation rule itself, independent of where the socket came from."""
        _, server = shared
        handler_cls = server.httpd.RequestHandlerClass
        checker = handler_cls._authorised

        class Fake:
            def __init__(self, addr, path, headers):
                self.client_address = addr
                self.path = path
                self.headers = headers

            def _is_loopback(self):
                return handler_cls._is_loopback(self)

        remote = ("192.168.1.50", 5000)
        assert checker(Fake(("127.0.0.1", 5000), "/", {})) is True, "this machine is always allowed"
        assert checker(Fake(remote, "/", {})) is False, "a stranger with no key must be refused"
        assert checker(Fake(remote, "/?k=wrong", {})) is False
        assert checker(Fake(remote, "/?k=abc123", {})) is True
        assert checker(Fake(remote, "/", {"X-Eteq-Key": "abc123"})) is True

    def test_page_passes_the_key_on(self, shared):
        _, server = shared
        page = urllib.request.urlopen(f"http://127.0.0.1:{server.port}/", timeout=10).read()
        assert b"X-Eteq-Key" in page, "the page must send the key it was opened with"
        assert b'get("k")' in page
