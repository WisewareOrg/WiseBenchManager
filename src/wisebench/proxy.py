"""mitmdump serving one recorded HAR, over a loopback port it picks itself."""

import hashlib
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from tenacity import Retrying, stop_after_delay, wait_fixed

_LISTEN_TIMEOUT = 10
_LISTEN_INTERVAL = 0.1


class ProxyError(Exception):
    """mitmdump's log named a request the recording did not hold."""


class Proxy:
    """One mitmdump process replaying one recorded HAR, on a loopback port it picks itself.

    Holds the subprocess, the port it is listening on, and the log file mitmdump writes to;
    starts mitmdump and waits for it to accept connections, and on stop terminates it and
    raises if the log names a killed non-replay request.
    """

    def __init__(self, har, log_path):
        """Bind one Proxy to a recording and a log file.

        Args:
            har (str | os.PathLike): Path to the HAR file `mitmdump --server-replay` reads.
            log_path (str | os.PathLike): Path to write mitmdump's combined stdout and stderr to.
        """
        self.har = har
        self.log_path = log_path
        self._process = None
        self._port = None

    @property
    def port(self):
        """int: The loopback port mitmdump is listening on, set once `start` has run."""
        return self._port

    def start(self):
        """Pick a free loopback port, start mitmdump on it, and wait until it is listening.

        Raises:
            OSError: mitmdump exited before it started listening, or never accepted a
                connection within the wait bound.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            self._port = sock.getsockname()[1]

        mitmdump = str(Path(sys.executable).parent / "mitmdump")
        with open(self.log_path, "w") as log:
            self._process = subprocess.Popen(
                [
                    mitmdump,
                    "--server-replay",
                    str(self.har),
                    "--set",
                    "server_replay_extra=kill",
                    "--set",
                    "server_replay_reuse=true",
                    "--set",
                    "connection_strategy=lazy",
                    "--set",
                    "upstream_cert=false",
                    "--listen-host",
                    "127.0.0.1",
                    "--listen-port",
                    str(self._port),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        self._wait_until_listening()

    def stop(self):
        """Terminate mitmdump and raise if its log names a killed non-replay request.

        Raises:
            ProxyError: The log contains a `killed non-replay request` line.
        """
        self._process.terminate()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        for line in Path(self.log_path).read_text().splitlines():
            if "killed non-replay request" in line:
                raise ProxyError(line)

    def _wait_until_listening(self):
        """Retry a loopback connect to `self.port` until it succeeds, mitmdump exits, or the
        wait bound elapses.

        Checking `self._process.poll()` outside the retried block, rather than inside it,
        fails at once on a dead process instead of retrying it for the full wait bound.

        Raises:
            OSError: mitmdump exited, or no connection succeeded within the wait bound.
        """
        for attempt in Retrying(
            stop=stop_after_delay(_LISTEN_TIMEOUT),
            wait=wait_fixed(_LISTEN_INTERVAL),
            reraise=True,
        ):
            if self._process.poll() is not None:
                raise OSError(f"mitmdump exited {self._process.returncode}")
            with attempt:
                address = ("127.0.0.1", self._port)
                with socket.create_connection(address, timeout=_LISTEN_INTERVAL):
                    pass


@pytest.fixture
def proxy(request, record_property):
    """Start mitmdump replaying `request.param`'s HAR file, and yield its loopback port.

    `request.param` is the recording's path; a test or its conftest supplies it with
    `@pytest.mark.parametrize("proxy", [<path>], indirect=True)` -- which set to replay is the
    layer's own choice, not this fixture's. The recording's sha256 is recorded through
    `record_property` under the key `recording_sha256`. mitmdump's combined stdout and stderr
    are written to one file per fixture use, named for the test and an incrementing per-session
    count, beside this run's `report.jsonl`. A setup failure -- the recording unreadable,
    mitmdump missing or never listening -- ends the session with `pytest.exit(...,
    returncode=2)`, naming the test and the log's last lines. At teardown, a
    `killed non-replay request` line in the log means the board asked for something the
    recording does not hold, and ends the session with `pytest.exit(..., returncode=2)` the same
    way, so the run is reported as the bench's own failure.

    Args:
        request (pytest.FixtureRequest): Supplies the recording path as `request.param`.
        record_property: pytest's built-in fixture this fixture records the recording's sha256
            through.

    Yields:
        int: The loopback port mitmdump is listening on.
    """
    har = request.param
    run_dir = Path(request.config.option.report_log).parent
    count = getattr(request.session, "_wisebench_proxy_count", 0)
    request.session._wisebench_proxy_count = count + 1
    log_path = run_dir / f"proxy-{request.node.originalname}-{count}.log"

    server = Proxy(har, log_path)
    try:
        server.start()
        with open(har, "rb") as f:
            record_property("recording_sha256", hashlib.file_digest(f, "sha256").hexdigest())
    except Exception as exc:
        tail = Path(log_path).read_text().strip() if Path(log_path).exists() else ""
        reason = f"proxy: {request.node.nodeid}: {exc}"
        pytest.exit(f"{reason}\n{tail}" if tail else reason, returncode=2)

    yield server.port
    try:
        server.stop()
    except ProxyError as exc:
        pytest.exit(f"proxy: {request.node.nodeid}: {exc}", returncode=2)
