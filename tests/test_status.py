"""`wisebench.runlog`, `records` and `bench_state`: the disk, records and system state read into
the run model."""

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
from status_helpers import mark, real_run, rep

from wisebench import bench_state, bench_test, runlog, screen, status
from wisebench.lock import Lock
from wisebench.records import Records

STAMP = "20261110T030715Z"
TEST_STARTED = mark("run started kind=test ref=main")
STARTED = datetime(2026, 11, 10, 3, 0, 0, tzinfo=UTC)
ROOT = os.geteuid() == 0


def log(*parts):
    return "".join(parts)


def parse(text, reports=None, record=STAMP):
    return runlog.parse_run(record, text, reports or {})


def stage_of(run, name):
    return next(s for s in run.stages if s.name == name)


def suite_log(*body, stage="target_suite"):
    """A log holding one suite stage whose body is `body`, left open."""
    return log(TEST_STARTED, mark(f"stage {stage} started", 1), *body)


def target(*body, reports=None):
    """The target_suite stage parsed from `body`, with `reports` as its report.jsonl lines."""
    run = parse(suite_log(*body), {"target_suite": reports} if reports else None)
    return stage_of(run, "target_suite")


def queued(*ids):
    return [mark(f"test queued {i}", 1, 1) for i in ids]


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


def secs(n, micro=0):
    return STARTED + timedelta(seconds=n, microseconds=micro)


GRAMMAR = {
    "run fields": (
        log(mark("run started kind=run ref=feature/x pr=7"), mark("run failed", 12, 30)),
        lambda r: (r.kind, r.ref, r.pr, r.started, r.result, r.ended),
        ("run", "feature/x", 7, STARTED, "failed", STARTED + timedelta(minutes=12, seconds=30)),
    ),
    "absent pr, sha and result are None": (
        log(TEST_STARTED, mark("stage checkout started"), mark("stage checkout passed", 0, 5)),
        lambda r: (r.pr, r.result, r.ended, stage_of(r, "checkout").sha),
        (None, None, None, None),
    ),
    "every stage state, in the sequence of a test run": (
        log(
            TEST_STARTED,
            mark("stage checkout started"),
            mark("stage checkout passed sha=0123456789abcdef", 0, 2),
            mark("stage precondition started", 0, 3),
            mark("stage precondition failed reason=hook exited 1", 0, 4),
            mark("stage build skipped", 0, 5),
            mark("stage bundle started", 0, 6),
            mark("stage bundle error reason=boom", 0, 7),
            mark("stage host_suite started", 0, 8),
        ),
        lambda r: (
            [(s.name, s.state) for s in r.stages],
            stage_of(r, "checkout").sha,
            stage_of(r, "host_suite").ended,
            stage_of(r, "target_suite").started,
        ),
        (
            [
                ("checkout", "passed"),
                ("precondition", "failed"),
                ("build", "skipped"),
                ("bundle", "error"),
                ("host_suite", "open"),
                ("target_suite", "not-started"),
            ],
            "0123456789abcdef",
            None,
            None,
        ),
    ),
    "whole-second and microsecond timestamps": (
        log(
            TEST_STARTED,
            mark("stage checkout started", 0, 1, ".250000"),
            mark("stage checkout passed", 0, 3),
        ),
        lambda r: (stage_of(r, "checkout").started, stage_of(r, "checkout").ended),
        (secs(1, 250000), secs(3)),
    ),
    "a nodeid with spaces, brackets and an absolute path is kept whole": (
        suite_log(
            mark("test queued /abs/target suite/t.py::case[a b-/c d]", 1, 1),
            mark("test started /abs/target suite/t.py::case[a b-/c d]", 1, 2),
        ),
        lambda r: [s.name for s in stage_of(r, "target_suite").sections],
        ["/abs/target suite/t.py::case[a b-/c d]"],
    ),
    "a bench prefix mid-line is output, not a marker": (
        log(
            TEST_STARTED,
            mark("stage build started", 1),
            "see bench: 2026-11-10T03:05:00+00:00 run passed\n",
        ),
        lambda r: (r.result, stage_of(r, "build").lines),
        (None, ("see bench: 2026-11-10T03:05:00+00:00 run passed",)),
    ),
}


@pytest.mark.parametrize(("text", "read", "expected"), GRAMMAR.values(), ids=GRAMMAR)
def test_the_marker_grammar_is_read_into_the_run(text, read, expected):
    assert read(parse(text)) == expected


