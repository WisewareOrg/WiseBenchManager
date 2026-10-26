"""`Proxy`: replays a recorded HAR with mitmdump, and the `proxy` fixture around it."""

import hashlib
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mitmproxy import exceptions
from mitmproxy.io import FlowWriter
from mitmproxy.test import tflow, tutils

from wisebench import proxy

_HIT_URL = "http://example.invalid/hello"
_HIT_BODY = b"hello-body"
_MISS_URL = "http://example.invalid/missing"

_HTTPS_HIT_URL = "https://wisebench-proxy-test.invalid/hello"
_HTTPS_HIT_BODY = "hello-from-replay"
_HTTPS_MISS_URL = "https://wisebench-proxy-test.invalid/missing"

_TCP_GARBAGE = b"not-http-or-tls\n"

_POST_URL = "http://example.invalid/submit"
_POST_BODY = "field=value"


def _har_entry(url, body):
    return {
        "startedDateTime": datetime.now(UTC).isoformat(),
        "time": 0,
        "request": {"method": "GET", "url": url, "httpVersion": "HTTP/1.1", "headers": []},
        "response": {
            "status": 200,
            "httpVersion": "HTTP/1.1",
            "headers": [{"name": "Content-Type", "value": "text/plain"}],
            "content": {"text": body},
        },
    }


@pytest.fixture
def har(tmp_path):
    path = tmp_path / "recording.har"
    entry = _har_entry(_HIT_URL, _HIT_BODY.decode())
    path.write_text(json.dumps({"log": {"version": "1.2", "entries": [entry]}}))
    return path


@pytest.fixture
def https_har(tmp_path):
    path = tmp_path / "https_recording.har"
    entry = _har_entry(_HTTPS_HIT_URL, _HTTPS_HIT_BODY)
    path.write_text(json.dumps({"log": {"version": "1.2", "entries": [entry]}}))
    return path


@pytest.fixture
def post_har(tmp_path):
    path = tmp_path / "post_recording.har"
    entry = {
        "startedDateTime": datetime.now(UTC).isoformat(),
        "time": 0,
        "request": {
            "method": "POST",
            "url": _POST_URL,
            "httpVersion": "HTTP/1.1",
            "headers": [],
            "postData": {"text": _POST_BODY},
        },
        "response": {
            "status": 200,
            "httpVersion": "HTTP/1.1",
            "headers": [{"name": "Content-Type", "value": "text/plain"}],
            "content": {"text": "ok"},
        },
    }
    path.write_text(json.dumps({"log": {"version": "1.2", "entries": [entry]}}))
    return path


def _fetch(port, url):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{port}"})
    )
    return opener.open(url, timeout=5)


def test_start_serves_the_recorded_response(har, tmp_path):
    server = proxy.Proxy(har, tmp_path / "proxy.log", tmp_path / "proxy.flows")
    server.start()
    try:
        assert _fetch(server.port, _HIT_URL).read() == _HIT_BODY
        assert server.served() == {("GET", _HIT_URL): 1}
        assert server.killed() == []
    finally:
        server.stop()


def test_a_recorded_request_replays_every_time_it_is_repeated(har, tmp_path):
    server = proxy.Proxy(har, tmp_path / "proxy.log", tmp_path / "proxy.flows")
    server.start()
    try:
        assert _fetch(server.port, _HIT_URL).read() == _HIT_BODY
        assert _fetch(server.port, _HIT_URL).read() == _HIT_BODY
        assert server.served() == {("GET", _HIT_URL): 2}
    finally:
        server.stop()


def test_killed_names_a_request_outside_the_recording(har, tmp_path):
    server = proxy.Proxy(har, tmp_path / "proxy.log", tmp_path / "proxy.flows")
    server.start()
    try:
        with pytest.raises((urllib.error.URLError, ConnectionError)):
            _fetch(server.port, _MISS_URL)
        assert server.killed() == [_MISS_URL]
        assert server.served() == {("GET", _HIT_URL): 0}
    finally:
        server.stop()


