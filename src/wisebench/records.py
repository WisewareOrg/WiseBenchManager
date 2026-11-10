"""Records: the records directory read into runs, re-parsed only when a file changed.

Reads files and writes none.
"""

import json
import os
import re
from pathlib import Path

from wisebench.runlog import parse_run

_RECORD_NAME_RE = re.compile(r"^(?P<stamp>.+?)(?:-(?P<suffix>\d+))?$")


def _signature(path):
    """(mtime_ns, size) of `path`, or None when it does not exist."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_mtime_ns, info.st_size


def _read_reports(path):
    """The JSON lines of a `report.jsonl`; a line that does not parse (a cut one) is skipped."""
    reports = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                reports.append(json.loads(line))
            except ValueError:
                continue
    return reports


def _record_order(name):
    """Sort key putting `<stamp>-10` after `<stamp>-2` and the bare stamp as suffix 1."""
    match = _RECORD_NAME_RE.match(name)
    return match["stamp"], int(match["suffix"] or 1)


class Records:
    """The runs under a records directory, re-parsed only when a file's (mtime, size) changed.

    Args:
        directory (pathlib.Path): The records directory.
    """

    def __init__(self, directory):
        self.directory = Path(directory)
        self._cache = {}

    def runs(self):
        """Every run folder holding a `bench.log` that has a `run started` line, newest first.

        A folder that vanishes or cannot be read is left out.

        Returns:
            list[Run]: The runs, newest first.
        """
        try:
            names = [entry.name for entry in os.scandir(self.directory) if entry.is_dir()]
        except OSError:
            names = []
        runs = []
        seen = set()
        for name in sorted(names, key=_record_order, reverse=True):
            try:
                run = self._run(name)
            except OSError:
                continue
            if run is None:
                continue
            seen.add(name)
            if run.started is not None:
                runs.append(run)
        for name in set(self._cache) - seen:
            del self._cache[name]
        return runs

    def _run(self, name):
        """The run in folder `name`, from the cache when no file changed; None without a
        `bench.log`."""
        folder = self.directory / name
        log = folder / "bench.log"
        reports = {
            suite: folder / suite / "report.jsonl" for suite in ("host_suite", "target_suite")
        }
        signature = tuple(_signature(path) for path in (log, *reports.values()))
        if signature[0] is None:
            return None
        cached = self._cache.get(name)
        if cached is not None and cached[0] == signature:
            return cached[1]
        text = log.read_bytes().decode("utf-8", errors="replace")
        run = parse_run(
            name,
            text,
            {suite: _read_reports(path) for suite, path in reports.items() if path.exists()},
        )
        self._cache[name] = (signature, run)
        return run