def test_sections_split_at_markers_and_ids_are_stable_across_a_re_parse():
    """A section runs to the next marker; lines before the first marker belong to the stage; the
    last section keeps the summary; the id is (stage, kind, name, occurrence) and survives."""
    body = [
        "collecting ...\n",
        *queued("t.py::a", "t.py::b"),
        "stage preamble\n",
        mark("setup fx started", 1, 2),
        "fixture output\n",
        mark("test started t.py::a", 1, 3),
        "a output\n",
        mark("test started t.py::a", 1, 4),
        mark("test started t.py::b", 1, 5),
        "=== 2 passed in 1.0s ===\n",
    ]
    stage = target(*body)

    assert stage.lines == ("collecting ...", "stage preamble")
    assert [(s.id[1:], s.lines) for s in stage.sections] == [
        (("setup", "fx", 0), ("fixture output",)),
        (("test", "t.py::a", 0), ("a output",)),
        (("test", "t.py::a", 1), ()),
        (("test", "t.py::b", 0), ("=== 2 passed in 1.0s ===",)),
    ]
    assert [s.id for s in target(*body, "more\n").sections] == [s.id for s in stage.sections]


def test_test_rows_follow_queue_order_with_unstarted_tests_emitted_in_place():
    """Queued tests that never started are body-less rows: before the next started test, or
    after the last row at stage end; a setup section sits at its marker."""
    stage = target(
        *queued(*(f"t.py::{n}" for n in "abcde")),
        mark("test started t.py::a", 1, 2),
        mark("setup fx started", 1, 3),
        mark("test started t.py::c", 1, 4),
        reports=[
            rep("t.py::a", "call", "passed"),
            rep("t.py::a", "teardown", "passed"),
            rep("t.py::b", "setup", "skipped"),
            rep("t.py::b", "teardown", "passed"),
            rep("t.py::c", "call", "failed"),
            rep("t.py::c", "teardown", "passed"),
            rep("t.py::d", "setup", "failed"),
            rep("t.py::d", "teardown", "passed"),
        ],
    )

    assert [s.name for s in stage.sections] == ["t.py::a", "fx", *(f"t.py::{n}" for n in "bcde")]
    assert [(s.name, s.outcome) for s in stage.sections if s.kind == "test"] == [
        ("t.py::a", "passed"),
        ("t.py::b", "skipped"),
        ("t.py::c", "failed"),
        ("t.py::d", "failed"),
        ("t.py::e", "not-started"),
    ]
    assert stage.sections[-1].started is None


@pytest.mark.parametrize(
    ("phases", "outcome"),
    [
        ((("setup", "passed"), ("call", "passed")), "open"),
        ((("setup", "passed"), ("call", "passed"), ("teardown", "failed")), "failed"),
        ((("setup", "skipped"), ("call", "failed"), ("teardown", "passed")), "failed"),
    ],
    ids=["open until teardown", "teardown failure", "failed beats skipped"],
)
def test_a_tests_outcome_is_open_until_its_teardown_then_the_worst_of_its_phases(phases, outcome):
    stage = target(
        *queued("t.py::a"),
        mark("test started t.py::a", 1, 2),
        reports=[rep("t.py::a", when, result) for when, result in phases],
    )
    assert stage.sections[0].outcome == outcome


@pytest.mark.parametrize("setup_outcome", ["passed", "failed"])
def test_a_setup_sections_outcome_is_the_following_tests_setup_report(setup_outcome):
    stage = target(
        *queued("t.py::a"),
        mark("setup fx started", 1, 2),
        mark("test started t.py::a", 1, 3),
        reports=[rep("t.py::a", "setup", setup_outcome)],
    )
    assert stage.sections[0].outcome == setup_outcome


def test_a_failed_test_carries_its_crash_line_and_the_last_traceback_entrys_lines():
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
    stage = target(
        *queued("t.py::bad", "t.py::good"),
        mark("test started t.py::bad", 1, 2),
        mark("test started t.py::good", 1, 3),
        reports=[
            rep("t.py::bad", "call", "failed", longrepr),
            rep("t.py::bad", "teardown", "passed"),
            rep("t.py::good", "call", "passed"),
            rep("t.py::good", "teardown", "passed"),
        ],
    )

    bad, good = stage.sections
    assert bad.failure == ("/w/t.py:12: assert 1 == 2", "    def f():", ">   assert 1 == 2")
    assert good.failure == ()


