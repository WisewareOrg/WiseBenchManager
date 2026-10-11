"""The pytest plugin: the board fixtures, the `tst` and `provenance` markers, the record."""

from __future__ import annotations

import os
import re
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any

import pytest

from wisebench import version
from wisebench.board import Board, Transport
from wisebench.config import Config
from wisebench.errors import BenchFailure
from wisebench.events import EventLog
from wisebench.record import Marks, SessionRecord
from wisebench.ssh import FabricTransport

TST_ID = re.compile(r"[A-Z]{3}[0-9]{3}")
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
        self.record.result_dir.mkdir(parents=True, exist_ok=True)
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
    """Read and check an item's `tst` and `provenance` markers; a malformed `tst` raises."""
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
            f"{item.nodeid}: tst id {item_id!r} is not three capitals and three digits"
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
    bench = Bench(result_dir, version.runner_version())
    config.stash[Bench.KEY] = bench
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
def bench_transport(bench_config: Config) -> Iterator[Transport]:
    """The ssh transport to the configured board."""
    transport = FabricTransport(bench_config.board)
    yield transport
    transport.close()


@pytest.fixture(scope="session")
def board(
    request: pytest.FixtureRequest, bench_config: Config, bench_transport: Transport
) -> Iterator[Board]:
    """The configured board, its hostname checked before any other command.

    At session end, when no OTA ran, the booted slot and `/etc/buildinfo` go to the record.
    """
    record = Bench.of(request.config).record
    connected = Board(bench_transport, bench_config.board, record.set)
    yield connected
    if record.properties.get("bench.ota_ran") is not True:
        record.set("bench.booted_slot", connected.booted_slot())
        record.set("bench.board_buildinfo", connected.buildinfo())
