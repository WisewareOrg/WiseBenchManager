"""`Proxy`: replays a recorded HAR with mitmdump, and the `proxy` fixture around it."""

import hashlib
import json
import subprocess
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wisebench import proxy

_HIT_URL = "http://example.invalid/hello"
_HIT_BODY = b"hello-body"
_MISS_URL = "http://example.invalid/missing"

_HTTPS_HIT_URL = "https://wisebench-proxy-test.invalid/hello"
_HTTPS_HIT_BODY = "hello-from-replay"
_HTTPS_MISS_URL = "https://wisebench-proxy-test.invalid/missing"


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


def _fetch(port, url):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{port}"})
    )
    return opener.open(url, timeout=5)


def test_start_serves_the_recorded_response(har, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    server = proxy.Proxy(har, tmp_path / "proxy.log")
    server.start()
    try:
        assert _fetch(server.port, _HIT_URL).read() == _HIT_BODY
    finally:
        server.stop()


def test_a_recorded_request_replays_every_time_it_is_repeated(har, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    server = proxy.Proxy(har, tmp_path / "proxy.log")
    server.start()
    try:
        assert _fetch(server.port, _HIT_URL).read() == _HIT_BODY
        assert _fetch(server.port, _HIT_URL).read() == _HIT_BODY
    finally:
        server.stop()


def test_stop_raises_on_a_request_outside_the_recording(har, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    server = proxy.Proxy(har, tmp_path / "proxy.log")
    server.start()
    with pytest.raises((urllib.error.URLError, ConnectionError)):
        _fetch(server.port, _MISS_URL)
    with pytest.raises(proxy.ProxyError, match="killed non-replay request"):
        server.stop()


def test_https_replay_needs_no_reachable_upstream(https_har, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    server = proxy.Proxy(https_har, tmp_path / "proxy.log")
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
        with pytest.raises(proxy.ProxyError, match="killed non-replay request"):
            server.stop()


def test_start_raises_and_logs_mitmdumps_own_error_on_an_unreadable_recording(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    bad_har = tmp_path / "not_a_har_file"
    bad_har.write_text("not a har file")
    server = proxy.Proxy(bad_har, tmp_path / "proxy.log")
    with pytest.raises(OSError, match="mitmdump exited"):
        server.start()
    assert (tmp_path / "proxy.log").read_text().strip()


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

    proxy_logs = list(run_dir.glob("proxy-test_it-*.log"))
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
    result.stdout.fnmatch_lines(["*proxy: *::test_it*killed non-replay request*"])


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