def test_crlf_and_invalid_utf8_are_read_without_raising(tmp_path):
    raw = (
        TEST_STARTED.replace("\n", "\r\n")
        + mark("stage build started", 1).replace("\n", "\r\n")
        + "out \xff line\r\n"
        + mark("stage build passed", 2).replace("\n", "\r\n")
    ).encode("latin-1")
    write_record(tmp_path, raw=raw)

    (run,) = Records(tmp_path).runs()

    assert (run.ref, stage_of(run, "build").state) == ("main", "passed")
    assert stage_of(run, "build").lines == ("out \ufffd line",)


def test_a_last_line_without_its_newline_is_held_until_the_newline_lands(tmp_path):
    head = log(TEST_STARTED, mark("stage build started", 1))
    run_dir = write_record(tmp_path, text=head + "half an output line")
    records = Records(tmp_path)

    def build():
        return stage_of(records.runs()[0], "build")

    def append(data):
        with open(run_dir / "bench.log", "ab") as f:
            f.write(data)

    assert build().lines == ()
    append(b"\n")
    assert build().lines == ("half an output line",)
    append(mark("stage build passed", 2).rstrip("\n").encode())
    assert build().state == "open"
    append(b"\n")
    assert build().state == "passed"


def test_runs_are_newest_first_by_stamp_then_numeric_suffix_and_need_a_bench_log(tmp_path):
    """`-10` sorts after `-2`; the bare stamp is suffix 1; folders without bench.log and plain
    files are not runs."""
    for name in (STAMP, f"{STAMP}-2", f"{STAMP}-10", "20261111T000000Z", "20261109T000000Z"):
        write_record(tmp_path, name, TEST_STARTED)
    (tmp_path / "20261112T000000Z" / "host_suite").mkdir(parents=True)
    (tmp_path / "DISABLED").write_text("x\n")

    assert [run.record for run in Records(tmp_path).runs()] == [
        "20261111T000000Z",
        f"{STAMP}-10",
        f"{STAMP}-2",
        STAMP,
        "20261109T000000Z",
    ]


def test_suite_reports_are_read_from_each_suites_report_jsonl_and_a_cut_last_line_is_ignored(
    tmp_path,
):
    text = suite_log(
        *queued("t.py::a", "t.py::b"),
        mark("test started t.py::a", 1, 2),
        mark("test started t.py::b", 1, 3),
    )
    run_dir = write_record(
        tmp_path,
        text=text,
        reports={
            "target_suite": [rep("t.py::a", "call", "passed"), rep("t.py::a", "teardown", "passed")]
        },
    )
    with open(run_dir / "target_suite" / "report.jsonl", "a") as f:
        f.write('{"$report_type": "TestReport", "nodeid": "t.py::b", "wh')

    (run,) = Records(tmp_path).runs()

    assert {s.name: s.outcome for s in stage_of(run, "target_suite").sections} == {
        "t.py::a": "passed",
        "t.py::b": "open",
    }


@pytest.mark.parametrize("damage", ["removed", "unreadable"])
def test_a_record_that_vanishes_or_cannot_be_read_drops_out_of_the_next_runs(tmp_path, damage):
    if damage == "unreadable" and ROOT:
        pytest.skip("root reads a mode-000 file")
    write_record(tmp_path, STAMP, TEST_STARTED)
    bad = write_record(tmp_path, "20261111T000000Z", TEST_STARTED)
    records = Records(tmp_path)
    assert len(records.runs()) == 2

    if damage == "removed":
        (bad / "bench.log").unlink()
        bad.rmdir()
    else:
        (bad / "bench.log").write_text(TEST_STARTED + "more\n")
        (bad / "bench.log").chmod(0)

    assert [r.record for r in records.runs()] == [STAMP]


def test_a_same_size_rewrite_with_the_old_mtime_is_not_reread_but_a_size_change_is(tmp_path):
    old = mark("run started kind=test ref=aaaa")
    new = mark("run started kind=test ref=bbbb")
    path = write_record(tmp_path, text=old) / "bench.log"
    before = path.stat()
    records = Records(tmp_path)
    assert [r.ref for r in records.runs()] == ["aaaa"]

    path.write_text(new)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert [r.ref for r in records.runs()] == ["aaaa"]

    path.write_text(new + mark("stage checkout started", 1))
    assert [r.ref for r in records.runs()] == ["bbbb"]


KAS = "2026-11-10 03:10:00 - INFO     - NOTE: "


