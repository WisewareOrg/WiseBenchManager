"""`screen.Screen`: what a resolved frame shows, read back as the text of the rendered widget."""

import re

import pytest
from status_helpers import (
    DIRECTORY,
    DISABLED,
    IDLE,
    LIVE,
    NOW,
    OLDER,
    OLDEST,
    STAMP,
    building,
    failing_at,
    live,
    mark,
    real_run,
    run,
    running_test,
    shown,
    stage,
    suite,
    t,
)
from urwid.testing import render_runs, resolve_runs

from wisebench import runlog, screen, view

pytestmark = pytest.mark.usefixtures("local_zone")

HAND = run(record="20261107T030715Z", kind="test", ref="feature/x", pr=None)
ERRORED = run(OLDEST, states=["passed", "error"] + ["not-started"] * 5, result="error")
HISTORY = [live("build"), failing_at(OLDER, "bundle"), ERRORED, HAND]
FINISHED = run(
    host_suite=suite("host_suite", ["passed"] * 2),
    target_suite=suite("target_suite", ["passed"] * 6),
)
HAND_ENTRY = {
    "kind": "test",
    "id": 3,
    "added": "2026-11-10T03:15:00+00:00",
    "ref": "feature-a",
    "sha": "1" * 40,
    "set": None,
    "skip_build": False,
    "args": [],
}
MERGE_ENTRY = {"kind": "run", "pr": 14, "title": "Fix slot", "sha": "9f8e7d6" + "0" * 33}
CLEAN = {"sha": "1234567" + "0" * 33, "subject": "Fix the thing", "clean": True}


def lc(hour, minute=0, second=0):
    """The `HH:MM:SS` a UTC time reads as in the pinned zone, three hours ahead of UTC."""
    return f"{(hour + 3) % 24:02d}:{minute:02d}:{second:02d}"


def frame(runs, bench=IDLE, now=NOW, queue=None, preview=None, **state):
    """`runs` resolved through a State built from `state`."""
    snap = view.Snapshot(runs, bench, now, DIRECTORY, queue, preview)
    return view.resolve(view.State(**state), snap)


def spaced(text):
    """A pattern for `text` with each run of spaces in it matching any run of spaces."""
    return r" +".join(re.escape(word) for word in text.split())


def lines_of(text):
    """The rendered text as a list of lines."""
    return text.split("\n")


def line_holding(text, needle):
    """The index of the first rendered line holding `needle`."""
    return next(i for i, line in enumerate(lines_of(text)) if needle in line)


@pytest.mark.parametrize(
    ("cols", "rows", "small"), [(79, 24, True), (80, 23, True), (80, 24, False)]
)
def test_below_80_columns_or_24_rows_the_view_says_terminal_too_small(cols, rows, small):
    text = shown(frame([run()]), cols, rows)
    assert ("terminal too small" in text) is small
    assert ("1 Run" in text) is not small


def test_the_tab_line_names_the_tabs_and_a_past_runs_record_and_the_status_bar_the_clock():
    newest = shown(frame([run(), run(OLDER)]))
    assert re.search(r"1 Run +2 Queue +3 History", newest)
    assert re.search(rf"{lc(10, 7, 30)} +\? keys", newest)
    past = shown(frame([run(), run(OLDER)], record=OLDER))
    assert re.search(rf"1 Run +{OLDER}", past)


@pytest.mark.parametrize(
    ("bench", "badge", "timer"),
    [(LIVE, "RUNNING", "on"), (IDLE, "IDLE", "on"), (DISABLED, "DISABLED", "off")],
)
def test_the_status_bar_is_the_badge_the_timer_and_the_keys_box(bench, badge, timer):
    assert re.search(rf"{badge} +timer {timer}\b.*\? keys", shown(frame([run()], bench)))


@pytest.mark.parametrize(
    ("runs", "bench", "state", "header", "prefixed"),
    [
        ([live("build")], LIVE, {}, rf"PR #31 +a1b2c3d +record {STAMP}", False),
        ([run()], IDLE, {}, rf"last run +PR #31 +a1b2c3d +record {STAMP}", True),
        (
            [live("build"), failing_at(OLDER, "bundle")],
            LIVE,
            {"record": OLDER},
            rf"PR #31 +a1b2c3d +record {OLDER} +failed +ended {lc(4, 2)}",
            False,
        ),
    ],
    ids=["live", "idle", "past"],
)
def test_the_run_header_names_the_run_and_its_state(runs, bench, state, header, prefixed):
    text = shown(frame(runs, bench, **state))
    assert re.search(header, text)
    assert ("last run" in text) is prefixed


