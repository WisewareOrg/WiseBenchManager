"""`wisebench.status`: the disk, records and system state read into the run model."""

import functools
import json
import os
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from helpers import (
    repo_with_one_commit,
    write_bench_toml,
    write_lg_env,
    write_suite,
)

from wisebench import bench_test, status

STAMP = "20261110T030715Z"
STARTED = datetime(2026, 11, 10, 3, 0, 0, tzinfo=UTC)
ROOT = os.geteuid() == 0


def mark(event, minute=0, second=0, fraction=""):
    """One `bench:` marker line at 2026-11-10 03:<minute>:<second>, newline-terminated."""
    return f"bench: 2026-11-10T03:{minute:02d}:{second:02d}{fraction}+00:00 {event}\n"


def log(*parts):
    return "".join(parts)


def rep(nodeid, when, outcome, longrepr=None):
    """One pytest-reportlog TestReport line as a dict."""
    out = {"$report_type": "TestReport", "nodeid": nodeid, "when": when, "outcome": outcome}
    if longrepr is not None:
        out["longrepr"] = longrepr
    return out


def parse(text, reports=None, record=STAMP):
    return status.parse_run(record, text, reports or {})


def stage_of(run, name):
    return next(s for s in run.stages if s.name == name)


def suite_log(*body, stage="target_suite"):
    """A log holding one suite stage whose body is `body`, left open."""
    return log(
        mark("run started kind=test ref=main"),
        mark(f"stage {stage} started", 1),
        *body,
    )


def write_record(directory, name=STAMP, text="", reports=None, raw=None):
    """Write `directory/<name>/bench.log` (and each suite's report.jsonl); return the folder."""
    run_dir = Path(directory) / name
    run_dir.mkdir(parents=True)
    (run_dir / "bench.log").write_bytes(raw if raw is not None else text.encode())
    for suite, lines in (reports or {}).items():
        (run_dir / suite).mkdir()
        body = "".join(json.dumps(line) + "\n" for line in lines)
        (run_dir / suite / "report.jsonl").write_text(body)
    return run_dir


# ---------------------------------------------------------------- marker grammar


@pytest.mark.parametrize("result", ["passed", "failed", "error"])
def test_run_started_and_run_result_markers_fill_the_run(result):
    """kind, ref, pr and the start time come from `run started`; result and end from `run <r>`."""
    run = parse(
        log(
            mark("run started kind=run ref=feature/x pr=7"),
            mark(f"run {result}", 12, 30),
        )
    )

    assert run.record == STAMP
    assert run.kind == "run"
    assert run.ref == "feature/x"
    assert run.pr == 7
    assert run.started == STARTED
    assert run.result == result
    assert run.ended == STARTED + timedelta(minutes=12, seconds=30)


def test_a_run_without_pr_sha_or_result_has_none_for_each():
    """No ` pr=`, no `sha=` and no `run <result>` line leave pr, sha, result and ended None."""
    run = parse(
        log(
            mark("run started kind=test ref=main"),
            mark("stage checkout started"),
            mark("stage checkout passed", 0, 5),
        )
    )

    assert run.pr is None
    assert run.result is None
    assert run.ended is None
    assert stage_of(run, "checkout").sha is None


def test_every_stage_state_and_the_sequence_for_a_kind_test_run():
    """Each stage marker shape lands in its state; the sequence is the full one, without posted."""
    run = parse(
        log(
            mark("run started kind=test ref=main"),
            mark("stage checkout started"),
            mark("stage checkout passed sha=0123456789abcdef", 0, 2),
            mark("stage precondition started", 0, 3),
            mark("stage precondition failed reason=hook exited 1", 0, 4),
            mark("stage build skipped", 0, 5),
            mark("stage bundle started", 0, 6),
            mark("stage bundle error reason=boom", 0, 7),
            mark("stage host_suite started", 0, 8),
        )
    )

    assert [s.name for s in run.stages] == [
        "checkout",
        "precondition",
        "build",
        "bundle",
        "host_suite",
        "target_suite",
    ]
    assert [s.state for s in run.stages] == [
        "passed",
        "failed",
        "skipped",
        "error",
        "open",
        "not-started",
    ]
    checkout = stage_of(run, "checkout")
    assert checkout.sha == "0123456789abcdef"
    assert checkout.started == STARTED
    assert checkout.ended == STARTED + timedelta(seconds=2)
    open_stage = stage_of(run, "host_suite")
    assert open_stage.started == STARTED + timedelta(seconds=8)
    assert open_stage.ended is None
    assert stage_of(run, "target_suite").started is None


