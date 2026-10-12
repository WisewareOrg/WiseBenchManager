"""mitmdump serving one recorded HAR, over a loopback port the fixture picks itself."""

import hashlib
import socket
import subprocess
from pathlib import Path

import pytest
from tenacity import Retrying, stop_after_delay, wait_fixed

_LISTEN_TIMEOUT = 10
_LISTEN_INTERVAL = 0.1


class ProxyError(Exception):
    """mitmdump's log named a request the recording did not hold."""


class Proxy:
    """One mitmdump process replaying one recorded HAR, on a loopback port it is given.

    Holds the subprocess and the log file mitmdump writes to; starts mitmdump and waits for it
    to accept connections, and on stop terminates it and raises if the log names a killed
    non-replay request.
    """

    def __init__(self, har, port, log_path):
        """Bind one Proxy to a recording, a port and a log file.

        Args:
            har (str | os.PathLike): Path to the HAR file `mitmdump --server-replay` reads.
            port (int): The loopback port mitmdump listens on.
            log_path (str | os.PathLike): Path to write mitmdump's combined stdout and stderr to.
        """
        self.har = har
        self.port = port
        self.log_path = log_path
        self._process = None

    def start(self):
        """Start mitmdump and wait until it accepts connections on `self.port`.

        Raises:
            tenacity.RetryError: mitmdump never accepted a connection within the wait bound.
        """
        with open(self.log_path, "w") as log:
            self._process = subprocess.Popen(
                [
                    "mitmdump",
                    "--server-replay",
                    str(self.har),
                    "--set",
                    "server_replay_extra=kill",
                    "--set",
                    "upstream_cert=false",
                    "--listen-host",
                    "127.0.0.1",
                    "--listen-port",
                    str(self.port),
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
        """Retry a loopback connect to `self.port` until it succeeds or the wait bound elapses.

        Raises:
            tenacity.RetryError: No connection succeeded within the wait bound.
        """
        for attempt in Retrying(
            stop=stop_after_delay(_LISTEN_TIMEOUT),
            wait=wait_fixed(_LISTEN_INTERVAL),
            reraise=True,
        ):
            with attempt:
                address = ("127.0.0.1", self.port)
                with socket.create_connection(address, timeout=_LISTEN_INTERVAL):
                    pass


@pytest.fixture
def proxy(request, record_property):
    """Start mitmdump replaying `request.param`'s HAR file, and yield its loopback port.

    `request.param` is the recording's path; a test or its conftest supplies it with
    `@pytest.mark.parametrize("proxy", [<path>], indirect=True)` -- which set to replay is the
    layer's own choice, not this fixture's. The recording's sha256 is recorded through
    `record_property` under the key `recording_sha256`. mitmdump's combined stdout and stderr
    are written to one file named for the test's node id, beside this run's `report.jsonl`. At
    teardown, a `killed non-replay request` line in that file means the board asked for
    something the recording does not hold, and ends the session with `pytest.exit(...,
    returncode=2)` so the run is reported as the bench's own failure.

    Args:
        request (pytest.FixtureRequest): Supplies the recording path as `request.param`.
        record_property: pytest's built-in fixture this fixture records the recording's sha256
            through.

    Yields:
        int: The loopback port mitmdump is listening on.

    Raises:
        OSError: `request.param` could not be opened to compute its sha256.
    """
    har = request.param
    with open(har, "rb") as f:
        record_property("recording_sha256", hashlib.file_digest(f, "sha256").hexdigest())

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    run_dir = Path(request.config.option.report_log).parent
    log_path = run_dir / f"proxy-{request.node.nodeid.replace('/', '_')}.log"

    server = Proxy(har, port, log_path)
    server.start()
    yield server.port
    try:
        server.stop()
    except ProxyError as exc:
        pytest.exit(f"proxy: {exc}", returncode=2)
