"""mitmdump serving one recorded HAR, over a loopback port it picks itself."""

import hashlib
import json
import socket
import subprocess
from pathlib import Path

import pytest
from tenacity import Retrying, stop_after_delay, wait_fixed

from wisebench import clone

_LISTEN_TIMEOUT = 10
_LISTEN_INTERVAL = 0.1


class Proxy:
    """One mitmdump process replaying one recorded HAR, on a loopback port it picks itself.

    Holds the subprocess, the port it is listening on, and the log and flow files mitmdump
    writes to; starts mitmdump and waits for it to accept connections. mitmdump, and the
    `flows.py` that reads flows back with mitmproxy's own `FlowReader`, run from the clone's
    `tools/mitmproxy` environment. `served` and `killed` report what mitmdump did from that
    read-back, and work both while mitmdump is running and after `stop`.
    """

    def __init__(self, har, log_path, flows_path):
        """Bind one Proxy to a recording, a log file and a flow file.

        Args:
            har (str | os.PathLike): Path to the HAR file `mitmdump --server-replay` reads.
            log_path (str | os.PathLike): Path to write mitmdump's combined stdout and stderr to.
            flows_path (str | os.PathLike): Path mitmdump saves every flow it handles to, with
                `--save-stream-file`. `served` and `killed` read it back.
        """
        self.har = har
        self.log_path = log_path
        self.flows_path = flows_path
        self._process = None
        self._port = None

    @property
    def port(self):
        """int: The loopback port mitmdump is listening on, set once `start` has run."""
        return self._port

    def start(self):
        """Pick a free loopback port, start mitmdump on it, and wait until it is listening.

        Kills mitmdump before raising if it never started listening, so a caller never has to
        track whether a failed `start` left a process running.

        Raises:
            OSError: mitmdump exited before it started listening, or never accepted a
                connection within the wait bound.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            self._port = sock.getsockname()[1]

        mitmdump = str(_tools() / ".venv" / "bin" / "mitmdump")
        with open(self.log_path, "w") as log:
            self._process = subprocess.Popen(
                [
                    mitmdump,
                    "--server-replay",
                    str(self.har),
                    "--save-stream-file",
                    str(self.flows_path),
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
        try:
            self._wait_until_listening()
        except Exception:
            self._process.kill()
            self._process.wait()
            raise

    def stop(self):
        """Terminate mitmdump."""
        self._process.terminate()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()

    def served(self):
        """Map every request the recording holds to how many times it was replayed.

        Returns:
            dict[tuple[str, str], int]: Every `(method, url)` read from the HAR, mapped to the
            count of flows in the flow file with that exact `(request.method, request.url)` and
            `is_replay == "response"`. A recorded request mitmdump never served maps to 0. A
            non-HTTP flow is never counted, since it is never a replay response.

        Raises:
            subprocess.CalledProcessError: The HAR could not be read, or see `_read_flows`.
        """
        recorded = [(f["method"], f["url"]) for f in _flows(self.har)]
        counts = dict.fromkeys(recorded, 0)
        for f in self._read_flows():
            if f["is_replay"] == "response":
                key = (f["method"], f["url"])
                if key in counts:
                    counts[key] += 1
        return counts

    def killed(self):
        """List every flow mitmdump did not serve from the recording, by what it named.

        Returns:
            list[str]: For every flow in the flow file whose `is_replay` is not `"response"`,
            in the order mitmdump wrote them: `request.url` for an HTTP flow, or
            `tcp://<host>:<port>` from `server_conn.address` for any other flow. A flow
            mitmdump saves unanswered at shutdown counts as killed.

        Raises:
            subprocess.CalledProcessError: See `_read_flows`.
        """
        return [f["url"] for f in self._read_flows() if f["is_replay"] != "response"]

    def _read_flows(self):
        """Read the complete flows in a snapshot of `flows_path`.

        A read error once the whole snapshot is consumed is a trailing record mitmdump is still
        writing; the flows before it are returned.

        Returns:
            list[dict]: The flows before any partly written trailing record, as `_flows` gives
            them.

        Raises:
            subprocess.CalledProcessError: The reader failed before the end of the snapshot.
        """
        return _flows(self.flows_path, growing=True)

    def _wait_until_listening(self):
        """Retry a loopback connect to `self.port` until it succeeds.

        Stops early if mitmdump exits or the wait bound elapses.

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