def test_a_kind_run_adds_the_posted_stage_even_while_still_at_checkout():
    """`posted` belongs to the sequence of kind=run, listed in full from the first line."""
    run = parse(log(mark("run started kind=run ref=main pr=3"), mark("stage checkout started")))

    assert [s.name for s in run.stages][-2:] == ["target_suite", "posted"]
    assert [s.state for s in run.stages] == ["open"] + ["not-started"] * 6


def test_marker_timestamps_parse_with_and_without_fractional_seconds():
    """Both widths of the marker timestamp (whole seconds, microseconds) are read."""
    run = parse(
        log(
            mark("run started kind=test ref=main"),
            mark("stage checkout started", 0, 1, ".250000"),
            mark("stage checkout passed", 0, 3),
        )
    )

    checkout = stage_of(run, "checkout")
    assert checkout.started == STARTED + timedelta(seconds=1, microseconds=250000)
    assert checkout.ended == STARTED + timedelta(seconds=3)


def test_a_nodeid_with_spaces_brackets_and_an_absolute_path_is_kept_whole():
    """A trailing value runs to end of line: the section name is the nodeid verbatim."""
    nodeid = "/abs/path/target suite/test_x.py::test_case[a b-/c d]"
    run = parse(
        suite_log(
            mark(f"test queued {nodeid}", 1, 1),
            mark(f"test started {nodeid}", 1, 2),
        )
    )

    (section,) = stage_of(run, "target_suite").sections
    assert section.kind == "test"
    assert section.name == nodeid


def test_a_bench_prefix_that_is_not_at_line_start_is_output_not_a_marker():
    """Only a line that starts `bench: ` is a marker; any other line is the stage's output."""
    mid_line = "see bench: 2026-11-10T03:05:00+00:00 run passed"
    run = parse(
        log(
            mark("run started kind=test ref=main"),
            mark("stage build started", 1),
            "plain output\n",
            mid_line + "\n",
        )
    )

    assert run.result is None
    assert stage_of(run, "build").lines == ("plain output", mid_line)


# ---------------------------------------------------------------- sections


def test_sections_split_at_markers_with_leading_lines_on_the_stage_and_summary_in_the_last():
    """A section runs to the next marker; lines before the first marker belong to the stage;
    lines after the last `test started` stay in that section."""
    run = parse(
        suite_log(
            "collecting ...\n",
            mark("test queued t.py::a", 1, 1),
            mark("test queued t.py::b", 1, 1),
            "stage preamble\n",
            mark("setup fx started", 1, 2),
            "fixture output\n",
            mark("test started t.py::a", 1, 3),
            "a output 1\n",
            "a output 2\n",
            mark("test started t.py::b", 1, 4),
            "b output\n",
            "=== 2 passed in 1.0s ===\n",
        )
    )

    target = stage_of(run, "target_suite")
    assert target.lines == ("collecting ...", "stage preamble")
    by_name = {s.name: s for s in target.sections}
    assert by_name["fx"].kind == "setup"
    assert by_name["fx"].lines == ("fixture output",)
    assert by_name["t.py::a"].lines == ("a output 1", "a output 2")
    assert by_name["t.py::b"].lines == ("b output", "=== 2 passed in 1.0s ===")
    assert by_name["t.py::a"].started == STARTED + timedelta(minutes=1, seconds=3)


def test_section_ids_are_stage_kind_name_and_occurrence_and_equal_across_two_parses():
    """The id is (stage, setup|test, name, occurrence index) and survives a full re-parse."""
    first = suite_log(
        mark("test queued t.py::a", 1, 1),
        mark("test started t.py::a", 1, 2),
        mark("test started t.py::a", 1, 3),
    )
    longer = first + "more output\n"

    before = [s.id for s in stage_of(parse(first), "target_suite").sections]
    after = [s.id for s in stage_of(parse(longer), "target_suite").sections]

    assert before == [
        ("target_suite", "test", "t.py::a", 0),
        ("target_suite", "test", "t.py::a", 1),
    ]
    assert after == before


