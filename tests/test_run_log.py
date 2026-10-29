"""`RunLog`: the run directory, the real `tee -i -a bench.log`, and fd 1/2 restored after."""

import os
import subprocess
from datetime import UTC, datetime

from wisebench import marks, run_log
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


def test_runlog_writes_reach_both_bench_log_and_fd1(tmp_path, capfd):
    """A write to fd 1 inside the run lands in bench.log and still reaches the real fd 1."""
    with run_log.RunLog(tmp_path) as log:
        os.write(1, b"marker line\n")

    assert "marker line" in capfd.readouterr().out
    assert "marker line" in (log.directory / "bench.log").read_text()


def test_runlog_restores_fds_after_exit(tmp_path, capfd):
    """After RunLog exits, fd 1 no longer feeds bench.log, but still reaches the real fd 1."""
    with run_log.RunLog(tmp_path) as log:
        os.write(1, b"inside\n")
    os.write(1, b"outside\n")

    captured = capfd.readouterr().out
    assert "inside" in captured
    assert "outside" in captured
    log_text = (log.directory / "bench.log").read_text()
    assert "inside" in log_text
    assert "outside" not in log_text


def test_runlog_preserves_order_between_markers_and_a_real_subprocesss_output(tmp_path, capfd):
    """bench.log and fd 1 both see a hook's inherited-stdio output in order with the markers."""
    with run_log.RunLog(tmp_path) as log:
        marks.write_stage_started("precondition")
        subprocess.run(["sh", "-c", "echo hook-output"], check=True)
        marks.write_stage_result("precondition", "passed")

    for text in (capfd.readouterr().out, (log.directory / "bench.log").read_text()):
        assert text.index("stage precondition started") < text.index("hook-output")
        assert text.index("hook-output") < text.index("stage precondition passed")