def test_https_replay_needs_no_reachable_upstream(https_har, tmp_path):
    server = proxy.Proxy(https_har, tmp_path / "proxy.log", tmp_path / "proxy.flows")
    server.start()
    try:
        hit = subprocess.run(
            ["curl", "-s", "-k", "--proxy", f"http://127.0.0.1:{server.port}", _HTTPS_HIT_URL],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert hit.stdout == _HTTPS_HIT_BODY

        miss = subprocess.run(
            ["curl", "-s", "-k", "--proxy", f"http://127.0.0.1:{server.port}", _HTTPS_MISS_URL],
            capture_output=True,
            timeout=5,
        )
        assert miss.returncode != 0
    finally:
        server.stop()
    assert server.killed() == [_HTTPS_MISS_URL]
    assert server.served() == {("GET", _HTTPS_HIT_URL): 1}


def test_killed_names_a_tcp_flow_the_proxy_could_not_classify_as_http(har, tmp_path):
    """A non-HTTP flow is named by its destination, and served() is unaffected by it."""
    server = proxy.Proxy(har, tmp_path / "proxy.log", tmp_path / "proxy.flows")
    server.start()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener_port = listener.getsockname()[1]
    try:
        with socket.create_connection(("127.0.0.1", server.port), timeout=5) as client:
            client.sendall(
                f"CONNECT 127.0.0.1:{listener_port} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{listener_port}\r\n\r\n".encode()
            )
            assert b"200" in client.recv(4096)
            client.sendall(_TCP_GARBAGE)
            accepted, _ = listener.accept()
            accepted.recv(4096)
            accepted.close()
    finally:
        listener.close()
        server.stop()
    assert server.killed() == [f"tcp://127.0.0.1:{listener_port}"]
    assert server.served() == {("GET", _HIT_URL): 0}


def test_start_raises_and_logs_mitmdumps_own_error_on_an_unreadable_recording(tmp_path):
    bad_har = tmp_path / "not_a_har_file"
    bad_har.write_text("not a har file")
    server = proxy.Proxy(bad_har, tmp_path / "proxy.log", tmp_path / "proxy.flows")
    with pytest.raises(OSError, match="mitmdump exited"):
        server.start()
    assert (tmp_path / "proxy.log").read_text().strip()


class _NeverListens:
    """A `Popen` stand-in that never exits and never accepts a connection."""

    def __init__(self):
        self.killed = False
        self.waited = False

    def poll(self):
        return None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waited = True


def test_start_kills_a_process_that_never_starts_listening(har, tmp_path, monkeypatch):
    stand_in = _NeverListens()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: stand_in)
    monkeypatch.setattr(proxy, "_LISTEN_TIMEOUT", 0.3)
    monkeypatch.setattr(proxy, "_LISTEN_INTERVAL", 0.05)

    server = proxy.Proxy(har, tmp_path / "proxy.log", tmp_path / "proxy.flows")
    started = time.monotonic()
    with pytest.raises(OSError):
        server.start()
    elapsed = time.monotonic() - started

    assert elapsed < 2
    assert stand_in.killed
    assert stand_in.waited