def test_test_rows_follow_queue_order_with_unstarted_tests_emitted_in_place():
    """Queued tests that never started are body-less rows: before the next started test, or
    after the last row at stage end; a setup section sits at its marker."""
    run = parse(
        log(
            mark("run started kind=test ref=main"),
            mark("stage target_suite started", 1),
            *(mark(f"test queued t.py::{n}", 1, 1) for n in "abcde"),
            mark("test started t.py::a", 1, 2),
            mark("setup fx started", 1, 3),
            mark("test started t.py::c", 1, 4),
            mark("stage target_suite passed", 1, 5),
        ),
        {
            "target_suite": [
                rep("t.py::a", "call", "passed"),
                rep("t.py::b", "setup", "skipped"),
                rep("t.py::c", "call", "failed"),
                rep("t.py::d", "setup", "failed"),
            ]
        },
    )

    sections = stage_of(run, "target_suite").sections
    assert [s.name for s in sections] == [
        "t.py::a",
        "fx",
        "t.py::b",
        "t.py::c",
        "t.py::d",
        "t.py::e",
    ]
    tests = {s.name: s for s in sections if s.kind == "test"}
    assert {name: s.outcome for name, s in tests.items()} == {
        "t.py::a": "passed",
        "t.py::b": "skipped",
        "t.py::c": "failed",
        "t.py::d": "failed",
        "t.py::e": "not-started",
    }
    assert tests["t.py::b"].lines == ()
    assert tests["t.py::e"].started is None


@pytest.mark.parametrize(
    ("phases", "outcome"),
    [
        ((("setup", "passed"), ("call", "passed"), ("teardown", "passed")), "passed"),
        ((("setup", "passed"), ("call", "failed"), ("teardown", "passed")), "failed"),
        ((("setup", "passed"), ("call", "skipped"), ("teardown", "passed")), "skipped"),
        ((("setup", "passed"), ("call", "passed"), ("teardown", "failed")), "failed"),
        ((("setup", "failed"), ("teardown", "passed")), "failed"),
        ((("setup", "skipped"), ("call", "failed"), ("teardown", "passed")), "failed"),
    ],
)
def test_a_tests_outcome_is_the_worst_of_its_phases(phases, outcome):
    """failed beats skipped beats passed across setup, call and teardown reports."""
    run = parse(
        suite_log(
            mark("test queued t.py::a", 1, 1),
            mark("test started t.py::a", 1, 2),
        ),
        {"target_suite": [rep("t.py::a", when, result) for when, result in phases]},
    )

    (section,) = stage_of(run, "target_suite").sections
    assert section.outcome == outcome


def test_a_started_test_with_no_report_yet_is_open():
    """A test that began and has no report is still `open`."""
    run = parse(
        suite_log(
            mark("test queued t.py::a", 1, 1),
            mark("test started t.py::a", 1, 2),
        )
    )

    (section,) = stage_of(run, "target_suite").sections
    assert section.outcome == "open"


@pytest.mark.parametrize(
    ("setup_outcome", "expected"), [("passed", "passed"), ("failed", "failed")]
)
def test_a_setup_sections_outcome_is_the_first_following_tests_setup_report(
    setup_outcome, expected
):
    """The setup row takes the `when=setup` outcome of the test that follows it."""
    run = parse(
        suite_log(
            mark("test queued t.py::a", 1, 1),
            mark("setup fx started", 1, 2),
            mark("test started t.py::a", 1, 3),
        ),
        {"target_suite": [rep("t.py::a", "setup", setup_outcome)]},
    )

    setup = next(s for s in stage_of(run, "target_suite").sections if s.kind == "setup")
    assert setup.outcome == expected


def test_stage_counts_are_started_or_reported_queued_passed_and_failed():
    """done = queued tests started or reported; passed = worst passed; failed = worst failed;
    skipped counts in neither."""
    run = parse(
        suite_log(
            *(mark(f"test queued t.py::{n}", 1, 1) for n in "abcde"),
            mark("test started t.py::a", 1, 2),
            mark("test started t.py::b", 1, 3),
            mark("test started t.py::c", 1, 4),
        ),
        {
            "target_suite": [
                rep("t.py::a", "call", "passed"),
                rep("t.py::b", "call", "failed"),
                rep("t.py::c", "call", "skipped"),
                rep("t.py::d", "setup", "failed"),
            ]
        },
    )

    target = stage_of(run, "target_suite")
    assert (target.done, target.queued, target.passed, target.failed) == (4, 5, 1, 2)


