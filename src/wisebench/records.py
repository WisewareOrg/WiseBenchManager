"""Records: the records directory read into runs, re-parsed only when a file changed."""

import json
import os
import re
from pathlib import Path

from wisebench.runlog import parse_run

_NAME_RE = re.compile(r"^(?P<stamp>.+?)(?:-(?P<suffix>\d+))?$")
_SUITES = ("host_suite", "target_suite")


def _order(name):
    """Sort key putting `<stamp>-10` after `<stamp>-2`; the bare stamp is suffix 1."""
    match = _NAME_RE.match(name)
    return match["stamp"], int(match["suffix"] or 1)


def _mtime_and_size(path):
    """(mtime_ns, size) of `path`, or None when it does not exist."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_mtime_ns, info.st_size


def _reports(path):
    """The JSON lines of a `report.jsonl`; a line that does not parse (a cut one) is skipped."""
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


class Records:
    """The runs under a records directory, re-parsed only when a file's (mtime, size) changed.

    Args:
        directory (pathlib.Path): The records directory.
    """

    def __init__(self, directory):
        """An empty cache over `directory`."""
        self.directory = Path(directory)
        self._cache = {}

    def runs(self):
        """Every run folder holding a `bench.log` that has a `run started` line, newest first.

        A folder that vanishes or cannot be read is left out.

        Returns:
            list[Run]: The runs, newest first.
        """
        try:
            names = [e.name for e in os.scandir(self.directory) if e.is_dir()]
        except OSError:
            names = []
        runs, seen = [], set()
        for name in sorted(names, key=_order, reverse=True):
            try:
                run = self._run(name)
            except OSError:
                continue
            if run is not None:
                seen.add(name)
                if run.started is not None:
                    runs.append(run)
        self._cache = {name: self._cache[name] for name in seen}
        return runs

    def _run(self, name):
        """The run in folder `name` from the cache or a parse; None without a `bench.log`."""
        folder = self.directory / name
        paths = [folder / "bench.log", *(folder / suite / "report.jsonl" for suite in _SUITES)]
        signature = tuple(_mtime_and_size(path) for path in paths)
        if signature[0] is None:
            return None
        if name in self._cache and self._cache[name][0] == signature:
            return self._cache[name][1]
        text = paths[0].read_bytes().decode("utf-8", errors="replace")
        reports = {
            suite: _reports(path)
            for suite, path in zip(_SUITES, paths[1:], strict=True)
            if path.exists()
        }
        run = parse_run(name, text, reports)
        self._cache[name] = (signature, run)
        return run