def build(*lines, stage="build"):
    run = parse(log(TEST_STARTED, mark(f"stage {stage} started", 1), *(f"{x}\n" for x in lines)))
    return stage_of(run, stage).progress


def test_build_progress_reads_the_last_task_count_and_the_tasks_still_running():
    """Last `Running task N of M` anywhere on a line; tasks Started with no later
    Succeeded/Failed; kas-prefixed starts are host-local times, bare ones None; a recipe of
    three or more dash fields loses the last two."""
    progress = build(
        KAS + "Running task 5 of 10 (/layers/a.bb:do_fetch)",
        KAS + "recipe linux-yocto-6.6.1-r0: task do_compile: Started",
        KAS + "recipe zlib-1.3-r0: task do_compile: Started",
        "NOTE: recipe base-files: task do_install: Started",
        "NOTE: recipe zlib-1.3-r0: task do_compile: Succeeded",
        "NOTE: recipe gcc-13-r0: task do_compile: Started",
        "NOTE: recipe gcc-13-r0: task do_compile: Failed",
        "prefix NOTE: Running task 7 of 10 (/layers/b.bb:do_compile)",
    )

    assert (progress.done, progress.total) == (7, 10)
    assert {(t.recipe, t.task, t.started) for t in progress.tasks} == {
        ("linux-yocto", "do_compile", datetime(2026, 11, 10, 3, 10, 0).astimezone()),
        ("base-files", "do_install", None),
    }


def test_build_progress_is_none_without_a_running_task_line_or_outside_the_build_stage():
    assert build("NOTE: recipe x-1-r0: task do_x: Started") is None
    assert build("NOTE: Running task 1 of 2 (/l/a.bb:do_x)", stage="precondition") is None


def proc_tree(tmp_path, holders):
    """A fake /proc whose `locks` lists `holders` as (mode, pid, dev, inode) FLOCK lines."""
    proc = tmp_path / "proc"
    proc.mkdir(parents=True)
    lines = [
        f"{i}: FLOCK  ADVISORY  {mode} {pid} {dev}:{inode} 0 EOF"
        for i, (mode, pid, dev, inode) in enumerate(holders, 1)
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
    ("holders", "lock_exists", "result", "expected"),
    [
        ([("WRITE", "ff:ff", 0)], True, None, True),
        ([("WRITE", "00:2c", 1)], True, None, False),
        ([], True, None, False),
        ([("WRITE", "00:2c", 0)], False, None, False),
        ([("WRITE", "00:2c", 0)], True, "run error", False),
    ],
    ids=["holder (device ignored)", "other inode", "no holder", "no lock file", "result line"],
)
def test_a_run_is_live_only_with_no_result_line_and_a_write_flock_on_the_lock_inode(
    tmp_path, holders, lock_exists, result, expected
):
    directory, inode = lock_file(tmp_path)
    proc = proc_tree(tmp_path, [(mode, 4242, dev, inode + skew) for mode, dev, skew in holders])
    if not lock_exists:
        (directory / "lock").unlink()
    run = live_run(*([mark(result, 0, 9)] if result else []))

    assert bench_state.is_live(directory, run, proc) is expected


def test_is_live_agrees_with_the_real_lock_and_the_real_proc(tmp_path):
    """The fake /proc is backed: `Lock` takes a real flock and the real /proc/locks shows it."""
    if not Path("/proc/locks").exists():
        pytest.skip("no /proc/locks")
    with Lock(tmp_path):
        assert bench_state.is_live(tmp_path, live_run(), Path("/proc")) is True
    assert bench_state.is_live(tmp_path, live_run(), Path("/proc")) is False


def test_garbage_lines_in_proc_locks_are_skipped_before_a_valid_holder(tmp_path):
    directory, inode = lock_file(tmp_path)
    proc = proc_tree(tmp_path, [("WRITE", 4242, "00:2c", inode)])
    valid = (proc / "locks").read_text()
    (proc / "locks").write_text(
        "\n1: FLOCK ADVISORY\n2: FLOCK  ADVISORY  WRITE 9 00:2c:notanumber 0 EOF\n"
        "3: FLOCK  ADVISORY  WRITE 9 nocolon 0 EOF\n" + valid
    )

    assert bench_state.is_live(directory, live_run(), proc) is True