def test_a_failed_test_carries_its_crash_line_and_the_last_traceback_entrys_lines():
    """failure = `path:lineno: message`, then the lines of the last reprtraceback entry."""
    longrepr = {
        "reprcrash": {"path": "/w/t.py", "lineno": 12, "message": "assert 1 == 2"},
        "reprtraceback": {
            "reprentries": [
                {"type": "ReprEntry", "data": {"lines": ["first entry"]}},
                {"type": "ReprEntry", "data": {"lines": ["    def f():", ">   assert 1 == 2"]}},
            ]
        },
        "sections": [],
    }
    run = parse(
        suite_log(
            mark("test queued t.py::bad", 1, 1),
            mark("test queued t.py::good", 1, 1),
            mark("test started t.py::bad", 1, 2),
            mark("test started t.py::good", 1, 3),
        ),
        {
            "target_suite": [
                rep("t.py::bad", "call", "failed", longrepr),
                rep("t.py::good", "call", "passed"),
            ]
        },
    )

    sections = {s.name: s for s in stage_of(run, "target_suite").sections}
    assert sections["t.py::bad"].failure == (
        "/w/t.py:12: assert 1 == 2",
        "    def f():",
        ">   assert 1 == 2",
    )
    assert sections["t.py::good"].failure == ()


def test_a_stage_with_no_section_markers_keeps_all_its_lines_and_no_sections():
    """Old-layout logs, non-suite stages and collection errors: lines on the stage, no sections."""
    run = parse(
        log(
            mark("run started kind=test ref=main"),
            mark("stage host_suite started", 1),
            "collected 0 items\n",
            "ERROR collecting t.py\n",
            mark("stage host_suite error reason=pytest exited 2", 2),
        )
    )

    host = stage_of(run, "host_suite")
    assert host.sections == ()
    assert host.lines == ("collected 0 items", "ERROR collecting t.py")


# ---------------------------------------------------------------- bytes and partial lines


def test_crlf_and_invalid_utf8_are_read_without_raising(tmp_path):
    """A trailing `\\r` is dropped from every line; invalid UTF-8 decodes with replacement."""
    raw = (
        mark("run started kind=test ref=main").replace("\n", "\r\n")
        + mark("stage build started", 1).replace("\n", "\r\n")
        + "out \xff line\r\n"
        + mark("stage build passed", 2).replace("\n", "\r\n")
    ).encode("latin-1")
    write_record(tmp_path, raw=raw)

    (run,) = status.Records(tmp_path).runs()

    build = stage_of(run, "build")
    assert run.ref == "main"
    assert build.state == "passed"
    assert build.lines == ("out \ufffd line",)


def test_a_last_line_without_its_newline_is_held_until_the_newline_lands(tmp_path):
    """Bytes after the final `\\n` are not parsed; once the newline is written they are."""
    head = log(mark("run started kind=test ref=main"), mark("stage build started", 1))
    run_dir = write_record(tmp_path, text=head + "half an output line")
    records = status.Records(tmp_path)

    (before,) = records.runs()
    assert stage_of(before, "build").lines == ()

    with open(run_dir / "bench.log", "ab") as f:
        f.write(b"\n")
    (after,) = records.runs()
    assert stage_of(after, "build").lines == ("half an output line",)

    marker = mark("stage build passed", 2).rstrip("\n")
    with open(run_dir / "bench.log", "ab") as f:
        f.write(marker.encode())
    (held,) = records.runs()
    assert stage_of(held, "build").state == "open"

    with open(run_dir / "bench.log", "ab") as f:
        f.write(b"\n")
    (done,) = records.runs()
    assert stage_of(done, "build").state == "passed"


# ---------------------------------------------------------------- build progress


KAS = "2026-11-10 03:10:00 - INFO     - NOTE: "


def build_log(*lines):
    return log(
        mark("run started kind=test ref=main"),
        mark("stage build started", 1),
        *(line + "\n" for line in lines),
    )


def test_build_progress_reads_the_last_task_count_and_the_tasks_still_running():
    """Last `Running task N of M` anywhere on a line; tasks Started with no later
    Succeeded/Failed; kas-prefixed starts are host-local times, bare ones None."""
    run = parse(
        build_log(
            KAS + "Running task 5 of 10 (/layers/a.bb:do_fetch)",
            KAS + "recipe linux-yocto-6.6.1-r0: task do_compile: Started",
            KAS + "recipe zlib-1.3-r0: task do_compile: Started",
            "NOTE: recipe busybox-1.36-r0: task do_install: Started",
            "NOTE: recipe zlib-1.3-r0: task do_compile: Succeeded",
            "NOTE: recipe gcc-13-r0: task do_compile: Started",
            "NOTE: recipe gcc-13-r0: task do_compile: Failed",
            "prefix NOTE: Running task 7 of 10 (/layers/b.bb:do_compile)",
        )
    )

    progress = stage_of(run, "build").progress
    assert (progress.done, progress.total) == (7, 10)
    assert {(t.recipe, t.task, t.started) for t in progress.tasks} == {
        ("linux-yocto", "do_compile", datetime(2026, 11, 10, 3, 10, 0).astimezone()),
        ("busybox", "do_install", None),
    }
    assert all(isinstance(t, status.Task) for t in progress.tasks)