def test_proxy_fixture_yields_a_proxy_and_records_the_recordings_sha256_and_counts(
    pytester, monkeypatch, har
):
    """The fixture yields a `Proxy`, and teardown records the sha256, served_count and
    killed_count through `record_property`."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    pytester.makepyfile(f"""
        import urllib.request

        import pytest

        @pytest.mark.parametrize("proxy", [{str(har)!r}], indirect=True)
        def test_it(proxy):
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({{"http": f"http://127.0.0.1:{{proxy.port}}"}})
            )
            assert opener.open({_HIT_URL!r}, timeout=5).read() == {_HIT_BODY!r}
    """)
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)

    run_dir = next(Path("records").iterdir())
    with open(har, "rb") as f:
        expected_sha256 = hashlib.file_digest(f, "sha256").hexdigest()
    report = (run_dir / "report.jsonl").read_text()
    assert f'"recording_sha256", "{expected_sha256}"' in report
    assert '"served_count", 1' in report
    assert '"killed_count", 0' in report

    proxy_logs = list(run_dir.glob("proxy-test_it-*.log"))
    assert len(proxy_logs) == 1
    assert "[replay]" in proxy_logs[0].read_text()


def test_proxy_fixture_records_served_count_as_a_sum_of_replays_not_a_count_of_requests(
    pytester, monkeypatch, har
):
    """`served_count` sums every replay of a recorded request, so replaying the same request
    twice reports 2, not the 1 distinct request the HAR recorded."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    pytester.makepyfile(f"""
        import urllib.request

        import pytest

        @pytest.mark.parametrize("proxy", [{str(har)!r}], indirect=True)
        def test_it(proxy):
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({{"http": f"http://127.0.0.1:{{proxy.port}}"}})
            )
            assert opener.open({_HIT_URL!r}, timeout=5).read() == {_HIT_BODY!r}
            assert opener.open({_HIT_URL!r}, timeout=5).read() == {_HIT_BODY!r}
    """)
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)

    run_dir = next(Path("records").iterdir())
    report = (run_dir / "report.jsonl").read_text()
    assert '"served_count", 2' in report


def test_proxy_fixture_records_a_kill_and_the_session_continues(pytester, monkeypatch, har):
    """A kill neither fails the test nor ends the session, and served_count stays the sum of
    actual replays (0 here) even though the HAR recorded one request."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    pytester.makepyfile(f"""
        import urllib.error
        import urllib.request

        import pytest

        @pytest.mark.parametrize("proxy", [{str(har)!r}], indirect=True)
        def test_it(proxy):
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({{"http": f"http://127.0.0.1:{{proxy.port}}"}})
            )
            with pytest.raises((urllib.error.URLError, ConnectionError)):
                opener.open({_MISS_URL!r}, timeout=5)

        def test_another():
            pass
    """)
    result = pytester.runpytest()
    assert result.ret == 0
    result.assert_outcomes(passed=2)

    run_dir = next(Path("records").iterdir())
    report = (run_dir / "report.jsonl").read_text()
    assert '"served_count", 0' in report
    assert '"killed_count", 1' in report


def test_proxy_fixture_records_a_tcp_flow_as_killed_and_the_session_continues(
    pytester, monkeypatch, har
):
    """A non-HTTP flow is counted into killed_count, and the session continues past it."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    pytester.makepyfile(f"""
        import socket

        import pytest

        @pytest.mark.parametrize("proxy", [{str(har)!r}], indirect=True)
        def test_it(proxy):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener_port = listener.getsockname()[1]
            with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as client:
                client.sendall(
                    "CONNECT 127.0.0.1:{{0}} HTTP/1.1\\r\\n"
                    "Host: 127.0.0.1:{{0}}\\r\\n\\r\\n".format(listener_port).encode()
                )
                assert b"200" in client.recv(4096)
                client.sendall({_TCP_GARBAGE!r})
                accepted, _ = listener.accept()
                accepted.recv(4096)
                accepted.close()
            listener.close()

        def test_another():
            pass
    """)
    result = pytester.runpytest()
    assert result.ret == 0
    result.assert_outcomes(passed=2)

    run_dir = next(Path("records").iterdir())
    report = (run_dir / "report.jsonl").read_text()
    assert '"killed_count", 1' in report


def test_proxy_fixture_exits_2_on_an_unreadable_recording(pytester, monkeypatch, tmp_path):
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    bad_har = tmp_path / "not_a_har_file"
    bad_har.write_text("not a har file")
    pytester.makepyfile(f"""
        import pytest

        @pytest.mark.parametrize("proxy", [{str(bad_har)!r}], indirect=True)
        def test_it(proxy):
            pass
    """)
    result = pytester.runpytest()
    assert result.ret == 2
    result.stdout.fnmatch_lines(["*proxy: *::test_it*mitmdump exited*"])


def test_a_proxy_nothing_reaches_reports_zero_for_every_recorded_request(har, tmp_path):
    server = proxy.Proxy(har, tmp_path / "proxy.log", tmp_path / "proxy.flows")
    server.start()
    server.stop()
    assert server.served() == {("GET", _HIT_URL): 0}
    assert server.killed() == []


def test_a_cut_trailing_flow_record_yields_the_complete_flows_before_it(har, tmp_path):
    flows_path = tmp_path / "proxy.flows"
    with open(flows_path, "wb") as fo:
        writer = FlowWriter(fo)

        served_req = tutils.treq(
            method=b"GET", host="example.invalid", port=80, scheme=b"http", path=b"/hello"
        )
        served_flow = tflow.tflow(req=served_req, resp=True)
        served_flow.is_replay = "response"
        writer.add(served_flow)

        cut_req = tutils.treq(
            method=b"GET", host="example.invalid", port=80, scheme=b"http", path=b"/mid-write"
        )
        cut_flow = tflow.tflow(req=cut_req, resp=False)
        cut_flow.is_replay = None
        writer.add(cut_flow)

    flows_path.write_bytes(flows_path.read_bytes()[:-5])

    server = proxy.Proxy(har, tmp_path / "proxy.log", flows_path)
    assert server.served() == {("GET", _HIT_URL): 1}
    assert server.killed() == []


def test_a_flow_record_corrupted_before_the_trailing_record_raises(har, tmp_path):
    """Only a cut trailing record is tolerated; a `FlowReadException` anywhere else propagates."""
    flows_path = tmp_path / "proxy.flows"
    with open(flows_path, "wb") as fo:
        writer = FlowWriter(fo)

        served_req = tutils.treq(
            method=b"GET", host="example.invalid", port=80, scheme=b"http", path=b"/hello"
        )
        served_flow = tflow.tflow(req=served_req, resp=True)
        served_flow.is_replay = "response"
        writer.add(served_flow)
        end_of_served = fo.tell()

        trailing_req = tutils.treq(
            method=b"GET", host="example.invalid", port=80, scheme=b"http", path=b"/after"
        )
        trailing_flow = tflow.tflow(req=trailing_req, resp=True)
        trailing_flow.is_replay = None
        writer.add(trailing_flow)

    corrupted = flows_path.read_bytes()
    corrupted = corrupted[:end_of_served] + b"\xff" + corrupted[end_of_served:]
    flows_path.write_bytes(corrupted)

    server = proxy.Proxy(har, tmp_path / "proxy.log", flows_path)
    with pytest.raises(exceptions.FlowReadException):
        server.served()


def test_served_matches_on_method_and_url_together_not_on_either_alone(post_har, tmp_path):
    """A flow sharing the recorded method and url only counts when mitmdump actually replayed
    it, and a flow sharing only the url under a different method never counts, even if it was
    somehow marked as replayed."""
    flows_path = tmp_path / "proxy.flows"
    with open(flows_path, "wb") as fo:
        writer = FlowWriter(fo)

        differing_body_req = tutils.treq(
            method=b"POST",
            host="example.invalid",
            port=80,
            scheme=b"http",
            path=b"/submit",
            content=b"field=other",
        )
        differing_body_flow = tflow.tflow(req=differing_body_req, resp=True)
        differing_body_flow.is_replay = None
        writer.add(differing_body_flow)

        differing_method_req = tutils.treq(
            method=b"GET", host="example.invalid", port=80, scheme=b"http", path=b"/submit"
        )
        differing_method_flow = tflow.tflow(req=differing_method_req, resp=True)
        differing_method_flow.is_replay = "response"
        writer.add(differing_method_flow)

    server = proxy.Proxy(post_har, tmp_path / "proxy.log", flows_path)
    assert server.served() == {("POST", _POST_URL): 0}