@pytest.mark.parametrize(
    ("returncode", "raises", "expected"),
    [
        (0, None, "on"),
        (3, None, "off"),
        (1, None, "—"),
        (None, FileNotFoundError("systemctl"), "—"),
        (None, subprocess.TimeoutExpired("systemctl", 1), "—"),
    ],
    ids=["active", "inactive", "failed", "no systemctl", "timeout"],
)
def test_the_timer_is_asked_with_is_active_and_a_one_second_timeout(
    monkeypatch, returncode, raises, expected
):
    calls = stub_systemctl(monkeypatch, returncode, raises)

    assert bench_state.timer_state("bench-run") == expected

    ((command, kwargs),) = calls
    assert command == ["systemctl", "--user", "is-active", "bench-run.timer"]
    assert kwargs["timeout"] == 1


def stub_systemctl(monkeypatch, returncode=None, raises=None):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(command, returncode, stdout="", stderr="")

    monkeypatch.setattr(bench_state.subprocess, "run", run)
    return calls


def test_read_run_unit_is_the_run_tables_unit_and_none_without_a_table_a_file_or_valid_toml(
    tmp_path,
):
    for name in ("with_unit", "no_run", "missing", "invalid"):
        (tmp_path / name).mkdir()
    write_bench_toml(tmp_path / "with_unit")
    with open(tmp_path / "with_unit" / "bench.toml", "a") as f:
        f.write('\n[run]\nunit = "bench-run"\n')
    write_bench_toml(tmp_path / "no_run")
    (tmp_path / "invalid" / "bench.toml").write_text("[run\nunit = ")

    units = {
        n: bench_state.read_run_unit(tmp_path / n)
        for n in ("with_unit", "no_run", "missing", "invalid")
    }
    assert units == {"with_unit": "bench-run", "no_run": None, "missing": None, "invalid": None}


def test_read_bench_combines_disabled_presence_liveness_and_timer(tmp_path, monkeypatch):
    """disabled = DISABLED exists (content unread); live = is_live of the newest run; timer =
    timer_state(run_unit), the dash for a None unit; no newest run is not live."""
    directory, inode = lock_file(tmp_path)
    proc = proc_tree(tmp_path, [("WRITE", 4242, "00:2c", inode)])
    calls = stub_systemctl(monkeypatch, returncode=0)
    newest = live_run()

    quiet = bench_state.read_bench(directory, newest, "bench-run", proc)
    assert quiet == bench_state.Bench(live=True, disabled=False, timer="on")

    (directory / "DISABLED").write_bytes(b"\xff\xfe not text")
    disabled = bench_state.read_bench(directory, newest, None, proc)
    assert disabled == bench_state.Bench(live=True, disabled=True, timer="—")
    assert len(calls) == 1

    first_launch = bench_state.read_bench(directory, None, "bench-run", proc)
    assert first_launch == bench_state.Bench(live=False, disabled=True, timer="on")


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
    write_record(
        records,
        text=suite_log(*queued("t.py::a"), mark("test started t.py::a", 1, 2)),
        reports={
            "target_suite": [rep("t.py::a", "call", "passed")],
            "host_suite": [rep("t.py::h", "call", "passed")],
        },
    )
    (records / "DISABLED").write_text("reason\n")
    (records / "lock").touch()
    proc = proc_tree(tmp_path, [("WRITE", 4242, "00:2c", (records / "lock").stat().st_ino)])
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
        runs = Records(records).runs()
        bench_state.is_live(records, runs[0], proc)
        bench_state.timer_state("bench-run")
        bench = bench_state.read_bench(records, runs[0], "bench-run", proc)
        screen.render(status.View(), runs, bench, 24, 80, STARTED)
    finally:
        for path in paths:
            path.chmod(0o755 if path.is_dir() else 0o644)

    assert forbidden == []
    assert snapshot(tmp_path) == before
    assert popen_argv == [["systemctl", "--user", "is-active", "bench-run.timer"]] * len(popen_argv)
    assert popen_argv


NAMES = ("checkout", "precondition", "build", "bundle", "host_suite", "target_suite", "posted")
CANDIDATE_SUITE = (
    "import pytest\n\n\n@pytest.fixture\ndef board(bench_candidate):\n    return bench_candidate\n"
    "\n\ndef test_one(board):\n    assert board\n"
)


def passing(path):
    write_suite(path, "host_suite")
    write_suite(path, "target_suite")


def failing_host(path):
    write_suite(path, "host_suite", passing=False)


def candidate_fixture(path):
    write_suite(path, "host_suite")
    (path / "target_suite").mkdir()
    (path / "target_suite" / "test_one.py").write_text(CANDIDATE_SUITE)