@pytest.mark.parametrize(
    ("full", "shown"),
    [
        ("linux-yocto-6.6.1-r0", "linux-yocto"),
        ("lib-foo-bar-1.0-r0", "lib-foo-bar"),
        ("base-files", "base-files"),
        ("zlib", "zlib"),
    ],
)
def test_the_recipe_loses_its_last_two_dash_fields_only_when_it_has_three_or_more(full, shown):
    """bitbake's PN-PV-PR is reduced to PN; a name of fewer than three fields is verbatim."""
    run = parse(
        build_log(
            "NOTE: Running task 1 of 2 (/l/a.bb:do_x)",
            f"NOTE: recipe {full}: task do_compile: Started",
        )
    )

    (task,) = stage_of(run, "build").progress.tasks
    assert task.recipe == shown


def test_build_progress_is_none_without_a_running_task_line_or_outside_the_build_stage():
    """No `Running task` line gives None; the same line in another stage gives none either."""
    no_count = parse(build_log("NOTE: recipe x-1-r0: task do_x: Started"))
    other_stage = parse(
        log(
            mark("run started kind=test ref=main"),
            mark("stage precondition started", 1),
            "NOTE: Running task 1 of 2 (/l/a.bb:do_x)\n",
        )
    )

    assert stage_of(no_count, "build").progress is None
    assert stage_of(other_stage, "precondition").progress is None


# ---------------------------------------------------------------- Records


def test_runs_are_newest_first_by_stamp_then_numeric_suffix_and_need_a_bench_log(tmp_path):
    """`-10` sorts after `-2`; the bare stamp is suffix 1; folders without bench.log and plain
    files are not runs."""
    text = log(mark("run started kind=test ref=main"))
    for name in (STAMP, f"{STAMP}-2", f"{STAMP}-10", "20261111T000000Z", "20261109T000000Z"):
        write_record(tmp_path, name, text)
    (tmp_path / "20261112T000000Z" / "host_suite").mkdir(parents=True)
    (tmp_path / "DISABLED").write_text("x\n")

    names = [run.record for run in status.Records(tmp_path).runs()]

    assert names == [
        "20261111T000000Z",
        f"{STAMP}-10",
        f"{STAMP}-2",
        STAMP,
        "20261109T000000Z",
    ]


def test_suite_reports_are_read_from_each_suites_report_jsonl_and_a_cut_last_line_is_ignored(
    tmp_path,
):
    """Records parses both report.jsonl files; an unparseable final line is dropped, not fatal."""
    text = suite_log(
        mark("test queued t.py::a", 1, 1),
        mark("test queued t.py::b", 1, 1),
        mark("test started t.py::a", 1, 2),
        mark("test started t.py::b", 1, 3),
    )
    run_dir = write_record(
        tmp_path,
        text=text,
        reports={"target_suite": [rep("t.py::a", "call", "passed")]},
    )
    with open(run_dir / "target_suite" / "report.jsonl", "a") as f:
        f.write('{"$report_type": "TestReport", "nodeid": "t.py::b", "wh')

    (run,) = status.Records(tmp_path).runs()

    sections = {s.name: s.outcome for s in stage_of(run, "target_suite").sections}
    assert sections == {"t.py::a": "passed", "t.py::b": "open"}


def test_a_removed_record_drops_out_of_the_next_runs(tmp_path):
    """A record that vanishes after a first `.runs()` is left out of the next one."""
    text = log(mark("run started kind=test ref=main"))
    write_record(tmp_path, STAMP, text)
    gone = write_record(tmp_path, "20261111T000000Z", text)
    records = status.Records(tmp_path)
    assert len(records.runs()) == 2

    (gone / "bench.log").unlink()
    gone.rmdir()

    assert [r.record for r in records.runs()] == [STAMP]


