"""`status.watch` in a child process on a real terminal: the bytes it writes, its exit, its end."""

import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path

import pexpect
import pytest
from status_helpers import (
    STAMP,
    merge,
    output,
    queued,
    reports,
    run_log,
    started_log,
)
from tenacity import retry, stop_after_delay, wait_fixed

ENTERED = b"\x1b[?1049h"


def hostile(label):
    """Text that retitles a terminal and clears it; `label` tells where it came from."""
    return f"\x1b]0;{label}\x07\x9b2J"


class TestTerminal:
    @pytest.fixture(autouse=True)
    def _terminal(self, bench_clone, tmp_path):
        self.clone, self.began = bench_clone, tmp_path / "began"
        with contextlib.ExitStack() as self.cleanup:
            yield

    def watch(self, prelude=""):
        """Start the view in a child process, after `prelude`, and wait for its first screen."""
        child = self.start(prelude)
        child.expect_exact(b"1 Run")
        return child

    def start(self, prelude):
        """Start the view in a child process, after `prelude`."""
        code = (
            "import sys, time\nfrom pathlib import Path\nfrom wisebench import status\n"
            f"{prelude}\n"
            f"sys.exit(status.watch(Path({str(self.clone.checkout)!r}), "
            f"Path({str(self.clone.records)!r})))"
        )
        env = {**os.environ, "TERM": "xterm-256color"}
        child = pexpect.spawn(
            sys.executable, ["-c", code], env=env, dimensions=(34, 120), timeout=30
        )
        self.cleanup.callback(child.close, force=True)
        self.output = child.logfile_read = io.BytesIO()
        return child

    def exit_status(self, child):
        """Wait for the child to end and return its exit status."""
        child.expect(pexpect.EOF)
        child.close()
        return child.exitstatus

    def record(self, name, text, target_suite=()):
        """Write a run's `bench.log` and its target suite's report."""
        suite = self.clone.records / name / "target_suite"
        suite.mkdir(parents=True)
        (suite / "report.jsonl").write_text("".join(json.dumps(r) + "\n" for r in target_suite))
        (suite.parent / "bench.log").write_text(text)

    @pytest.mark.parametrize("keys", ["q", "\x03"], ids=["q", "Ctrl-C"])
    def test_quitting_exits_0_and_the_mouse_is_never_reported(self, keys):
        child = self.watch()
        child.send(keys)
        status = self.exit_status(child)

        written = self.output.getvalue()
        assert status == 0, "quitting is not a failure"
        assert ENTERED in written, "the view took the terminal"
        assert not [m for m in (1000, 1002, 1003, 1006) if b"?%dh" % m in written], "no mouse mode"

    def test_a_crash_exits_1_with_its_traceback(self):
        child = self.start("status.Records.runs = lambda self: 1 / 0")
        status = self.exit_status(child)

        assert status == 1, "a crash is a failure"
        assert b"ZeroDivisionError" in self.output.getvalue(), "what went wrong"

    def test_text_from_outside_never_drives_the_terminal(self):
        self.record(STAMP, run_log("build", "error", build=output(hostile("log"))))
        (self.clone.records / "DISABLED").write_text(f"2026-11-10T03:15:00\n{hostile('reason')}\n")
        self.record(
            f"2025{hostile('record')}",
            run_log(
                "target_suite",
                "failed",
                kind="test",
                ref=hostile("ref"),
                pr=None,
                target_suite=queued("a") + started_log("a", 26, hostile("test")),
            ),
            reports("a", "failed"),
        )
        entries = [merge(title=hostile("title"))]

        child = self.watch(f"status.Queue.entries = lambda self: {entries!r}")
        for keys, labels in (
            ("", ("reason", "log")),
            ("2", ("title",)),
            ("3", ("ref",)),
            ("\x1b[B\r", ("record", "test")),
        ):
            child.send(keys)
            for label in labels:
                child.expect(rf"\^\[\]0;{label}".encode())
        child.send("q")
        self.exit_status(child)

        written = self.output.getvalue()
        for label in ("reason", "log", "title", "ref", "record", "test"):
            assert f"\x1b]0;{label}".encode() not in written, f"the {label} is shown as text"
        assert "\x9b".encode() not in written, "no C1 control reaches the terminal"

    def test_quitting_does_not_wait_for_a_read(self):
        child = self.watch(
            "def entries(self):\n"
            f"    Path({str(self.began)!r}).touch()\n"
            "    time.sleep(8)\n"
            "status.Queue.entries = entries"
        )
        child.send("2")
        self.wait_for(self.began)
        start = time.monotonic()
        child.send("q")
        status = self.exit_status(child)

        assert status == 0, "quitting is not a failure"
        assert time.monotonic() - start < 3, "the read had 8 s to go"

    @retry(stop=stop_after_delay(20), wait=wait_fixed(0.05), reraise=True)
    def wait_for(self, path):
        """Wait for a file the child makes."""
        assert Path(path).exists(), f"{path} was made"
