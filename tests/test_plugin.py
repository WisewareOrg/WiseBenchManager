"""The plugin's own record (run.json's fields, the reportlog path), the bench_candidate
fixture, and -- when BENCH_RECORD_DIR is set -- the collected/setup/test-started markers."""

import json
from pathlib import Path

import pytest

from wisebench import marks
from wisebench.plugin import BENCH_RECORD_DIR, SETUP_ERROR_EXIT_STATUS


def _markers(captured_out):
    """Parse every `bench:` marker line out of captured stdout, in order."""
    return [m for m in (marks.parse(line) for line in captured_out.splitlines()) if m is not None]


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


def test_bench_record_dir_writes_report_and_run_json_directly_there(
    pytester, monkeypatch, tmp_path
):
    """BENCH_RECORD_DIR, when set, is written into directly -- no stamp subdirectory."""
    record_dir = tmp_path / "run" / "target_suite"
    monkeypatch.setenv(BENCH_RECORD_DIR, str(record_dir))
    pytester.makepyfile("def test_pass():\n    assert True\n")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
    assert (record_dir / "run.json").is_file()
    assert (record_dir / "report.jsonl").is_file()
    assert not any(p.is_dir() for p in record_dir.iterdir())


def test_bench_record_dir_unset_writes_no_markers(pytester, monkeypatch, capfd):
    """Without BENCH_RECORD_DIR, today's behaviour holds: no `bench:` marker is printed."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    monkeypatch.delenv(BENCH_RECORD_DIR, raising=False)
    pytester.makepyfile("def test_one():\n    assert True\n")
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=1)
    assert _markers(capfd.readouterr().out) == []


def test_bench_record_dir_set_prints_collected_after_deselection(
    pytester, monkeypatch, capfd, tmp_path
):
    """BENCH_RECORD_DIR set prints `collected <n>` counting the deselected-down item set."""
    monkeypatch.setenv(BENCH_RECORD_DIR, str(tmp_path / "run"))
    pytester.makepyfile("""
        def test_one():
            assert True
        def test_two():
            assert True
        def test_three():
            assert True
    """)
    result = pytester.runpytest("-s", "-k", "one or two")
    result.assert_outcomes(passed=2, deselected=1)
    collected = next(m for m in _markers(capfd.readouterr().out) if m["type"] == "collected")
    assert collected["n"] == 2


def test_bench_record_dir_set_prints_test_started_for_each_test(
    pytester, monkeypatch, capfd, tmp_path
):
    """BENCH_RECORD_DIR set prints `test started <nodeid>` at the start of each test, in order."""
    monkeypatch.setenv(BENCH_RECORD_DIR, str(tmp_path / "run"))
    pytester.makepyfile("""
        def test_one():
            assert True
        def test_two():
            assert True
    """)
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=2)
    nodeids = [m["nodeid"] for m in _markers(capfd.readouterr().out) if m["type"] == "test_started"]
    assert len(nodeids) == 2
    assert nodeids[0].endswith("::test_one")
    assert nodeids[1].endswith("::test_two")


def test_bench_record_dir_set_marks_setup_of_a_fixture_depending_on_bench_candidate(
    pytester, monkeypatch, capfd, tmp_path
):
    """BENCH_RECORD_DIR set writes `setup <fixture> started` then `setup <fixture> done`
    around a fixture whose own argnames include bench_candidate."""
    monkeypatch.setenv(BENCH_RECORD_DIR, str(tmp_path / "run"))
    pytester.makepyfile("""
        import pytest

        @pytest.fixture
        def install_candidate(bench_candidate):
            return bench_candidate

        def test_one(install_candidate):
            assert True
    """)
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=1)
    markers = _markers(capfd.readouterr().out)
    started = [m for m in markers if m["type"] == "setup_started"]
    done = [m for m in markers if m["type"] == "setup_result"]
    assert [m["fixture"] for m in started] == ["install_candidate"]
    assert [m["fixture"] for m in done] == ["install_candidate"]
    assert done[0]["result"] == "done"


def test_bench_record_dir_set_marks_setup_error_for_a_failing_fixture(
    pytester, monkeypatch, capfd, tmp_path
):
    """A fixture depending on bench_candidate that raises during setup writes
    `setup <fixture> error`."""
    monkeypatch.setenv(BENCH_RECORD_DIR, str(tmp_path / "run"))
    pytester.makepyfile("""
        import pytest

        @pytest.fixture
        def install_candidate(bench_candidate):
            raise RuntimeError("board unreachable")

        def test_one(install_candidate):
            assert True
    """)
    result = pytester.runpytest("-s")
    assert result.ret == SETUP_ERROR_EXIT_STATUS
    setup_result = next(m for m in _markers(capfd.readouterr().out) if m["type"] == "setup_result")
    assert setup_result["fixture"] == "install_candidate"
    assert setup_result["result"] == "error"


def test_bench_record_dir_set_does_not_mark_a_fixture_that_does_not_depend_on_bench_candidate(
    pytester, monkeypatch, capfd, tmp_path
):
    """A fixture that does not depend on bench_candidate gets no setup marker."""
    monkeypatch.setenv(BENCH_RECORD_DIR, str(tmp_path / "run"))
    pytester.makepyfile("""
        import pytest

        @pytest.fixture
        def unrelated():
            return 1

        def test_one(unrelated):
            assert True
    """)
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=1)
    markers = _markers(capfd.readouterr().out)
    assert [m for m in markers if m["type"] in ("setup_started", "setup_result")] == []
