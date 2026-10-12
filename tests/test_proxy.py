"""`Proxy`: replays a recorded HAR with mitmdump, and the `proxy` fixture around it."""

import hashlib
import json
import socket
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wisebench import proxy

_HIT_URL = "http://example.invalid/hello"
_HIT_BODY = b"hello-body"
_MISS_URL = "http://example.invalid/missing"


def _har_text():
    now = datetime.now(UTC).isoformat()
    entry = {
        "startedDateTime": now,
        "time": 0,
        "request": {"method": "GET", "url": _HIT_URL, "httpVersion": "HTTP/1.1", "headers": []},
        "response": {
            "status": 200,
            "httpVersion": "HTTP/1.1",
            "headers": [{"name": "Content-Type", "value": "text/plain"}],
            "content": {"text": _HIT_BODY.decode()},
        },
    }
    return json.dumps({"log": {"version": "1.2", "entries": [entry]}})


@pytest.fixture
def har(tmp_path):
    path = tmp_path / "recording.har"
    path.write_text(_har_text())
    return path


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _fetch(port, url):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{port}"})
    )
    return opener.open(url, timeout=5)


def test_start_serves_the_recorded_response(har, tmp_path):
    server = proxy.Proxy(har, _free_port(), tmp_path / "proxy.log")
    server.start()
    try:
        assert _fetch(server.port, _HIT_URL).read() == _HIT_BODY
    finally:
        server.stop()


def test_stop_is_silent_when_only_recorded_requests_were_served(har, tmp_path):
    server = proxy.Proxy(har, _free_port(), tmp_path / "proxy.log")
    server.start()
    _fetch(server.port, _HIT_URL)
    server.stop()


def test_stop_raises_on_a_request_outside_the_recording(har, tmp_path):
    server = proxy.Proxy(har, _free_port(), tmp_path / "proxy.log")
    server.start()
    with pytest.raises((urllib.error.URLError, ConnectionError)):
        _fetch(server.port, _MISS_URL)
    with pytest.raises(proxy.ProxyError, match="killed non-replay request"):
        server.stop()


def test_proxy_fixture_yields_a_port_and_records_the_recordings_sha256(pytester, monkeypatch, har):
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    pytester.makepyfile(f"""
        import urllib.request

        import pytest

        @pytest.mark.parametrize("proxy", [{str(har)!r}], indirect=True)
        def test_it(proxy):
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({{"http": f"http://127.0.0.1:{{proxy}}"}})
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

    proxy_logs = list(run_dir.glob("proxy-*.log"))
    assert len(proxy_logs) == 1
    assert "[replay]" in proxy_logs[0].read_text()


def test_proxy_fixture_exits_2_on_a_request_outside_the_recording(pytester, monkeypatch, har):
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    pytester.makepyfile(f"""
        import urllib.error
        import urllib.request

        import pytest

        @pytest.mark.parametrize("proxy", [{str(har)!r}], indirect=True)
        def test_it(proxy):
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({{"http": f"http://127.0.0.1:{{proxy}}"}})
            )
            with pytest.raises((urllib.error.URLError, ConnectionError)):
                opener.open({_MISS_URL!r}, timeout=5)
    """)
    result = pytester.runpytest()
    assert result.ret == 2
    result.stdout.fnmatch_lines(["*proxy: *killed non-replay request*"])
