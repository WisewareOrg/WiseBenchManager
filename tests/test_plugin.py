"""The plugin's own record: run.json's fields, and the reportlog path it sets."""

import json
from pathlib import Path

import pytest

from wisebench.plugin import SETUP_ERROR_EXIT_STATUS


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
    """run.json has exactly the always-present fields, with the right exit_status."""
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
    """report.jsonl lands under the results directory."""
    run_dir = _run_one_passing_test(pytester, monkeypatch)
    assert (run_dir / "report.jsonl").is_file()


def test_run_json_carries_the_bench_fields_that_are_set(pytester, monkeypatch):
    """run.json carries test_commit and set from their env vars, and omits unset fields."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    monkeypatch.setenv("BENCH_SHA", "deadbeef")
    monkeypatch.setenv("BENCH_SET", "scratch-set")
    run_dir = _run_one_passing_test(pytester, monkeypatch)
    data = json.loads((run_dir / "run.json").read_text())
    assert data["test_commit"] == "deadbeef"
    assert "tree" not in data
    assert data["set"] == "scratch-set"
    assert "skip_build" not in data


def test_run_json_carries_skip_build_when_set(pytester, monkeypatch):
    """run.json carries skip_build when BENCH_SKIP_BUILD is set."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    monkeypatch.setenv("BENCH_SKIP_BUILD", "true")
    run_dir = _run_one_passing_test(pytester, monkeypatch)
    data = json.loads((run_dir / "run.json").read_text())
    assert data["skip_build"] == "true"


def test_a_fixture_setup_error_substitutes_the_exit_status(pytester, monkeypatch):
    """A fixture erroring during setup substitutes SETUP_ERROR_EXIT_STATUS for pytest's own."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    pytester.makepyfile("""
        import pytest

        @pytest.fixture
        def broken():
            raise RuntimeError("board unreachable")

        def test_one(broken):
            pass
    """)
    result = pytester.runpytest()
    assert result.ret == SETUP_ERROR_EXIT_STATUS
    run_dirs = list(Path("records").iterdir())
    data = json.loads((run_dirs[0] / "run.json").read_text())
    assert data["exit_status"] == SETUP_ERROR_EXIT_STATUS


def test_bench_candidate_defaults_to_none(pytester, monkeypatch):
    """bench_candidate is None without --bench-candidate."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    pytester.makepyfile("""
        def test_candidate_is_none(bench_candidate):
            assert bench_candidate is None
    """)
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_bench_candidate_reads_the_command_line_option(pytester, monkeypatch):
    """bench_candidate reads --bench-candidate's value."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    pytester.makepyfile("""
        def test_candidate_is_the_built_commit(bench_candidate):
            assert bench_candidate == "deadbeef"
    """)
    result = pytester.runpytest("--bench-candidate", "deadbeef")
    result.assert_outcomes(passed=1)