def states(*of_each):
    return dict(zip(NAMES, of_each, strict=True))


def one(stage, outcome="passed"):
    return [("test", f"{stage}/test_one.py::test_one", outcome)]


P, S, N = "passed", "skipped", "not-started"
REAL_RUNS = {
    "passing": (
        passing,
        {},
        states(P, P, P, P, P, P, P),
        {"host_suite": one("host_suite"), "target_suite": one("target_suite")},
        "passed",
        None,
    ),
    "skip-build": (
        lambda path: write_suite(path, "target_suite"),
        {"skip_build": True},
        states(P, P, S, S, S, P, P),
        {"target_suite": one("target_suite")},
        "passed",
        None,
    ),
    "failing suite": (
        failing_host,
        {},
        states(P, P, P, P, "failed", N, P),
        {"host_suite": one("host_suite", "failed")},
        "failed",
        bench_test.CandidateFailure,
    ),
    "fixture taking bench_candidate": (
        candidate_fixture,
        {},
        states(P, P, P, P, P, P, P),
        {
            "host_suite": one("host_suite"),
            "target_suite": [("setup", "board", P), *one("target_suite")],
        },
        "passed",
        None,
    ),
    "error": (
        lambda path: None,
        {"precondition": "false", "post": False},
        states(P, "error", N, N, N, N, N),
        {},
        "error",
        bench_test.BenchFailure,
    ),
}


@pytest.mark.parametrize(
    ("setup", "options", "expected", "sections", "result", "raises"),
    REAL_RUNS.values(),
    ids=REAL_RUNS,
)
def test_a_log_written_by_a_real_bench_test_run_parses_into_the_model(
    monkeypatch, tmp_path, setup, options, expected, sections, result, raises
):
    """The parser reads what `BenchTest.run` wrote: stage states, result, sha, pr, sections and
    the real report.jsonl outcome and failure lines, for each way a run can end."""
    monkeypatch.setattr(bench_test.subprocess, "run", functools.partial(subprocess.run, timeout=60))
    monkeypatch.chdir(tmp_path)
    repo_with_one_commit(tmp_path)
    write_bench_toml(tmp_path, precondition=options.get("precondition", "true"))
    setup(tmp_path)
    runner = bench_test.BenchTest(
        tmp_path, "HEAD", None, skip_build=options.get("skip_build", False), kind="run", pr=7
    )
    post = None if options.get("post") is False else (lambda failure: None)
    try:
        runner.run(write_lg_env(tmp_path), [], post=post)
        raised = None
    except Exception as exc:
        raised = exc

    assert type(raised) is (raises or type(None))
    (run,) = Records(tmp_path / "records").runs()
    assert (run.kind, run.ref, run.pr, run.result) == ("run", "HEAD", 7, result)
    assert {s.name: s.state for s in run.stages} == expected
    assert len(stage_of(run, "checkout").sha) == 40
    for name in ("host_suite", "target_suite"):
        found = stage_of(run, name).sections
        assert [(s.kind, s.name, s.outcome) for s in found] == sections.get(name, [])
        assert all(bool(s.failure) == (s.outcome == "failed") for s in found)


def test_a_committed_real_host_record_parses_into_the_model():
    """A scrubbed record of a real `bench test`: stage states, sha, build output left on the
    stage, the setup row and the bracketed absolute nodeid of the real target suite."""
    run = real_run()

    assert (run.kind, run.ref[:7], run.pr, run.result) == ("test", "39e00d7", None, "passed")
    assert run.ended - run.started == timedelta(minutes=10, seconds=13)
    assert [(s.name, s.state) for s in run.stages] == [
        (name, "passed")
        for name in ("checkout", "precondition", "build", "bundle", "host_suite", "target_suite")
    ]
    assert len(stage_of(run, "checkout").sha) == 40
    build = stage_of(run, "build")
    assert build.sections == ()
    assert any("Tasks Summary" in line for line in build.lines)
    assert [(s.kind, s.outcome) for s in stage_of(run, "host_suite").sections] == [
        ("test", "passed")
    ] * 2
    target = stage_of(run, "target_suite").sections
    assert [(s.kind, s.outcome) for s in target] == [("setup", "passed")] + [("test", "passed")] * 4
    assert target[0].name == "install_candidate"
    assert target[-1].name.endswith(
        "test_instance_reaches_out[/srv/bench/tests/replay/sets/cards4-live/flows.har]"
    )