@pytest.mark.skipif(ROOT, reason="root reads a mode-000 file")
def test_an_unreadable_record_drops_out_of_the_next_runs(tmp_path):
    """A changed bench.log that cannot be read is left out, not raised."""
    text = log(mark("run started kind=test ref=main"))
    write_record(tmp_path, STAMP, text)
    bad = write_record(tmp_path, "20261111T000000Z", text)
    records = status.Records(tmp_path)
    assert len(records.runs()) == 2

    (bad / "bench.log").write_text(text + "more\n")
    (bad / "bench.log").chmod(0)

    assert [r.record for r in records.runs()] == [STAMP]


def test_a_same_size_rewrite_with_the_old_mtime_is_not_reread_but_a_size_change_is(tmp_path):
    """A log is re-parsed only when its (mtime, size) changed."""
    old = log(mark("run started kind=test ref=aaaa"))
    new = log(mark("run started kind=test ref=bbbb"))
    assert len(old) == len(new)
    path = write_record(tmp_path, text=old) / "bench.log"
    before = path.stat()
    records = status.Records(tmp_path)
    assert [r.ref for r in records.runs()] == ["aaaa"]

    path.write_text(new)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert [r.ref for r in records.runs()] == ["aaaa"]

    path.write_text(new + mark("stage checkout started", 1))
    assert [r.ref for r in records.runs()] == ["bbbb"]
    assert stage_of(records.runs()[0], "checkout").state == "open"


# ---------------------------------------------------------------- liveness


def proc_tree(tmp_path, pid, start_epoch, holders, comm="bench run"):
    """A fake /proc: `locks` with `holders` lines, `stat` with btime, `<pid>/stat` starting at
    `start_epoch`."""
    proc = tmp_path / "proc"
    (proc / str(pid)).mkdir(parents=True)
    ticks_per_second = os.sysconf("SC_CLK_TCK")
    btime = int(start_epoch) - 1000
    (proc / "stat").write_text(f"cpu  1 2 3\nbtime {btime}\nprocesses 9\n")
    fields = ["0"] * 48
    fields[17] = str((int(start_epoch) - btime) * ticks_per_second)
    (proc / str(pid) / "stat").write_text(f"{pid} ({comm}) S 1 " + " ".join(fields) + "\n")
    lines = [
        f"{i}: FLOCK  ADVISORY  {mode} {holder_pid} {dev}:{inode} 0 EOF"
        for i, (mode, holder_pid, dev, inode) in enumerate(holders, 1)
    ]
    (proc / "locks").write_text("\n".join(lines) + ("\n" if lines else ""))
    return proc


def lock_file(tmp_path):
    lock = tmp_path / "records" / "lock"
    lock.parent.mkdir(exist_ok=True)
    lock.touch()
    return lock.parent, lock.stat().st_ino


def live_run(*extra):
    return parse(log(mark("run started kind=run ref=main pr=1"), *extra))


@pytest.mark.parametrize(
    ("holder_start_offset", "expected"),
    [(-3600, True), (0, True), (2, True), (3, False), (3600, False)],
)
def test_a_run_is_live_when_the_lock_holder_started_no_later_than_two_seconds_after_it(
    tmp_path, holder_start_offset, expected
):
    """The FLOCK WRITE holder on the lock's inode, started <= run started + 2 s; the `maj:min`
    in /proc/locks is not compared (it deliberately differs from the file's device here)."""
    directory, inode = lock_file(tmp_path)
    proc = proc_tree(
        tmp_path,
        4242,
        STARTED.timestamp() + holder_start_offset,
        [("WRITE", 4242, "ff:ff", inode)],
    )

    assert status.is_live(directory, live_run(), proc) is expected


def test_a_run_is_not_live_with_no_holder_no_lock_file_or_a_holder_on_another_inode(tmp_path):
    """No FLOCK on the lock's inode, or no lock file, means not live."""
    directory, inode = lock_file(tmp_path)
    other = proc_tree(
        tmp_path, 4242, STARTED.timestamp() - 5, [("WRITE", 4242, "00:2c", inode + 1)]
    )
    assert status.is_live(directory, live_run(), other) is False

    empty = proc_tree(tmp_path / "empty", 4242, STARTED.timestamp() - 5, [])
    assert status.is_live(directory, live_run(), empty) is False

    held = proc_tree(
        tmp_path / "held", 4242, STARTED.timestamp() - 5, [("WRITE", 4242, "00:2c", inode)]
    )
    (directory / "lock").unlink()
    assert status.is_live(directory, live_run(), held) is False


