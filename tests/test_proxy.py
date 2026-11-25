"""`Proxy`: replays a recorded HAR with mitmdump, and the `proxy` fixture around it."""

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wisebench import proxy

_TOOLS_ENV = Path(__file__).resolve().parents[1] / "tools" / "mitmproxy"

_HIT_URL = "http://example.invalid/hello"
_HIT_BODY = b"hello-body"
_MISS_URL = "http://example.invalid/missing"

_HTTPS_HIT_URL = "https://wisebench-proxy-test.invalid/hello"
_HTTPS_HIT_BODY = "hello-from-replay"
_HTTPS_MISS_URL = "https://wisebench-proxy-test.invalid/missing"

_TCP_GARBAGE = b"not-http-or-tls\n"

_BOTH_METHODS_URL = "http://example.invalid/submit"
_GET_RESPONSE = "from-get"
_POST_RESPONSE = "from-post"


@pytest.fixture(autouse=True)
def _tools_env(bench_clone):
    """Give the fake clone this repository's `tools/mitmproxy`, synced, as its own."""
    (bench_clone.root / "tools").mkdir()
    (bench_clone.root / "tools" / "mitmproxy").symlink_to(_TOOLS_ENV, target_is_directory=True)


def _har_entry(url, body, method="GET", request_body=None):
    request = {"method": method, "url": url, "httpVersion": "HTTP/1.1", "headers": []}
    if request_body is not None:
        request["postData"] = {"text": request_body}
    return {
        "startedDateTime": datetime.now(UTC).isoformat(),
        "time": 0,
        "request": request,
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
def get_and_post_har(tmp_path):
    """One URL recorded twice: as a GET with one response and as a POST with another."""
    path = tmp_path / "get_and_post_recording.har"
    entries = [
        _har_entry(_BOTH_METHODS_URL, _GET_RESPONSE),
        _har_entry(_BOTH_METHODS_URL, _POST_RESPONSE, method="POST", request_body="field=value"),
    ]
    path.write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}))
    return path


def _fetch(port, url):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{port}"})
    )
    return opener.open(url, timeout=5)


def _recorded_hit_then_miss(har, tmp_path):
    """A real flow file from mitmdump: one replayed hit, then one killed miss, in that order."""
    flows_path = tmp_path / "recorded.flows"
    server = proxy.Proxy(har, tmp_path / "recorded.log", flows_path)
    server.start()
    try:
        assert _fetch(server.port, _HIT_URL).read() == _HIT_BODY
        with pytest.raises((urllib.error.URLError, ConnectionError)):
            _fetch(server.port, _MISS_URL)
    finally:
        server.stop()
    return flows_path.read_bytes()


def _end_of_first_record(data):
    """The offset just past the first tnetstring record (`<length>:<payload><type>`)."""
    length, _, _ = data.partition(b":")
    return len(length) + 1 + int(length) + 1


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
    """killed() lists one entry per miss, not deduplicated, named by its URL each time."""
    server = proxy.Proxy(har, tmp_path / "proxy.log", tmp_path / "proxy.flows")
    server.start()
    try:
        with pytest.raises((urllib.error.URLError, ConnectionError)):
            _fetch(server.port, _MISS_URL)
        with pytest.raises((urllib.error.URLError, ConnectionError)):
            _fetch(server.port, _MISS_URL)
        assert server.killed() == [_MISS_URL, _MISS_URL]
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
    listener.settimeout(5)
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
            accepted.settimeout(5)
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
    pytester, monkeypatch, har, bench_clone
):
    """The fixture yields a `Proxy`, and records the sha256, served_count and killed_count."""
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

    run_dir = next(bench_clone.records.iterdir())
    with open(har, "rb") as f:
        expected_sha256 = hashlib.file_digest(f, "sha256").hexdigest()
    report = (run_dir / "report.jsonl").read_text()
    assert f'"recording_sha256", "{expected_sha256}"' in report
    assert '"served_count", 1]' in report
    assert '"killed_count", 0]' in report

    proxy_logs = list(run_dir.glob("proxy-test_it-*.log"))
    assert len(proxy_logs) == 1
    assert "[replay]" in proxy_logs[0].read_text()


def test_proxy_fixture_records_served_count_as_a_sum_of_replays_not_a_count_of_requests(
    pytester, monkeypatch, har, bench_clone
):
    """`served_count` sums every replay, not the count of distinct recorded requests."""
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

    run_dir = next(bench_clone.records.iterdir())
    report = (run_dir / "report.jsonl").read_text()
    assert '"served_count", 2]' in report


def test_proxy_fixture_records_a_kill_and_the_session_continues(
    pytester, monkeypatch, har, bench_clone
):
    """A kill never fails the test or ends the session; served_count counts only replays."""
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

    run_dir = next(bench_clone.records.iterdir())
    report = (run_dir / "report.jsonl").read_text()
    assert '"served_count", 0]' in report
    assert '"killed_count", 1]' in report


