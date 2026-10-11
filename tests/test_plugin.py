"""The plugin's own record: run.json's fields, and the reportlog path it sets."""

import json
from pathlib import Path


def _run_one_passing_test(pytester, monkeypatch):
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    pytester.makepyfile("def test_pass():\n    assert True\n")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
    run_dirs = list(Path("records").iterdir())
    assert len(run_dirs) == 1
    return run_dirs[0]


def test_run_json_has_the_named_fields(pytester, monkeypatch):
    run_dir = _run_one_passing_test(pytester, monkeypatch)
    data = json.loads((run_dir / "run.json").read_text())
    assert set(data) == {"version", "command_line", "start", "end", "exit_status"}
    assert data["version"]
    assert data["command_line"]
    assert data["start"] < data["end"]
    assert data["exit_status"] == 0


def test_reportlog_lands_under_the_results_dir(pytester, monkeypatch):
    run_dir = _run_one_passing_test(pytester, monkeypatch)
    assert (run_dir / "report.jsonl").is_file()