def test_a_run_with_a_result_line_is_not_live_even_while_the_lock_is_still_held(tmp_path):
    """`run error` is written before the lock is released: the result line ends liveness."""
    directory, inode = lock_file(tmp_path)
    proc = proc_tree(tmp_path, 4242, STARTED.timestamp() - 5, [("WRITE", 4242, "00:2c", inode)])

    assert status.is_live(directory, live_run(), proc) is True
    assert status.is_live(directory, live_run(mark("run error", 0, 9)), proc) is False


# ---------------------------------------------------------------- timer and bench state


def stub_systemctl(monkeypatch, returncode=None, raises=None):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(command, returncode, stdout="", stderr="")

    monkeypatch.setattr(status.subprocess, "run", run)
    return calls


def test_the_timer_is_asked_with_is_active_and_a_one_second_timeout(monkeypatch):
    """argv is `systemctl --user is-active <unit>.timer`, timeout=1."""
    calls = stub_systemctl(monkeypatch, returncode=0)

    status.timer_state("bench-run")

    ((command, kwargs),) = calls
    assert command == ["systemctl", "--user", "is-active", "bench-run.timer"]
    assert kwargs["timeout"] == 1


@pytest.mark.parametrize(("returncode", "expected"), [(0, "on"), (3, "off"), (1, "—"), (4, "—")])
def test_timer_state_maps_the_exit_code(monkeypatch, returncode, expected):
    """rc 0 is on, rc 3 is off, any other rc is the dash."""
    stub_systemctl(monkeypatch, returncode=returncode)

    assert status.timer_state("bench-run") == expected


@pytest.mark.parametrize(
    "error", [FileNotFoundError("systemctl"), subprocess.TimeoutExpired("systemctl", 1)]
)
def test_timer_state_is_the_dash_when_systemctl_is_absent_or_times_out(monkeypatch, error):
    """An absent binary or a timeout reads as the dash, never raises."""
    stub_systemctl(monkeypatch, raises=error)

    assert status.timer_state("bench-run") == "—"


def test_read_run_unit_returns_the_unit_from_bench_toml(tmp_path):
    """`[run] unit` of cwd/bench.toml."""
    write_bench_toml(tmp_path)
    with open(tmp_path / "bench.toml", "a") as f:
        f.write('\n[run]\nunit = "bench-run"\n')

    assert status.read_run_unit(tmp_path) == "bench-run"


def test_read_run_unit_is_none_without_a_run_table_a_file_or_valid_toml(tmp_path):
    """No `[run]`, no bench.toml, and a bench.toml that does not load all give None."""
    no_run = tmp_path / "no_run"
    no_run.mkdir()
    write_bench_toml(no_run)
    missing = tmp_path / "missing"
    missing.mkdir()
    invalid = tmp_path / "invalid"
    invalid.mkdir()
    (invalid / "bench.toml").write_text("[run\nunit = ")

    assert status.read_run_unit(no_run) is None
    assert status.read_run_unit(missing) is None
    assert status.read_run_unit(invalid) is None


def test_read_bench_combines_disabled_presence_liveness_and_timer(tmp_path, monkeypatch):
    """disabled = DISABLED exists (content unread); live = is_live of the newest run; timer =
    timer_state(run_unit), the dash for a None unit."""
    directory, inode = lock_file(tmp_path)
    proc = proc_tree(tmp_path, 4242, STARTED.timestamp() - 5, [("WRITE", 4242, "00:2c", inode)])
    calls = stub_systemctl(monkeypatch, returncode=0)
    newest = live_run()

    quiet = status.read_bench(directory, newest, "bench-run", proc)
    assert quiet == status.Bench(live=True, disabled=False, timer="on")

    (directory / "DISABLED").write_bytes(b"\xff\xfe not text")
    disabled = status.read_bench(directory, newest, None, proc)
    assert disabled == status.Bench(live=True, disabled=True, timer="—")
    assert len(calls) == 1


def test_read_bench_is_not_live_without_a_newest_run(tmp_path, monkeypatch):
    """`newest` None means no run to be live."""
    directory, inode = lock_file(tmp_path)
    proc = proc_tree(tmp_path, 4242, STARTED.timestamp() - 5, [("WRITE", 4242, "00:2c", inode)])
    stub_systemctl(monkeypatch, returncode=3)

    bench = status.read_bench(directory, None, "bench-run", proc)

    assert bench == status.Bench(live=False, disabled=False, timer="off")


