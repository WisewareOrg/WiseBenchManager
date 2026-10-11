import json
from pathlib import Path
from typing import Any

import pytest
from conftest import VERSION, ScriptedSsh, ok

import wisebench.plugin
from wisebench.board import BOOT_ID, BUILDINFO, RAUC_STATUS
from wisebench.plugin import Bench

OTA = """
import pytest
from wisebench.plugin import Bench


@pytest.fixture(scope="session")
def ota(request, board):
    Bench.of(request.config).record.set("bench.ota_ran", True)
"""


def results(layer: pytest.Pytester) -> Path:
    return layer.path / "results"


def summary(layer: pytest.Pytester) -> dict[str, Any]:
    (path,) = results(layer).glob("*Z.json")
    return json.loads(path.read_text())


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def outcomes(result: pytest.RunResult) -> dict[str, str | None]:
    """The outcome the plugin folded for each test of an in-process run, by test name."""
    config = result.reprec.getcall("pytest_sessionfinish").session.config
    return {n.split("::")[-1]: t.outcome for n, t in Bench.of(config).record.tests.items()}


def test_the_board_is_checked_used_read_and_closed(
    layer: pytest.Pytester, ssh: ScriptedSsh
) -> None:
    ssh.script["uname"] = ok("Linux")
    layer.makepyfile("def test_uname(board):\n    assert board.run('uname')[0] == ['Linux']")
    layer.runpytest("-k", "uname").assert_outcomes(passed=1)
    assert ssh.commands() == ["hostname", BOOT_ID, "uname", RAUC_STATUS, BUILDINFO]
    assert ssh.calls[-1] == ("deactivate",)
    written = summary(layer)
    assert (written["bench.runner_version"], written["bench.board"]) == (VERSION, "bench")
    assert written["bench.boot_ids"] == ["one"]
    assert (written["bench.booted_slot"], written["bench.board_buildinfo"]) == (
        "A",
        "DISTRO = test",
    )
    assert (written["bench.ota_ran"], written["verdict"]) == (False, None)
    assert written["bench.pytest_args"][:2] == ["-k", "uname"]
    assert (results(layer) / written["report"]).is_file()


def test_a_wrong_board_is_an_error_and_nothing_else_is_sent(
    layer: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, ssh: ScriptedSsh
) -> None:
    monkeypatch.setenv("BENCH_HOSTNAME", "another-board")
    layer.makepyfile("def test_one(board):\n    pass\n\ndef test_two(board):\n    pass")
    result = layer.runpytest()
    result.assert_outcomes(errors=2)
    result.stdout.fnmatch_lines(["*answers as hostname 'bench-board'*"])
    assert ssh.commands() == ["hostname"]
    written = summary(layer)
    assert "bench.boot_ids" not in written and written["bench.booted_slot"] is None


def test_a_run_without_the_board_touches_nothing(layer: pytest.Pytester, ssh: ScriptedSsh) -> None:
    layer.makepyfile("def test_host_only():\n    pass")
    layer.runpytest().assert_outcomes(passed=1)
    assert ssh.calls == []
    assert "bench.board" not in summary(layer)


