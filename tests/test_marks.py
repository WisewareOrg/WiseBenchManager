"""`marks`: the writer/parser pair for every `bench:` marker line (ADR 0002's grammar)."""

import os
from datetime import datetime

import pytest

from wisebench import marks


def _written_lines(fd_writer):
    """Run one write call through a real pipe and return its decoded lines.

    Args:
        fd_writer (Callable[[int], None]): Called with the pipe's write fd.

    Returns:
        list[str]: Each line written, newlines stripped.
    """
    r, w = os.pipe()
    try:
        fd_writer(w)
    finally:
        os.close(w)
    data = b""
    while True:
        chunk = os.read(r, 4096)
        if not chunk:
            break
        data += chunk
    os.close(r)
    return data.decode().splitlines()


def _assert_valid_iso(ts):
    datetime.fromisoformat(ts)  # raises ValueError if not a valid ISO-8601 instant


def test_run_started_round_trips_pid_kind_and_ref(monkeypatch):
    """write_run_started/parse round-trip pid, kind and ref with neither pr nor skip-build."""
    [line] = _written_lines(lambda w: marks.write_run_started(4242, "test", "HEAD", fd=w))
    parsed = marks.parse(line)
    assert parsed["type"] == "run_started"
    _assert_valid_iso(parsed["ts"])
    assert parsed["pid"] == 4242
    assert parsed["kind"] == "test"
    assert parsed["ref"] == "HEAD"
    assert parsed["pr"] is None
    assert parsed["skip_build"] is False


def test_run_started_round_trips_a_ref_containing_spaces():
    """write_run_started/parse round-trip a ref containing spaces, with no pr or skip-build."""
    [line] = _written_lines(
        lambda w: marks.write_run_started(1, "test", "a branch with spaces", fd=w)
    )
    parsed = marks.parse(line)
    assert parsed["ref"] == "a branch with spaces"


def test_run_started_round_trips_pr_and_skip_build(monkeypatch):
    """write_run_started/parse round-trip pr= and the skip-build flag together."""
    [line] = _written_lines(
        lambda w: marks.write_run_started(99, "run", "cafef00d", pr=14, skip_build=True, fd=w)
    )
    parsed = marks.parse(line)
    assert parsed["kind"] == "run"
    assert parsed["ref"] == "cafef00d"
    assert parsed["pr"] == 14
    assert parsed["skip_build"] is True


def test_stage_started_round_trips_the_stage_name():
    """write_stage_started/parse round-trip the stage name."""
    [line] = _written_lines(lambda w: marks.write_stage_started("precondition", fd=w))
    parsed = marks.parse(line)
    assert parsed["type"] == "stage_started"
    _assert_valid_iso(parsed["ts"])
    assert parsed["name"] == "precondition"


def test_stage_result_round_trips_a_plain_result():
    """write_stage_result/parse round-trip a result with neither sha nor reason."""
    [line] = _written_lines(lambda w: marks.write_stage_result("build", "passed", fd=w))
    parsed = marks.parse(line)
    assert parsed["type"] == "stage_result"
    assert parsed["name"] == "build"
    assert parsed["result"] == "passed"
    assert parsed["sha"] is None
    assert parsed["reason"] is None


def test_stage_result_round_trips_sha_on_checkout_passed():
    """write_stage_result/parse round-trip sha= riding on a passed checkout."""
    [line] = _written_lines(
        lambda w: marks.write_stage_result("checkout", "passed", sha="a" * 40, fd=w)
    )
    parsed = marks.parse(line)
    assert parsed["name"] == "checkout"
    assert parsed["result"] == "passed"
    assert parsed["sha"] == "a" * 40
    assert parsed["reason"] is None


def test_stage_result_round_trips_a_reason_containing_spaces():
    """write_stage_result/parse round-trip a free-text reason containing spaces."""
    [line] = _written_lines(
        lambda w: marks.write_stage_result(
            "precondition", "error", reason="precondition exited 9, see log", fd=w
        )
    )
    parsed = marks.parse(line)
    assert parsed["name"] == "precondition"
    assert parsed["result"] == "error"
    assert parsed["reason"] == "precondition exited 9, see log"
    assert parsed["sha"] is None


def test_collected_round_trips_the_count():
    """write_collected/parse round-trip the collected count."""
    [line] = _written_lines(lambda w: marks.write_collected(37, fd=w))
    parsed = marks.parse(line)
    assert parsed["type"] == "collected"
    assert parsed["n"] == 37


def test_setup_started_round_trips_the_fixture_name():
    """write_setup_started/parse round-trip the fixture name."""
    [line] = _written_lines(lambda w: marks.write_setup_started("bench_candidate", fd=w))
    parsed = marks.parse(line)
    assert parsed["type"] == "setup_started"
    assert parsed["fixture"] == "bench_candidate"


def test_setup_result_round_trips_the_fixture_name_and_result():
    """write_setup_result/parse round-trip the fixture name and its outcome."""
    [line] = _written_lines(lambda w: marks.write_setup_result("bench_candidate", "done", fd=w))
    parsed = marks.parse(line)
    assert parsed["type"] == "setup_result"
    assert parsed["fixture"] == "bench_candidate"
    assert parsed["result"] == "done"


def test_test_started_round_trips_a_nodeid_containing_spaces():
    """write_test_started/parse round-trip a nodeid containing spaces (a parametrize id)."""
    nodeid = "test_one.py::test_x[param with space]"
    [line] = _written_lines(lambda w: marks.write_test_started(nodeid, fd=w))
    parsed = marks.parse(line)
    assert parsed["type"] == "test_started"
    assert parsed["nodeid"] == nodeid


def test_run_result_round_trips_the_result():
    """write_run_result/parse round-trip the final run outcome."""
    [line] = _written_lines(lambda w: marks.write_run_result("error", fd=w))
    parsed = marks.parse(line)
    assert parsed["type"] == "run_result"
    assert parsed["result"] == "error"


@pytest.mark.parametrize(
    "line",
    [
        "hello world",
        "collecting ... ",
        "bench: starting the deployment now",
        "bench: ",
        "bench:",
    ],
)
def test_parse_ignores_lines_that_are_not_a_marker(line):
    """parse ignores plain output and lines that merely start with 'bench:' but do not match."""
    assert marks.parse(line) is None


def test_a_write_call_ignores_a_broken_pipe():
    """Every write_* call ignores EPIPE instead of raising, so a dead reader never crashes it."""
    r, w = os.pipe()
    os.close(r)
    try:
        marks.write_run_result("passed", fd=w)
    finally:
        os.close(w)
