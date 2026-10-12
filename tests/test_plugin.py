"""The plugin's own record: run.json's fields, and the reportlog path it sets."""

import json
from pathlib import Path

import pytest


def _run_one_passing_test(pytester, monkeypatch):
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    pytester.makepyfile("def test_pass():\n    assert True\n")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
    run_dirs = list(Path("records").iterdir())
    assert len(run_dirs) == 1
    return run_dirs[0]


@pytest.mark.parametrize(
    ("body", "expected_exit_status"),
    [
        ("def test_pass():\n    assert True\n", 0),
        ("def test_fail():\n    assert False\n", 1),
    ],
)
def test_run_json_has_the_named_fields(pytester, monkeypatch, body, expected_exit_status):
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    pytester.makepyfile(body)
    result = pytester.runpytest()
    assert result.ret == expected_exit_status
    run_dirs = list(Path("records").iterdir())
    assert len(run_dirs) == 1
    data = json.loads((run_dirs[0] / "run.json").read_text())
    assert set(data) == {"version", "command_line", "start", "end", "exit_status"}
    assert data["version"]
    assert data["command_line"]
    assert data["start"] < data["end"]
    assert data["exit_status"] == expected_exit_status


def test_reportlog_lands_under_the_results_dir(pytester, monkeypatch):
    run_dir = _run_one_passing_test(pytester, monkeypatch)
    assert (run_dir / "report.jsonl").is_file()


def test_run_json_carries_whichever_bench_fields_are_set(pytester, monkeypatch):
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    monkeypatch.setenv("BENCH_SHA", "deadbeef")
    monkeypatch.setenv("BENCH_TREE", "/checkout")
    monkeypatch.setenv("BENCH_SET", "scratch-set")
    monkeypatch.delenv("BENCH_BUNDLE_SHA256", raising=False)
    monkeypatch.delenv("BENCH_SLOT_BEFORE", raising=False)
    monkeypatch.delenv("BENCH_SLOT_AFTER", raising=False)
    run_dir = _run_one_passing_test(pytester, monkeypatch)
    data = json.loads((run_dir / "run.json").read_text())
    assert data["commit"] == "deadbeef"
    assert data["tree"] == "/checkout"
    assert data["set"] == "scratch-set"
    assert "bundle_sha256" not in data
    assert "slot_before" not in data
    assert "slot_after" not in data


def test_run_json_carries_the_bundle_and_slot_fields_when_set(pytester, monkeypatch):
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    monkeypatch.setenv("BENCH_BUNDLE_SHA256", "sha256value")
    monkeypatch.setenv("BENCH_SLOT_BEFORE", "rootfs.1")
    monkeypatch.setenv("BENCH_SLOT_AFTER", "rootfs.0")
    run_dir = _run_one_passing_test(pytester, monkeypatch)
    data = json.loads((run_dir / "run.json").read_text())
    assert data["bundle_sha256"] == "sha256value"
    assert data["slot_before"] == "rootfs.1"
    assert data["slot_after"] == "rootfs.0"