SUITE = """
import pytest
from wisebench.errors import BenchFailure

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


@pytest.mark.parametrize(
    ("ota", "extra", "items"),
    [
        (False, "", {"TST011": "not credited", "TST014": "not credited"}),
        (True, "", {"TST011": "passed", "TST014": "recorded"}),
        (True, "def test_breaks(board):\n    raise OSError", {"TST011": "passed"}),
        (
            True,
            "def test_breaks(board):\n    raise BenchFailure('gone')",
            {"TST011": "not credited"},
        ),
    ],
)
def test_items_are_credited_by_the_rule(
    layer: pytest.Pytester, ssh: ScriptedSsh, ota: bool, extra: str, items: dict[str, str]
) -> None:
    if ota:
        layer.makeconftest(OTA)
    layer.makepyfile(SUITE.replace("(board)", "(ota)" if ota else "(board)") + "\n" + extra)
    result = layer.runpytest()
    statuses = {item: entry["status"] for item, entry in summary(layer)["items"].items()}
    assert (RAUC_STATUS in ssh.commands()) is not ota
    assert {item: statuses[item] for item in items} == items
    if "BenchFailure" in extra:
        assert outcomes(result)["test_breaks"] == "error"
        assert summary(layer)["items"]["TST011"]["reason"] == "the bench failed"


def test_fixture_errors_and_xfails_are_folded_by_name(layer: pytest.Pytester) -> None:
    layer.makepyfile(
        """
        import pytest

        @pytest.fixture
        def breaks_after():
            yield
            raise RuntimeError

        def test_teardown(breaks_after):
            pass

        @pytest.mark.xfail
        def test_expected():
            assert False

        @pytest.mark.xfail
        def test_unexpected():
            pass

        @pytest.mark.skip
        def test_skipped():
            pass
        """
    )
    folded = outcomes(layer.runpytest())
    assert folded == {
        "test_teardown": "error",
        "test_expected": "xfailed",
        "test_unexpected": "xpassed",
        "test_skipped": "skipped",
    }


@pytest.mark.parametrize(
    ("marker", "message"),
    [
        ('tst("T011")', "tst id 'T011' is not two or more capitals"),
        ('tst("TST11")', "tst id 'TST11' is not two or more capitals"),
        ('tst("tst011")', "tst id 'tst011' is not two or more capitals"),
        ('tst("TST011a")', "tst id 'TST011a' is not two or more capitals"),
        ("tst(11)", "tst id 11 is not two or more capitals"),
        ("tst", "tst takes exactly one item id"),
        ('tst("TST011", "TST012")', "tst takes exactly one item id"),
        ('tst("TST011", credited=True)', "tst has unknown keyword credited"),
        ('tst("TST011", recorded="yes")', "tst recorded= must be True or False"),
    ],
)
def test_a_malformed_tst_marker_is_a_collection_error(
    layer: pytest.Pytester, marker: str, message: str
) -> None:
    layer.makepyfile(f"import pytest\n\n@pytest.mark.{marker}\ndef test_a():\n    pass")
    result = layer.runpytest()
    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.stdout.fnmatch_lines([f"*{message}*"])
    assert summary(layer)["items"] == {}


def test_tst_takes_one_marker_per_test_and_ids_of_any_length(layer: pytest.Pytester) -> None:
    layer.makepyfile(
        """
        import pytest

        @pytest.mark.tst("SR001")
        def test_short():
            pass

        @pytest.mark.tst("SRSA1010")
        def test_long():
            pass
        """
    )
    layer.runpytest().assert_outcomes(passed=2)
    assert set(summary(layer)["items"]) == {"SR001", "SRSA1010"}
    layer.makepyfile(
        'import pytest\n\npytestmark = pytest.mark.tst("TST001")\n\n'
        '@pytest.mark.tst("TST011")\ndef test_a():\n    pass'
    )
    layer.runpytest().stdout.fnmatch_lines(["*more than one tst marker*"])


def test_events_and_the_index_line(layer: pytest.Pytester) -> None:
    layer.makepyfile("def test_a():\n    pass\n\ndef test_b():\n    assert False")
    layer.runpytest().assert_outcomes(passed=1, failed=1)
    events = lines(results(layer) / "events.jsonl")
    kinds = [(e["kind"], e.get("nodeid", "").split("::")[-1], e.get("outcome")) for e in events]
    assert kinds == [
        ("session_start", "", None),
        ("test_start", "test_a", None),
        ("test_end", "test_a", "passed"),
        ("test_start", "test_b", None),
        ("test_end", "test_b", "failed"),
        ("record_written", "", None),
    ]
    assert all(e["ts"].endswith("Z") for e in events)
    (index,) = lines(results(layer) / "index.jsonl")
    assert (index["runner_version"], index["counts"]) == (VERSION, {"passed": 1, "failed": 1})


def test_the_report_log_path_given_on_the_command_line_is_kept(layer: pytest.Pytester) -> None:
    layer.makepyfile("def test_a():\n    pass")
    layer.runpytest(f"--report-log={layer.path / 'mine.jsonl'}").assert_outcomes(passed=1)
    assert (layer.path / "mine.jsonl").is_file()
    assert summary(layer)["report"] == "../mine.jsonl"


def test_bench_tree_wins_and_records_default_to_the_start_directory(
    layer: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (layer.path / "bench.toml").rename(tree / "bench.toml")
    (layer.path / "bench.toml").write_text("schema = 2")
    monkeypatch.setenv("BENCH_TREE", str(tree))
    monkeypatch.delenv("BENCH_RESULT_DIR")
    layer.makepyfile("def test_uses_board(board):\n    pass")
    layer.runpytest().assert_outcomes(passed=1)
    assert len(list((layer.path / "records").glob("*Z.json"))) == 1


@pytest.mark.parametrize(
    ("seam", "value", "message"),
    [
        ("clone_changes", " M x.py\n", "uncommitted changes"),
        ("checkout_version", "0.0.0", "run uv sync"),
    ],
)
def test_a_dirty_or_stale_runner_is_refused(
    layer: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, seam: str, value: str, message: str
) -> None:
    monkeypatch.setattr(wisebench.plugin, seam, lambda *args: value)
    layer.makepyfile("def test_a():\n    pass")
    result = layer.runpytest()
    assert result.ret == pytest.ExitCode.INTERNAL_ERROR
    assert message in result.stdout.str() + result.stderr.str()
