"""`bench_state.stop_timer` and `enable`: the systemctl calls and the failure texts."""

import subprocess

import pytest
from helpers import start_argv, stop_argv

from wisebench import bench_state

FAILURES = [
    pytest.param(
        dict(returncode=1, stderr="Unit bench-run.timer not found.\n"),
        "systemctl failed: Unit bench-run.timer not found.",
        id="stderr",
    ),
    pytest.param(dict(returncode=5, stderr=""), "systemctl failed: exit 5", id="no stderr"),
    pytest.param(
        dict(raises=OSError("no systemctl")), "systemctl failed: no systemctl", id="oserror"
    ),
    pytest.param(
        dict(raises=subprocess.TimeoutExpired("systemctl", 1)),
        f"systemctl failed: {subprocess.TimeoutExpired('systemctl', 1)}",
        id="timeout",
    ),
]


def test_stop_timer_stops_the_timer_unit_with_the_query_timeout_and_returns_none(systemctl):
    assert bench_state.stop_timer("bench-run") is None

    ((argv, kwargs),) = systemctl.invocations
    assert argv == stop_argv("bench-run")
    assert kwargs["timeout"] == 1


@pytest.mark.parametrize(("answer", "text"), FAILURES)
def test_stop_timer_returns_the_failure_text_and_does_not_raise(systemctl, answer, text):
    systemctl.answer("stop", **answer)

    assert bench_state.stop_timer("bench-run") == text


def test_enable_removes_disabled_then_starts_the_timer_unit(systemctl, tmp_path):
    (tmp_path / "DISABLED").write_text("2026-11-13T00:00:00+00:00\nboom\n")

    assert bench_state.enable(tmp_path, "bench-run") is None

    assert not (tmp_path / "DISABLED").exists()
    ((argv, kwargs),) = systemctl.invocations
    assert argv == start_argv("bench-run")
    assert kwargs["timeout"] == 1


def test_enable_without_a_disabled_file_still_starts_the_timer(systemctl, tmp_path):
    assert bench_state.enable(tmp_path, "bench-run") is None

    assert systemctl.calls == [start_argv("bench-run")]


@pytest.mark.parametrize(("answer", "text"), FAILURES)
def test_enable_returns_the_start_failure_text_and_does_not_raise(
    systemctl, tmp_path, answer, text
):
    systemctl.answer("start", **answer)

    assert bench_state.enable(tmp_path, "bench-run") == text


def test_enable_that_cannot_remove_disabled_says_so_and_does_not_start_the_timer(
    systemctl, tmp_path
):
    (tmp_path / "DISABLED").mkdir()
    with pytest.raises(OSError) as refused:
        (tmp_path / "DISABLED").unlink()

    assert bench_state.enable(tmp_path, "bench-run") == f"cannot remove DISABLED: {refused.value}"

    assert systemctl.calls == []
