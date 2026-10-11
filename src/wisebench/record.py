"""The run's summary beside pytest-reportlog's record: session facts and each item's credit.

No maintained tool fits because the credit rule, which folds every test carrying an item into one
status, is this project's own; the raw record is pytest-reportlog's.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

INDEX = "index.jsonl"


@dataclass(frozen=True)
class Marks:
    """What a test's markers say: the item it discharges, and its role."""

    item_id: str | None = None
    recorded: bool = False
    provenance: bool = False


@dataclass
class TestRecord:
    """One test's outcome, folded from its setup, call and teardown reports.

    A fixture failing, or a `BenchFailure` raised from the test body, is the bench's failure:
    the outcome is `error` and `bench_failed` is set.
    """

    nodeid: str
    marks: Marks
    outcome: str | None = None
    bench_failed: bool = False

    def add(self, report: pytest.TestReport, bench_failure: bool = False) -> None:
        if bench_failure or (report.when != "call" and report.failed):
            self.bench_failed = True
            self.outcome = "error"
        elif report.when == "setup" and report.skipped:
            self.outcome = "skipped"
        elif report.when == "call":
            self.outcome = self._call_outcome(report)

    @staticmethod
    def _call_outcome(report: pytest.TestReport) -> str:
        if hasattr(report, "wasxfail"):
            return "xfailed" if report.skipped else "xpassed"
        return report.outcome


class Credit:
    """Decides each item's status: `passed`, `recorded` or `not credited` with the reason.

    An item is credited only when the session as a whole can be: the ota fixture ran, the
    bench did not fail, and at least one provenance test ran and every one of them passed.
    """

    def __init__(self, ota_ran: bool, tests: list[TestRecord]) -> None:
        self._reason = self._session_reason(ota_ran, tests)

    @staticmethod
    def _session_reason(ota_ran: bool, tests: list[TestRecord]) -> str | None:
        provenance = [test.outcome for test in tests if test.marks.provenance]
        if not ota_ran:
            return "ota did not run"
        if any(test.bench_failed for test in tests):
            return "the bench failed"
        if not provenance:
            return "no provenance test ran"
        if any(outcome != "passed" for outcome in provenance):
            return "a provenance test did not pass"
        return None

    def status(self, test: TestRecord) -> tuple[str | None, str | None]:
        """The test's item status and the reason it is not credited; `(None, None)` with no item."""
        if test.marks.item_id is None:
            return None, None
        reason = self._reason
        if reason is None and test.outcome != "passed":
            reason = f"test {test.outcome}"
        if reason is not None:
            return "not credited", reason
        return ("recorded" if test.marks.recorded else "passed"), None

    def items(self, tests: list[TestRecord]) -> dict[str, dict[str, str | None]]:
        """Each item's status: credited only when every test carrying it is, and alike."""
        statuses: dict[str, list[tuple[str | None, str | None]]] = {}
        for test in tests:
            if test.marks.item_id is not None:
                statuses.setdefault(test.marks.item_id, []).append(self.status(test))
        return {item: self._item(results) for item, results in statuses.items()}

    @staticmethod
    def _item(results: list[tuple[str | None, str | None]]) -> dict[str, str | None]:
        refused = [reason for status, reason in results if status == "not credited"]
        if refused:
            return {"status": "not credited", "reason": refused[0]}
        kinds = {status for status, _ in results}
        if len(kinds) > 1:
            return {"status": "not credited", "reason": "its tests disagree on recorded"}
        return {"status": kinds.pop(), "reason": None}


class SessionRecord:
    """One session's facts and test outcomes, summarised as `<started>.json` and an index line.

    The raw record is pytest-reportlog's, `<started>.reportlog.jsonl` unless `--report-log` names
    another: every test report, with what each test recorded through `record_property`.
    """

    def __init__(self, result_dir: Path) -> None:
        self.result_dir = result_dir
        self.started = datetime.now(UTC)
        self.properties: dict[str, Any] = {}
        self.tests: dict[str, TestRecord] = {}
        self.stem = self.started.strftime("%Y%m%dT%H%M%S.%fZ")
        self.report = result_dir / f"{self.stem}.reportlog.jsonl"

    def set(self, name: str, value: Any) -> None:
        """Record a session fact under its namespaced name."""
        self.properties[name] = value

    def add(
        self, nodeid: str, marks: Marks, report: pytest.TestReport, bench_failure: bool = False
    ) -> TestRecord:
        test = self.tests.setdefault(nodeid, TestRecord(nodeid, marks))
        test.add(report, bench_failure)
        return test

    def write(self, exit_status: int) -> Path:
        """Write the summary and append its line to `index.jsonl`; return the summary's path."""
        self.properties.setdefault("bench.ota_ran", False)
        self.properties.setdefault("bench.booted_slot", None)
        self.properties.setdefault("bench.board_buildinfo", None)
        tests = list(self.tests.values())
        path = self.result_dir / f"{self.stem}.json"
        summary = {
            "started": self.started.isoformat().replace("+00:00", "Z"),
            "finished": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "exit_status": exit_status,
            "verdict": None,
            "report": os.path.relpath(self.report, self.result_dir),
            **self.properties,
            "items": Credit(self.properties["bench.ota_ran"] is True, tests).items(tests),
        }
        path.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
        index = {
            "timestamp": summary["started"],
            "file": path.name,
            "runner_version": self.properties.get("bench.runner_version"),
            "verdict": None,
            "counts": dict(Counter(test.outcome for test in tests)),
        }
        with (self.result_dir / INDEX).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(index, default=str) + "\n")
        return path
