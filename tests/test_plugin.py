import json
import re
from pathlib import Path
from typing import Any

import pytest
from conftest import VERSION, results
from fake_board import FakeBoardPlugin, FakeTransport, ok

import wisebench.version
from wisebench.board import BOOT_ID, BUILDINFO, RAUC_STATUS
from wisebench.errors import BenchFailure
from wisebench.plugin import Bench, marks_of
from wisebench.record import Marks

BOARD = {
    "hostname": ok("bench-board\n"),
    BOOT_ID: ok("one\n"),
    RAUC_STATUS: ok('{"compatible": "test", "booted": "A"}'),
    BUILDINFO: ok("DISTRO = test\n"),
}

UTC_ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z")


def run(layer: pytest.Pytester, transport: FakeTransport | None = None) -> pytest.RunResult:
    return layer.runpytest(plugins=[FakeBoardPlugin(transport or FakeTransport(BOARD))])


def record(layer: pytest.Pytester) -> dict[str, Any]:
    (path,) = results(layer).glob("*.json")
    return json.loads(path.read_text())


def entry_of(layer: pytest.Pytester, name: str) -> dict[str, Any]:
    (test,) = [test for test in record(layer)["tests"] if test["nodeid"].endswith(name)]
    return test


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_board_fixture_runs_after_the_hostname_check(layer: pytest.Pytester) -> None:
    transport = FakeTransport({**BOARD, "uname": ok("Linux\n")})
    layer.makepyfile("def test_uname(board):\n    assert board.run('uname').stdout == 'Linux\\n'")
    run(layer, transport).assert_outcomes(passed=1)
    assert transport.commands() == ["hostname", BOOT_ID, "uname", RAUC_STATUS, BUILDINFO]


