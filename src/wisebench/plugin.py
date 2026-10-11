"""The pytest plugin: the board fixtures, the `tst` and `provenance` markers, the record."""

from __future__ import annotations

import importlib.metadata
import os
import re
import subprocess
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any

import pytest
import setuptools_scm

from wisebench.board import Board, driver_for
from wisebench.config import Config
from wisebench.errors import BenchFailure
from wisebench.events import EventLog
from wisebench.record import Marks, SessionRecord

CLONE = Path(__file__).resolve().parent
REPOSITORY = CLONE.parents[1]


def clone_changes(clone: Path) -> str:
    """Tracked changes in `clone`, as `git status --porcelain` lists them; no clone raises."""
    status = subprocess.run(
        ["git", "-C", str(clone), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
    )
    if status.returncode:
        raise BenchFailure(f"the runner is not run from its git clone: {status.stderr.strip()}")
    return status.stdout


def checkout_version() -> str:
    """The version setuptools-scm, hatch-vcs's engine, derives from the clone's checkout now."""
    return setuptools_scm.get_version(root=str(REPOSITORY))


TST_ID = re.compile(r"[A-Z]{2,}[0-9]{3,}")
TST_KEYWORDS = {"recorded"}


class Bench:
    """One session's record and event stream; its methods are the hooks that fill them."""

    KEY = pytest.StashKey["Bench"]()

    def __init__(self, result_dir: Path, runner_version: str) -> None:
        self.record = SessionRecord(result_dir)
        self.events = EventLog(result_dir)
        self.record.set("bench.runner_version", runner_version)

    @classmethod
    def of(cls, config: pytest.Config) -> Bench:
        return config.stash[cls.KEY]

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        self.record.set("bench.pytest_args", list(session.config.invocation_params.args))
        self.events.emit(
            "session_start", runner_version=self.record.properties["bench.runner_version"]
        )

    def pytest_runtest_logstart(self, nodeid: str) -> None:
        self.events.emit("test_start", nodeid=nodeid)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(
        self, item: pytest.Item, call: pytest.CallInfo[None]
    ) -> Generator[None, Any, Any]:
        report = yield
        bench_failure = call.excinfo is not None and call.excinfo.errisinstance(BenchFailure)
        self.record.add(item.nodeid, marks_of(item), report, bench_failure)
        return report

    def pytest_runtest_logfinish(self, nodeid: str) -> None:
        test = self.record.tests.get(nodeid)
        self.events.emit("test_end", nodeid=nodeid, outcome=test.outcome if test else None)

    def pytest_sessionfinish(self, exitstatus: int) -> None:
        path = self.record.write(int(exitstatus))
        self.events.emit("record_written", file=path.name)


def marks_of(item: pytest.Item) -> Marks:
    """Read and check an item's `tst` and `provenance` markers; a malformed `tst` raises.

    No maintained tool fits because pytest-requirements neither validates an item id nor
    carries `recorded=` nor computes credit, and `--strict-markers` checks names only.
    """
    tst = list(item.iter_markers("tst"))
    provenance = item.get_closest_marker("provenance") is not None
    if not tst:
        return Marks(provenance=provenance)
    if len(tst) > 1:
        raise pytest.Collector.CollectError(f"{item.nodeid}: more than one tst marker")
    mark = tst[0]
    if len(mark.args) != 1:
        raise pytest.Collector.CollectError(f"{item.nodeid}: tst takes exactly one item id")
    item_id = mark.args[0]
    if not isinstance(item_id, str) or not TST_ID.fullmatch(item_id):
        raise pytest.Collector.CollectError(
            f"{item.nodeid}: tst id {item_id!r} is not two or more capitals, "
            "then three or more digits"
        )
    unknown = set(mark.kwargs) - TST_KEYWORDS
    if unknown:
        raise pytest.Collector.CollectError(
            f"{item.nodeid}: tst has unknown keyword {', '.join(sorted(unknown))}"
        )
    recorded = mark.kwargs.get("recorded", False)
    if not isinstance(recorded, bool):
        raise pytest.Collector.CollectError(f"{item.nodeid}: tst recorded= must be True or False")
    return Marks(item_id=item_id, recorded=recorded, provenance=provenance)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "tst(item_id, recorded=False): the requirement item this test discharges"
    )
    config.addinivalue_line(
        "markers", "provenance: the test that proves the board runs the commit under test"
    )
    result_dir = Path(
        os.environ.get("BENCH_RESULT_DIR") or config.invocation_params.dir / "records"
    )
    if clone_changes(CLONE):
        raise BenchFailure("the runner's clone has uncommitted changes; refusing to run")
    installed = importlib.metadata.version("wisebench")
    if installed != checkout_version():
        raise BenchFailure(
            f"the installed runner is {installed}, not its checkout's {checkout_version()}; "
            "run uv sync"
        )
    bench = Bench(result_dir, installed)
    config.stash[Bench.KEY] = bench
    result_dir.mkdir(parents=True, exist_ok=True)
    if config.option.report_log:
        bench.record.report = Path(config.option.report_log)
    else:
        config.option.report_log = str(bench.record.report)
    config.pluginmanager.register(bench, "wisebench-session")


@pytest.hookimpl(wrapper=True)
def pytest_pycollect_makeitem(
    collector: pytest.Collector, name: str, obj: object
) -> Generator[None, Any, Any]:
    result = yield
    for item in result if isinstance(result, list) else [result]:
        if isinstance(item, pytest.Item):
            marks_of(item)
    return result


@pytest.fixture(scope="session")
def bench_config(request: pytest.FixtureRequest) -> Config:
    """The layer's `bench.toml`, read from `BENCH_TREE`, else the directory pytest started in."""
    tree = os.environ.get("BENCH_TREE")
    return Config.load(Path(tree) if tree else request.config.invocation_params.dir)


@pytest.fixture(scope="session")
def board(request: pytest.FixtureRequest, bench_config: Config) -> Iterator[Board]:
    """The configured board, its hostname checked before any other command.

    At session end, when no OTA ran, the booted slot and `/etc/buildinfo` go to the record.
    """
    record = Bench.of(request.config).record
    target, ssh = driver_for(bench_config.identity.address)
    connected = Board(target, ssh, bench_config, record.set)
    try:
        yield connected
        if record.properties.get("bench.ota_ran") is not True:
            record.set("bench.booted_slot", connected.booted_slot())
            record.set("bench.board_buildinfo", connected.buildinfo())
    finally:
        connected.close()