def test_a_stage_row_is_glyph_name_start_and_took_and_a_stage_not_started_has_no_times():
    text = shown(frame([live("host_suite")], LIVE, now=t(3, 21, 5)))
    assert re.search(r"stage +start +took", text)
    assert re.search(rf"✓ checkout +{lc(3)} +0m02s", text)
    assert re.search(rf"● host suite +{lc(3, 20)} +1m05s", text)
    assert re.search(r"· target suite(?! +\d)", text)


def test_a_suite_pane_is_its_counts_then_a_header_per_section_with_fold_glyphs():
    text = shown(frame([running_test(2)], LIVE))
    for pattern in (
        r"target suite +3 of 6 +2 passed +0 failed",
        r"▸ ✓ setup install_candidate",
        r"▸ ✓ t\.py::test_0 +1m00s",
        r"▾ ● t\.py::test_2",
        r"· t\.py::test_4",
    ):
        assert re.search(pattern, text), pattern
    assert "out 2" in text


def test_a_live_build_pane_is_its_progress_and_its_running_tasks():
    text = shown(frame([building(2)], LIVE, now=t(3, 15, 10)))
    assert re.search(r"build +task 1204 of 3311 +36%", text)
    assert "running now" in text
    assert re.search(r"r0 +do_compile +4m10s", text)
    assert re.search(r"r1 +do_compile +4m09s", text)


def test_on_a_short_terminal_many_running_tasks_still_leave_three_log_lines():
    tasks = tuple(runlog.Task(f"r{i:02d}", "do_compile", t(3, 11, i)) for i in range(40))
    progress = runlog.BuildProgress(1204, 3311, tasks, phase="task")
    lines = [f"NOTE: output line {i}" for i in range(6)]
    build = stage("build", "open", t(3, 10), lines=lines, progress=progress)
    text = shown(frame([live("build", build=build)], LIVE))
    assert sum(line in text for line in lines) >= 3


def test_the_idle_summary_is_the_result_its_end_and_length_and_a_row_per_suite():
    text = shown(frame([FINISHED], IDLE))
    assert re.search(rf"passed +ended {lc(4, 2)}, took 62m", text)
    assert re.search(r"host suite +2 passed", text)
    assert re.search(r"target suite +6 passed", text)


def test_a_history_row_is_record_what_sha_result_took_and_the_trailing_column():
    text = shown(frame(HISTORY, LIVE, tab="history"))
    for pattern in (
        r"record +what +sha +result +took",
        rf"{STAMP} +PR #31 +a1b2c3d +running ",
        rf"{OLDER} +PR #31 +a1b2c3d +failed +62m +bundle",
        rf"{OLDEST} +PR #31 +a1b2c3d +bench error +62m +precondition",
        rf"{HAND.record} +feature/x +a1b2c3d +passed +62m +by hand",
    ):
        assert re.search(pattern, text), pattern


def test_the_history_footer_counts_the_records_and_names_the_directory():
    text = shown(frame(HISTORY, LIVE, tab="history"))
    assert f"{len(HISTORY)} records in {DIRECTORY}/" in text


def test_a_parsed_real_record_draws_its_stages_summary_and_target_suite():
    parsed = real_run()
    summary = shown(frame([parsed], IDLE))
    for name in ("checkout", "precondition", "build", "bundle", "host suite", "target suite"):
        assert re.search(rf"✓ {name} ", summary), name
    assert re.search(r"host suite +2 passed", summary)
    assert re.search(r"target suite +4 passed", summary)

    target = shown(frame([parsed], IDLE, stage="target_suite"))
    assert re.search(r"target suite +4 of 4 +4 passed +0 failed", target)
    assert "✓ setup install_candidate" in target


def test_a_stage_left_open_in_a_dead_run_reads_a_question_mark_and_a_dash_for_took():
    states = ["passed", "passed", "open"] + ["not-started"] * 4
    text = shown(frame([run(states=states, result=None)], IDLE))
    assert re.search(rf"\? build +{lc(3, 10)} +—", text)