def test_proxy_fixture_records_a_tcp_flow_as_killed_and_the_session_continues(
    pytester, monkeypatch, har, bench_clone
):
    """A non-HTTP flow is counted into killed_count, and the session continues past it."""
    pytester.makepyfile(f"""
        import socket

        import pytest

        @pytest.mark.parametrize("proxy", [{str(har)!r}], indirect=True)
        def test_it(proxy):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.settimeout(5)
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
                accepted.settimeout(5)
                accepted.recv(4096)
                accepted.close()
            listener.close()

        def test_another():
            pass
    """)
    result = pytester.runpytest()
    assert result.ret == 0
    result.assert_outcomes(passed=2)

    run_dir = next(bench_clone.records.iterdir())
    report = (run_dir / "report.jsonl").read_text()
    assert '"killed_count", 1]' in report


def test_proxy_fixture_exits_2_on_an_unreadable_recording(pytester, monkeypatch, tmp_path):
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
    """served() reports 0 for a recorded request nothing reached, and killed() stays empty."""
    server = proxy.Proxy(har, tmp_path / "proxy.log", tmp_path / "proxy.flows")
    server.start()
    server.stop()
    assert server.served() == {("GET", _HIT_URL): 0}
    assert server.killed() == []


def test_a_cut_trailing_flow_record_yields_the_complete_flows_before_it(har, tmp_path):
    """A record cut by mitmdump mid-write is tolerated; served()/killed() use what precedes it."""
    recorded = _recorded_hit_then_miss(har, tmp_path)
    flows_path = tmp_path / "cut.flows"
    flows_path.write_bytes(recorded[:-5])

    server = proxy.Proxy(har, tmp_path / "proxy.log", flows_path)
    assert server.served() == {("GET", _HIT_URL): 1}
    assert server.killed() == []


def test_a_flow_record_corrupted_before_the_trailing_record_raises(har, tmp_path, capfd):
    """Only a cut trailing record is tolerated; any other read failure raises, its cause shown."""
    recorded = _recorded_hit_then_miss(har, tmp_path)
    end = _end_of_first_record(recorded)
    flows_path = tmp_path / "corrupted.flows"
    flows_path.write_bytes(recorded[:end] + b"\xff" + recorded[end:])

    server = proxy.Proxy(har, tmp_path / "proxy.log", flows_path)
    with pytest.raises(subprocess.CalledProcessError):
        server.served()
    assert "FlowReadException" in capfd.readouterr().err


def test_served_matches_on_method_and_url_together_not_on_the_url_alone(get_and_post_har, tmp_path):
    """One GET of a URL recorded as both GET and POST is served once, as the GET only."""
    server = proxy.Proxy(get_and_post_har, tmp_path / "proxy.log", tmp_path / "proxy.flows")
    server.start()
    try:
        assert _fetch(server.port, _BOTH_METHODS_URL).read() == _GET_RESPONSE.encode()
    finally:
        server.stop()
    assert server.served() == {("GET", _BOTH_METHODS_URL): 1, ("POST", _BOTH_METHODS_URL): 0}


def test_a_killed_request_is_never_counted_as_served_though_its_url_is_recorded(
    get_and_post_har, tmp_path
):
    """A POST whose body is not the recorded one is killed; only the recorded body is served."""
    server = proxy.Proxy(get_and_post_har, tmp_path / "proxy.log", tmp_path / "proxy.flows")
    server.start()
    try:
        recorded = urllib.request.Request(_BOTH_METHODS_URL, data=b"field=value")
        assert _fetch(server.port, recorded).read() == _POST_RESPONSE.encode()
        other = urllib.request.Request(_BOTH_METHODS_URL, data=b"field=other")
        with pytest.raises((urllib.error.URLError, ConnectionError)):
            _fetch(server.port, other)
    finally:
        server.stop()
    assert server.served() == {("GET", _BOTH_METHODS_URL): 0, ("POST", _BOTH_METHODS_URL): 1}
    assert server.killed() == [_BOTH_METHODS_URL]


def test_start_runs_the_clones_own_mitmdump_never_one_found_on_path(har, tmp_path, monkeypatch):
    """A `mitmdump` earlier on PATH is unpinned; the clone's locked one is the one that runs."""
    path_bin = tmp_path / "path-bin"
    path_bin.mkdir()
    impostor = path_bin / "mitmdump"
    impostor.write_text("#!/bin/sh\nexit 1\n")
    impostor.chmod(0o755)
    monkeypatch.setenv("PATH", f"{path_bin}{os.pathsep}{os.environ['PATH']}")

    server = proxy.Proxy(har, tmp_path / "proxy.log", tmp_path / "proxy.flows")
    server.start()
    try:
        assert _fetch(server.port, _HIT_URL).read() == _HIT_BODY
    finally:
        server.stop()
    assert server.served() == {("GET", _HIT_URL): 1}


def test_the_runner_and_its_plugin_import_without_mitmproxy():
    """mitmproxy runs only as processes from its own environment; the runner never imports it."""
    blocked = "import sys; sys.modules['mitmproxy'] = None; import wisebench.plugin"
    result = subprocess.run([sys.executable, "-c", blocked], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
