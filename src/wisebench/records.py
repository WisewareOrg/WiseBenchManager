"""Records: every run folder in the records directory, each re-read only when it changed."""

import json
import os
import re
from pathlib import Path

from wisebench.runlog import SUITES, parse_run

_RECORD_NAME = re.compile(r"^(?P<stamp>.+?)(?:-(?P<suffix>\d+))?$")


class Records:
    """The runs in a records directory, newest first.

    A run is re-parsed only when the (mtime, size) of its `bench.log` or of a suite's
    `report.jsonl` changed since the last read.

    Args:
        directory (pathlib.Path): The records directory.
    """

    def __init__(self, directory):
        self.directory = Path(directory)
        self._cache = {}

    def runs(self):
        """Every folder holding a `bench.log` that has a `run started` line, newest first.

        A folder that vanishes or cannot be read is left out.

        Returns:
            list[Run]: The runs.
        """
        try:
            names = [entry.name for entry in os.scandir(self.directory) if entry.is_dir()]
        except OSError:
            names = []
        runs = []
        cache = {}
        for name in sorted(names, key=_record_order, reverse=True):
            try:
                signature, run = self._read(name)
            except OSError:
                continue
            if run is None:
                continue
            cache[name] = signature, run
            if run.started is not None:
                runs.append(run)
        self._cache = cache
        return runs

    def _read(self, name):
        """(signature, run) of one folder, the cached run while its signature is unchanged; run is
        None without a `bench.log`."""
        folder = self.directory / name
        log = folder / "bench.log"
        suite_reports = {suite: folder / suite / "report.jsonl" for suite in SUITES}
        signature = tuple(_signature(path) for path in (log, *suite_reports.values()))
        if signature[0] is None:
            return signature, None
        cached = self._cache.get(name)
        if cached is not None and cached[0] == signature:
            return cached
        text = log.read_bytes().decode("utf-8", errors="replace")
        reports = {
            suite: _read_reports(path) for suite, path in suite_reports.items() if path.exists()
        }
        return signature, parse_run(name, text, reports)


def _signature(path):
    """(mtime_ns, size) of `path`, or None when it does not exist."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_mtime_ns, info.st_size


def _read_reports(path):
    """The JSON lines of a `report.jsonl`, a line that does not parse (one cut short) skipped."""
    reports = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                reports.append(json.loads(line))
            except ValueError:
                continue
    return reports


def _record_order(name):
    """Sort key putting `<stamp>-10` after `<stamp>-2`, and the bare stamp as suffix 1."""
    found = _RECORD_NAME.match(name)
    return found["stamp"], int(found["suffix"] or 1)