DIRTY = "x\x1b[0m\x07y\x9b31mz▶é"
SOURCES = {
    "a log line": lambda: frame(
        [live("build", build=stage("build", "open", t(3, 10), lines=[DIRTY]))], LIVE
    ),
    "a merge-queue title": lambda: frame(
        [run()], IDLE, tab="queue", queue=[{**MERGE_ENTRY, "title": DIRTY}]
    ),
    "a message carrying an exception's text": lambda: frame([run()], IDLE, message=DIRTY),
}


@pytest.mark.parametrize("source", SOURCES.values(), ids=SOURCES)
def test_no_esc_bel_or_csi_from_drawn_text_reaches_the_screen(source):
    text = shown(source(), cols=120)
    assert "z▶é" in text
    assert not {"\x1b", "\x07", "\x9b"} & set(text)


def test_a_failed_tests_crash_line_and_traceback_lines_are_drawn_under_its_output():
    failed = suite(
        "host_suite",
        ["passed", "failed"],
        {1: ("out line",)},
        {1: ("t.py:88: AssertionError", "E   boom")},
    )
    text = shown(frame([run(host_suite=failed)], IDLE, stage="host_suite"))
    output = line_holding(text, "out line")
    crash = line_holding(text, "t.py:88: AssertionError")
    boom = line_holding(text, "E   boom")
    assert output < crash < boom


def test_a_failed_test_with_only_failure_lines_still_shows_its_fold_glyph():
    failed = suite("host_suite", ["failed"], failures={0: ("t.py:1: AssertionError",)})
    assert "▾ ✗ case_00" in shown(frame([run(host_suite=failed)], IDLE, stage="host_suite"))


def past_build(lines=2):
    """A run whose build passed, with bitbake progress and `lines` output lines."""
    tasks = tuple(runlog.Task(f"r{i}", "do_compile", t(3, 11, i)) for i in range(3))
    progress = runlog.BuildProgress(1204, 3311, tasks, phase="task")
    output = [f"NOTE: line {i}" for i in range(lines)]
    done = stage("build", "passed", t(3, 10), t(3, 40), lines=output, progress=progress)
    return run(build=done)


def test_a_past_build_labels_its_output():
    assert "this stage's output" in shown(frame([past_build(lines=80)], IDLE, stage="build"))


def parsed_build(*lines):
    """A live run parsed from a log whose build stage has started and printed `lines`."""
    text = mark("run started kind=test ref=main") + mark("stage build started", 0, 1)
    return runlog.parse_run(STAMP, text + "".join(f"{x}\n" for x in lines), {})


KAS_INFO = "2026-11-10 03:00:02 - INFO     - NOTE: "
COUNTED = "NOTE: Running setscene task 1000 of 3300 (/l/a.bb:do_fetch_setscene)"
HEADS = {
    "nothing yet": (["NOTE: Executing"], "starting"),
    "cache loaded": (["Loading cache...done."], "parsing recipes"),
    "cache loaded, kas prefix": ([KAS_INFO + "Loading cache...done."], "parsing recipes"),
    "recipes parsed": (["Loading cache...done.", "Parsing recipes...done."], "initialising tasks"),
    "the last phase line wins": (
        ["Parsing recipes...done.", "Loading cache...done."],
        "parsing recipes",
    ),
    "a setscene count replaces the phase": (
        ["Parsing recipes...done.", COUNTED],
        "setscene 1000 of 3300  30%",
    ),
    "a phase line after a count leaves the counts": (
        ["NOTE: Running task 5 of 10 (/l/b.bb:do_compile)", "Parsing recipes...done."],
        "task 5 of 10  50%",
    ),
    "a task count replaces the phase": (
        ["Loading cache...done.", "NOTE: Running task 5 of 10 (/l/b.bb:do_compile)"],
        "task 5 of 10  50%",
    ),
}


@pytest.mark.parametrize(("lines", "phase"), HEADS.values(), ids=HEADS)
def test_a_builds_head_is_its_phase_text_until_the_first_count(lines, phase):
    text = shown(frame([parsed_build(*lines)], LIVE))
    assert re.search(r"build +" + spaced(phase) + r"(?!\S)", text)


def test_the_queue_draws_its_rows_and_the_preview():
    text = shown(
        frame(
            [live("build")],
            LIVE,
            queue=[HAND_ENTRY],
            preview=("feature-c", CLEAN),
            tab="queue",
            prompt=":run feature-c",
        )
    )
    assert re.search(r"▶.*running, build", text)
    assert re.search(r"by hand +feature-a", text)
    for line in (
        "will queue",
        'resolves to 1234567 "Fix the thing", clean tree',
        "joins as 3, after feature-a",
    ):
        assert line in text, line