# ---------------------------------------------------------------- read-only tick


def snapshot(root):
    """Every entry under root: kind, mode-independent mtime_ns, and file bytes."""
    seen = {}
    for path in sorted([root, *root.rglob("*")]):
        info = path.lstat()
        body = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
        seen[str(path)] = (stat.S_IFMT(info.st_mode), info.st_mtime_ns, body)
    return seen


@pytest.mark.skipif(ROOT, reason="root ignores read-only modes")
def test_a_refresh_writes_nothing_takes_no_lock_and_runs_only_the_timer_query(
    tmp_path, monkeypatch
):
    """Records.runs, is_live and timer_state over a read-only tree: bytes and mtimes unchanged,
    no flock/lockf call, and the only process started is the `systemctl` timer query."""
    import fcntl

    records = tmp_path / "records"
    run_dir = write_record(
        records,
        text=suite_log(
            mark("test queued t.py::a", 1, 1),
            mark("test started t.py::a", 1, 2),
        ),
        reports={"target_suite": [rep("t.py::a", "call", "passed")]},
    )
    (run_dir / "host_suite").mkdir()
    (run_dir / "host_suite" / "report.jsonl").write_text(
        json.dumps(rep("t.py::h", "call", "passed")) + "\n"
    )
    (records / "DISABLED").write_text("reason\n")
    (records / "lock").touch()
    proc = proc_tree(
        tmp_path,
        4242,
        STARTED.timestamp() - 5,
        [("WRITE", 4242, "00:2c", (records / "lock").stat().st_ino)],
    )
    before = snapshot(tmp_path)
    forbidden = []
    popen_argv = []

    def refuse_lock(*args, **kwargs):
        forbidden.append(args)
        raise AssertionError("a lock was taken")

    def record_popen(argv, *args, **kwargs):
        popen_argv.append(list(argv))
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(fcntl, "flock", refuse_lock)
    monkeypatch.setattr(fcntl, "lockf", refuse_lock)
    monkeypatch.setattr(subprocess, "Popen", record_popen)
    paths = [p for p in [tmp_path, *tmp_path.rglob("*")]]
    for path in paths:
        path.chmod(0o555 if path.is_dir() else 0o444)
    try:
        runs = status.Records(records).runs()
        status.is_live(records, runs[0], proc)
        status.timer_state("bench-run")
        status.read_bench(records, runs[0], "bench-run", proc)
    finally:
        for path in paths:
            path.chmod(0o755 if path.is_dir() else 0o644)

    assert forbidden == []
    assert snapshot(tmp_path) == before
    assert popen_argv
    assert all(
        argv == ["systemctl", "--user", "is-active", "bench-run.timer"] for argv in popen_argv
    )


# ---------------------------------------------------------------- writer/parser agreement


def test_a_bench_log_written_by_a_real_bench_test_run_parses_into_the_model(monkeypatch, tmp_path):
    """The parser reads what `BenchTest.run` wrote: stage states, sha, pr, posted, sections and
    the real report.jsonl outcome."""
    monkeypatch.setattr(bench_test.subprocess, "run", functools.partial(subprocess.run, timeout=60))
    monkeypatch.chdir(tmp_path)
    repo_with_one_commit(tmp_path)
    write_bench_toml(tmp_path)
    write_suite(tmp_path, "host_suite", passing=True)
    write_suite(tmp_path, "target_suite", passing=True)
    lg_env = write_lg_env(tmp_path)
    posted = []

    bench_test.BenchTest(tmp_path, "HEAD", None, kind="run", pr=7).run(
        lg_env, [], post=posted.append
    )

    (run,) = status.Records(tmp_path / "records").runs()
    assert (run.kind, run.ref, run.pr, run.result) == ("run", "HEAD", 7, "passed")
    assert [s.name for s in run.stages] == [
        "checkout",
        "precondition",
        "build",
        "bundle",
        "host_suite",
        "target_suite",
        "posted",
    ]
    assert {s.state for s in run.stages} == {"passed"}
    assert len(stage_of(run, "checkout").sha) == 40
    for name in ("host_suite", "target_suite"):
        stage = stage_of(run, name)
        (section,) = stage.sections
        assert section.name == f"{name}/test_one.py::test_one"
        assert section.outcome == "passed"
        assert (stage.done, stage.queued, stage.passed, stage.failed) == (1, 1, 1, 0)
