"""`RunLog`: the stamped run directory and its `bench.log`, opened O_APPEND, touching neither of
the process's own fd 1/2 -- hook and suite output is pointed at the file directly by whoever
spawns them (see test_bench_test.py), not spliced through RunLog itself."""

import os
from datetime import UTC, datetime

from wisebench import run_log
from wisebench.plugin import _TIMESTAMP


class _FixedDatetime:
    """A stand-in for the `datetime` class, `now()` always returning the same fixed instant."""

    @staticmethod
    def now(tz):
        return datetime(2026, 10, 28, 15, 30, 0, tzinfo=tz)


def test_runlog_creates_a_stamped_directory_under_the_given_base(tmp_path):
    """RunLog creates one stamped run directory under the base directory it is given."""
    before = datetime.now(UTC)
    with run_log.RunLog(tmp_path) as log:
        directory = log.directory
    after = datetime.now(UTC)

    assert directory.parent == tmp_path
    assert directory.is_dir()
    parsed = datetime.strptime(directory.name, _TIMESTAMP).replace(tzinfo=UTC)
    assert before.replace(microsecond=0) <= parsed <= after


def test_runlog_suffixes_dash_2_when_the_stamp_already_exists(monkeypatch, tmp_path):
    """A second RunLog for the same instant gets `-2` appended to the stamp."""
    monkeypatch.setattr(run_log, "datetime", _FixedDatetime)
    stamp = _FixedDatetime.now(UTC).strftime(_TIMESTAMP)
    (tmp_path / stamp).mkdir()

    with run_log.RunLog(tmp_path) as log:
        directory = log.directory

    assert directory.name == f"{stamp}-2"


def test_runlog_suffixes_dash_3_when_dash_2_also_exists(monkeypatch, tmp_path):
    """A third RunLog for the same instant gets `-3` appended to the stamp."""
    monkeypatch.setattr(run_log, "datetime", _FixedDatetime)
    stamp = _FixedDatetime.now(UTC).strftime(_TIMESTAMP)
    (tmp_path / stamp).mkdir()
    (tmp_path / f"{stamp}-2").mkdir()

    with run_log.RunLog(tmp_path) as log:
        directory = log.directory

    assert directory.name == f"{stamp}-3"


def test_runlog_creates_an_empty_bench_log_on_entry(tmp_path):
    """RunLog creates bench.log, empty, as soon as it opens -- not on the first write."""
    with run_log.RunLog(tmp_path) as log:
        bench_log = log.directory / "bench.log"
        assert bench_log.is_file()
        assert bench_log.read_text() == ""


def test_runlog_does_not_touch_the_processs_own_fd_1_or_2(tmp_path, capfd):
    """RunLog redirects no hook or suite output itself -- a write to the process's own fd 1
    still reaches the real fd 1, unaffected by an open RunLog, and never lands in bench.log."""
    with run_log.RunLog(tmp_path) as log:
        os.write(1, b"still on fd1\n")

    captured = capfd.readouterr().out
    assert "still on fd1" in captured
    assert "still on fd1" not in (log.directory / "bench.log").read_text()