def test_wrong_hostname_is_an_error_and_nothing_else_is_sent(
    layer: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BENCH_HOSTNAME", "another-board")
    transport = FakeTransport(BOARD)
    layer.makepyfile(
        "def test_one(board):\n    board.run('uname')\n\n"
        "def test_two(board):\n    board.run('uname')\n"
    )
    result = run(layer, transport)
    result.assert_outcomes(errors=2)
    result.stdout.fnmatch_lines(["*answers as hostname 'bench-board'*nothing else was sent*"])
    assert transport.calls == [("reconnect", "default"), ("run", "hostname")]
    written = record(layer)
    assert written["bench.board"] == "bench"
    assert written["bench.booted_slot"] is None
    assert written["bench.board_buildinfo"] is None
    assert "bench.boot_ids" not in written
    assert [test["outcome"] for test in written["tests"]] == ["error", "error"]


def test_a_failing_test_stays_a_failure(layer: pytest.Pytester) -> None:
    layer.makepyfile("def test_fails(board):\n    assert False")
    run(layer).assert_outcomes(failed=1)
    assert entry_of(layer, "test_fails")["outcome"] == "failed"


def test_a_missing_variable_is_an_error_naming_it(
    layer: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BENCH_ADDRESS")
    layer.makepyfile("def test_uses_board(board):\n    pass")
    result = run(layer)
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*BENCH_ADDRESS: Field required*"])


def test_record_carries_session_facts_and_board_state(layer: pytest.Pytester) -> None:
    layer.makepyfile("def test_uses_board(board):\n    pass")
    result = layer.runpytest("-k", "uses_board", plugins=[FakeBoardPlugin(FakeTransport(BOARD))])
    result.assert_outcomes(passed=1)
    written = record(layer)
    assert written["bench.runner_version"] == VERSION
    assert written["bench.board"] == "bench"
    assert written["bench.ota_ran"] is False
    assert written["bench.booted_slot"] == "A"
    assert written["bench.board_buildinfo"] == "DISTRO = test\n"
    assert written["verdict"] is None
    assert written["exit_status"] == 0
    assert UTC_ISO.fullmatch(written["started"]) and UTC_ISO.fullmatch(written["finished"])
    assert written["bench.pytest_args"][:2] == ["-k", "uses_board"]


def test_record_without_a_board_has_no_board_state(layer: pytest.Pytester) -> None:
    layer.makepyfile("def test_host_only():\n    pass")
    transport = FakeTransport({})
    run(layer, transport).assert_outcomes(passed=1)
    written = record(layer)
    assert written["bench.ota_ran"] is False
    assert written["bench.booted_slot"] is None
    assert written["bench.board_buildinfo"] is None
    assert "bench.board" not in written
    assert transport.calls == []


def test_every_test_entry_has_its_outcome_properties_and_item(layer: pytest.Pytester) -> None:
    layer.makepyfile(
        """
        import pytest

        @pytest.mark.tst("TST011")
        def test_item(record_property):
            record_property("layer.page_state", {"cards": 3})
            record_property("bench.anything", 1)

        def test_plain():
            pass

        @pytest.mark.skip
        def test_skipped():
            pass
        """
    )
    run(layer).assert_outcomes(passed=2, skipped=1)
    item = entry_of(layer, "test_item")
    assert item["outcome"] == "passed"
    assert item["properties"] == {"layer.page_state": {"cards": 3}, "bench.anything": 1}
    assert item["item"] == "TST011"
    plain = entry_of(layer, "test_plain")
    assert (plain["item"], plain["status"], plain["reason"]) == (None, None, None)
    assert entry_of(layer, "test_skipped")["outcome"] == "skipped"


def test_without_ota_every_item_is_not_credited(layer: pytest.Pytester) -> None:
    layer.makepyfile(
        """
        import pytest

        @pytest.mark.provenance
        def test_provenance(board):
            pass

        @pytest.mark.tst("TST011")
        def test_passes(board):
            pass

        @pytest.mark.tst("TST014", recorded=True)
        def test_records(board):
            pass
        """
    )
    run(layer).assert_outcomes(passed=3)
    statuses = {
        test["item"]: (test["status"], test["reason"])
        for test in record(layer)["tests"]
        if test["item"]
    }
    assert statuses == {
        "TST011": ("not credited", "ota did not run"),
        "TST014": ("not credited", "ota did not run"),
    }


def test_index_line_and_event_stream(layer: pytest.Pytester) -> None:
    layer.makepyfile("def test_a():\n    pass\n\ndef test_b():\n    assert False")
    run(layer).assert_outcomes(passed=1, failed=1)
    (written,) = results(layer).glob("*.json")
    (index,) = lines(results(layer) / "index.jsonl")
    assert index == {
        "timestamp": record(layer)["started"],
        "file": written.name,
        "runner_version": VERSION,
        "verdict": None,
        "counts": {"passed": 1, "failed": 1},
    }
    events = lines(results(layer) / "events.jsonl")
    assert [(event["kind"], event.get("nodeid", "").split("::")[-1]) for event in events] == [
        ("session_start", ""),
        ("test_start", "test_a"),
        ("test_end", "test_a"),
        ("test_start", "test_b"),
        ("test_end", "test_b"),
        ("record_written", ""),
    ]
    assert all(UTC_ISO.fullmatch(event["ts"]) for event in events)
    assert events[0]["runner_version"] == VERSION
    assert [event["outcome"] for event in events if event["kind"] == "test_end"] == [
        "passed",
        "failed",
    ]
    assert events[-1]["file"] == written.name


def test_second_session_appends_to_index_and_events(layer: pytest.Pytester) -> None:
    layer.makepyfile("def test_a():\n    pass")
    run(layer)
    run(layer)
    assert len(lines(results(layer) / "index.jsonl")) == 2
    assert len(list(results(layer).glob("*.json"))) == 2
    kinds = [event["kind"] for event in lines(results(layer) / "events.jsonl")]
    assert kinds.count("session_start") == 2


def test_record_is_written_when_collection_errors(layer: pytest.Pytester) -> None:
    layer.makepyfile("import nonexistent_module\n\ndef test_a():\n    pass")
    result = run(layer)
    assert result.ret == pytest.ExitCode.INTERRUPTED
    written = record(layer)
    assert written["exit_status"] == pytest.ExitCode.INTERRUPTED
    assert written["tests"] == []


def test_result_dir_defaults_to_records_under_the_start_directory(
    layer: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BENCH_RESULT_DIR")
    layer.makepyfile("def test_a():\n    pass")
    run(layer).assert_outcomes(passed=1)
    assert len(list((layer.path / "records").glob("*.json"))) == 1
    assert (layer.path / "records" / "index.jsonl").exists()


def test_fixture_error_is_an_error_outcome(layer: pytest.Pytester) -> None:
    layer.makepyfile(
        """
        import pytest

        @pytest.fixture
        def broken():
            raise RuntimeError("fixture broke")

        @pytest.mark.tst("TST011")
        def test_a(broken):
            pass
        """
    )
    run(layer).assert_outcomes(errors=1)
    assert entry_of(layer, "test_a")["outcome"] == "error"


@pytest.mark.parametrize(
    ("marker", "message"),
    [
        ('tst("TST11")', "tst id 'TST11' is not three capitals and three digits"),
        ('tst("tst011")', "tst id 'tst011' is not three capitals and three digits"),
        ('tst("TST0111")', "tst id 'TST0111' is not three capitals and three digits"),
        ("tst(11)", "tst id 11 is not three capitals and three digits"),
        ("tst", "tst takes exactly one item id"),
        ('tst("TST011", "TST012")', "tst takes exactly one item id"),
        ('tst("TST011", credited=True)', "tst has unknown keyword credited"),
        ('tst("TST011", recorded="yes")', "tst recorded= must be True or False"),
    ],
)
def test_malformed_tst_marker_is_a_collection_error(
    layer: pytest.Pytester, marker: str, message: str
) -> None:
    layer.makepyfile(f"import pytest\n\n@pytest.mark.{marker}\ndef test_a():\n    pass")
    result = run(layer)
    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.stdout.fnmatch_lines(["*ERROR collecting*", f"*{message}*"])


def test_two_tst_markers_are_a_collection_error(layer: pytest.Pytester) -> None:
    layer.makepyfile(
        'import pytest\n\npytestmark = pytest.mark.tst("TST001")\n\n'
        '@pytest.mark.tst("TST011")\ndef test_a():\n    pass'
    )
    result = run(layer)
    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.stdout.fnmatch_lines(["*more than one tst marker*"])


def test_well_formed_markers_collect_on_classes_and_parametrized_tests(
    layer: pytest.Pytester,
) -> None:
    layer.makepyfile(
        """
        import pytest

        @pytest.mark.tst("TST014", recorded=True)
        class TestTiming:
            @pytest.mark.parametrize("n", [1, 2])
            def test_a(self, n):
                pass

        @pytest.mark.tst("SRS010", recorded=False)
        @pytest.mark.provenance
        def test_b():
            pass
        """
    )
    run(layer).assert_outcomes(passed=3)
    assert {test["item"] for test in record(layer)["tests"]} == {"TST014", "SRS010"}


def test_markers_are_registered(layer: pytest.Pytester) -> None:
    result = layer.runpytest("--markers")
    result.stdout.fnmatch_lines(["@pytest.mark.tst(item_id, recorded=False):*", "*provenance:*"])


def test_dirty_runner_clone_is_refused_before_any_test(
    layer: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    def dirty(clone: Path) -> str:
        raise BenchFailure("the runner's clone has uncommitted changes (v1-dirty); refusing to run")

    monkeypatch.setattr(wisebench.version, "describe", dirty)
    layer.makepyfile("def test_a():\n    pass")
    result = run(layer)
    assert result.ret == pytest.ExitCode.INTERNAL_ERROR
    assert "uncommitted changes" in result.stdout.str() + result.stderr.str()
    assert not results(layer).exists()


def test_fixture_error_in_teardown_is_an_error_outcome(layer: pytest.Pytester) -> None:
    layer.makepyfile(
        """
        import pytest

        @pytest.fixture
        def breaks_after():
            yield
            raise RuntimeError("teardown broke")

        def test_a(breaks_after):
            pass
        """
    )
    run(layer).assert_outcomes(passed=1, errors=1)
    assert entry_of(layer, "test_a")["outcome"] == "error"


STUB_OTA = """

from wisebench.plugin import Bench


@pytest.fixture(scope="session")
def ota(request, board):
    Bench.of(request.config).record.set("bench.ota_ran", True)
"""


def with_stub_ota(layer: pytest.Pytester) -> None:
    """Add an `ota` fixture that only records that an OTA ran."""
    layer.makeconftest(layer.path.joinpath("conftest.py").read_text() + STUB_OTA)


def test_when_ota_ran_the_board_state_is_not_read_at_session_end(layer: pytest.Pytester) -> None:
    with_stub_ota(layer)
    transport = FakeTransport(BOARD)
    layer.makepyfile("def test_a(ota):\n    pass")
    run(layer, transport).assert_outcomes(passed=1)
    assert transport.commands() == ["hostname", BOOT_ID]
    written = record(layer)
    assert written["bench.ota_ran"] is True
    assert written["bench.booted_slot"] is None


def test_marks_of_reads_item_and_roles(layer: pytest.Pytester) -> None:
    marked, plain = layer.getitems(
        "import pytest\n\n@pytest.mark.tst('TST014', recorded=True)\n"
        "@pytest.mark.provenance\ndef test_marked():\n    pass\n\n"
        "def test_plain():\n    pass"
    )
    assert marks_of(marked) == Marks(item_id="TST014", recorded=True, provenance=True)
    assert marks_of(plain) == Marks()


def test_session_tracks_which_tests_had_a_fixture_raise(layer: pytest.Pytester) -> None:
    layer.makepyfile(
        """
        import pytest

        @pytest.fixture
        def broken():
            raise RuntimeError("setup broke")

        @pytest.fixture
        def breaks_after():
            yield
            raise RuntimeError("teardown broke")

        def test_setup(broken):
            pass

        def test_teardown(breaks_after):
            pass

        def test_fails():
            assert False
        """
    )
    recorder = layer.inline_run(plugins=[FakeBoardPlugin(FakeTransport(BOARD))])
    config = recorder.getcall("pytest_sessionfinish").session.config
    raised = {
        nodeid.split("::")[-1]: test.bench_failed
        for nodeid, test in Bench.of(config).record.tests.items()
    }
    assert raised == {"test_setup": True, "test_teardown": True, "test_fails": False}


def test_xfail_outcomes_are_named(layer: pytest.Pytester) -> None:
    layer.makepyfile(
        """
        import pytest

        @pytest.mark.xfail
        def test_expected():
            assert False

        @pytest.mark.xfail
        def test_unexpected():
            pass
        """
    )
    run(layer).assert_outcomes(xfailed=1, xpassed=1)
    assert entry_of(layer, "test_expected")["outcome"] == "xfailed"
    assert entry_of(layer, "test_unexpected")["outcome"] == "xpassed"


def test_bench_tree_names_the_checkout_holding_bench_toml(
    layer: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    start = layer.path / "bench.toml"
    start.rename(tree / "bench.toml")
    wrong = (tree / "bench.toml").read_text().replace("schema = 1", "schema = 2")
    start.write_text(wrong)
    monkeypatch.setenv("BENCH_TREE", str(tree))
    layer.makepyfile("def test_uses_board(board):\n    pass")
    run(layer).assert_outcomes(passed=1)


def test_without_bench_tree_a_start_directory_without_bench_toml_is_an_error(
    layer: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BENCH_TREE", raising=False)
    (layer.path / "bench.toml").unlink()
    layer.makepyfile("def test_uses_board(board):\n    pass")
    result = run(layer)
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*bench.toml not found*"])


def test_with_ota_and_provenance_passed_items_are_credited(layer: pytest.Pytester) -> None:
    with_stub_ota(layer)
    layer.makepyfile(
        """
        import pytest

        @pytest.mark.provenance
        def test_provenance(ota):
            pass

        @pytest.mark.tst("TST011")
        def test_passes(ota):
            pass

        @pytest.mark.tst("TST014", recorded=True)
        def test_records(ota):
            pass
        """
    )
    run(layer).assert_outcomes(passed=3)
    statuses = {
        test["item"]: (test["status"], test["reason"])
        for test in record(layer)["tests"]
        if test["item"]
    }
    assert statuses == {"TST011": ("passed", None), "TST014": ("recorded", None)}


def test_a_bench_failure_in_a_test_body_is_the_bench_s_and_credits_nothing(
    layer: pytest.Pytester,
) -> None:
    with_stub_ota(layer)
    layer.makepyfile(
        """
        import pytest
        from wisebench.errors import BenchFailure

        @pytest.mark.provenance
        def test_provenance(ota):
            pass

        @pytest.mark.tst("TST011")
        def test_passes(ota):
            pass

        def test_bench_breaks(ota):
            raise BenchFailure("the board went away")
        """
    )
    run(layer).assert_outcomes(passed=2, failed=1)
    assert entry_of(layer, "test_bench_breaks")["outcome"] == "error"
    passes = entry_of(layer, "test_passes")
    assert (passes["status"], passes["reason"]) == ("not credited", "the bench failed")


def test_an_ordinary_failure_in_a_test_body_stays_the_candidate_s(layer: pytest.Pytester) -> None:
    with_stub_ota(layer)
    layer.makepyfile(
        """
        import pytest

        @pytest.mark.provenance
        def test_provenance(ota):
            pass

        @pytest.mark.tst("TST011")
        def test_passes(ota):
            pass

        def test_candidate_fails(ota):
            raise OSError("the candidate broke")
        """
    )
    run(layer).assert_outcomes(passed=2, failed=1)
    assert entry_of(layer, "test_candidate_fails")["outcome"] == "failed"
    assert entry_of(layer, "test_passes")["status"] == "passed"


def test_the_record_lists_each_item_once_with_one_status(layer: pytest.Pytester) -> None:
    with_stub_ota(layer)
    layer.makepyfile(
        """
        import pytest

        @pytest.mark.provenance
        def test_provenance(ota):
            pass

        @pytest.mark.tst("TST011")
        @pytest.mark.parametrize("case", [1, 2])
        def test_item(ota, case):
            assert case == 1

        @pytest.mark.tst("TST014", recorded=True)
        def test_records(ota):
            pass
        """
    )
    run(layer).assert_outcomes(passed=3, failed=1)
    assert record(layer)["items"] == {
        "TST011": {"status": "not credited", "reason": "test failed"},
        "TST014": {"status": "recorded", "reason": None},
    }