def _tools():
    """The clone's own mitmproxy environment: `<clone>/tools/mitmproxy`."""
    return clone.root() / "tools" / "mitmproxy"


def _flows(path, growing=False):
    """Read a flow file or HAR file with `tools/mitmproxy/flows.py`, in mitmproxy's environment.

    Args:
        path (str | os.PathLike): The file to read.
        growing (bool): The file is one mitmdump may still be writing, so a partly written
            trailing record is tolerated.

    Returns:
        list[dict]: One `{"method", "url", "is_replay"}` per flow, in file order. `url` is
        `request.url` for an HTTP flow and `tcp://<host>:<port>` for any other, whose
        `method` is None.

    Raises:
        subprocess.CalledProcessError: The read failed; its `stderr` holds mitmproxy's error.
    """
    tools = _tools()
    args = [str(tools / ".venv" / "bin" / "python"), "-I", str(tools / "flows.py")]
    if growing:
        args.append("--growing")
    result = subprocess.run([*args, str(path)], check=True, capture_output=True, text=True)
    return [json.loads(line) for line in result.stdout.splitlines()]


@pytest.fixture
def proxy(request, record_property):
    """Start mitmdump replaying `request.param`'s HAR file, and yield the `Proxy`.

    `request.param` is the recording's path; a test or its conftest supplies it with
    `@pytest.mark.parametrize("proxy", [<path>], indirect=True)` -- which set to replay is the
    layer's own choice, not this fixture's. The recording's sha256 is recorded through
    `record_property` under the key `recording_sha256`. mitmdump's combined stdout and stderr,
    and the flow file `served` and `killed` read, are written to one pair of files per fixture
    use, named for the test and an incrementing per-session count, beside this run's
    `report.jsonl`. A setup failure -- the recording unreadable, mitmdump missing or never
    listening -- ends the session with `pytest.exit(..., returncode=2)`, naming the test and the
    log's last lines. At teardown, after `stop`, `served_count` (the total of `served()`) and
    `killed_count` (the length of `killed()`) are recorded through `record_property`. A kill
    neither fails the test nor ends the session; a test asserts on `served()` or `killed()`
    itself.

    Args:
        request (pytest.FixtureRequest): Supplies the recording path as `request.param`.
        record_property: pytest's built-in fixture this fixture records the recording's sha256
            and the served and killed counts through.

    Yields:
        Proxy: The running proxy, whose `port`, `served()` and `killed()` a test reads.
    """
    har = request.param
    run_dir = Path(request.config.option.report_log).parent
    count = getattr(request.session, "_wisebench_proxy_count", 0)
    request.session._wisebench_proxy_count = count + 1
    log_path = run_dir / f"proxy-{request.node.originalname}-{count}.log"
    flows_path = run_dir / f"proxy-{request.node.originalname}-{count}.flows"

    server = Proxy(har, log_path, flows_path)
    try:
        server.start()
        with open(har, "rb") as f:
            record_property("recording_sha256", hashlib.file_digest(f, "sha256").hexdigest())
    except Exception as exc:
        tail = Path(log_path).read_text().strip() if Path(log_path).exists() else ""
        reason = f"proxy: {request.node.nodeid}: {exc}"
        pytest.exit(f"{reason}\n{tail}" if tail else reason, returncode=2)

    yield server

    server.stop()
    record_property("served_count", sum(server.served().values()))
    record_property("killed_count", len(server.killed()))