@pytest.mark.parametrize("queue", [None, []], ids=["unread", "empty"])
def test_a_queue_with_nothing_in_it_draws_no_rows_and_no_footer(queue):
    text = shown(frame([run()], queue=queue, tab="queue"))
    assert "by hand" not in text and "merge queue" not in text
    assert "updated" not in text and " ago" not in text


def test_no_preview_box_without_a_result_for_the_typed_ref():
    typed = frame([run()], queue=[], preview=("other", CLEAN), tab="queue", prompt=":run feature-c")
    assert "will queue" not in shown(typed)


def test_the_run_prompt_shows_the_hints_enter_queue_it_and_esc_cancel_and_no_tab_complete():
    text = shown(frame([run()], queue=[], tab="queue", prompt=":run feature-c"))
    assert ":run feature-c" in text
    assert "Enter queue it  Esc cancel" in text
    assert "Tab complete" not in text


@pytest.mark.parametrize("tab", ["run", "queue", "history", "help"])
def test_a_message_is_drawn_on_every_tab(tab):
    assert "queued 5 1234567" in shown(frame([run()], tab=tab, message="queued 5 1234567"))


def test_help_lists_the_keys_under_three_headings_and_no_longer_h_l():
    text = shown(frame([run()], tab="help"))
    for heading in ("move", "runs and log", "commands"):
        assert heading in text
    for pattern in (
        r"Tab S-Tab +next / prev view",
        r"1-3 +jump to view",
        r"\[ \] +older / newer run",
        r"dd +remove hand-queued run",
        r":run ref +queue a ref",
        r":disable +stop bench",
        r":enable +start again",
    ):
        assert re.search(pattern, text), pattern
    assert "prev / next view" not in text


@pytest.mark.parametrize(
    ("tab", "pending"), [("queue", "d"), ("run", "g"), ("history", "z")], ids=["d", "g", "z"]
)
def test_a_pending_key_is_drawn_in_the_status_bar_before_the_keys_box(tab, pending):
    text = shown(frame([run()], tab=tab, pending=pending))
    assert re.search(rf"timer \w+ +{pending} +{lc(10, 7, 30)} +\? keys", text)


def test_no_pending_text_is_drawn_when_no_key_waits():
    assert re.search(rf"timer \w+ +{lc(10, 7, 30)} +\? keys", shown(frame([run()], tab="queue")))


CANVAS = {
    screen.GROUND,
    screen.TEXT,
    screen.DIM,
    screen.LOG,
    screen.RULE,
    screen.BRIGHT,
    screen.GREEN,
    screen.AMBER,
    screen.RED,
    screen.BLUE,
    screen.RUNNING_TINT,
    screen.FAILED_TINT,
    screen.SELECTION,
}
SCREENS = {
    "a live build": lambda: frame([building(3)], LIVE),
    "a suite's log in focus": lambda: frame([running_test(2)], LIVE, focus="log"),
    "a failed stage": lambda: frame([failing_at(OLDER, "bundle")], IDLE),
    "the queue, a preview and the command line": lambda: frame(
        [live("build")],
        LIVE,
        queue=[HAND_ENTRY, MERGE_ENTRY],
        preview=("feature-c", CLEAN),
        tab="queue",
        prompt=":run feature-c",
    ),
    "history and a message": lambda: frame(HISTORY, DISABLED, tab="history", message="hi"),
    "help": lambda: frame([run()], tab="help"),
}


def colours(attr):
    """The foreground and background a screen draws `attr` with, as `#rrggbb` or `default`."""
    rgb = attr.get_rgb_values()
    return {
        "default" if None in part else "#{:02x}{:02x}{:02x}".format(*part)
        for part in (rgb[:3], rgb[3:])
    }


@pytest.mark.parametrize("size", [(80, 24), (79, 24)], ids=["80x24", "too small"])
@pytest.mark.parametrize("make", SCREENS.values(), ids=SCREENS)
def test_every_cell_is_drawn_in_the_canvas_colours(make, size):
    drawn = screen.Screen(view.State())
    drawn.update(make())
    runs = resolve_runs(render_runs(drawn, size), [], colors=2**24)
    assert set().union(*(colours(attr) for row in runs for attr, _ in row)) <= CANVAS
