"""What the watch view shows, read back as the text of the running app's screen."""

import re

import pytest
from status_helpers import (
    DISABLED,
    IDLE,
    LIVE,
    NAMES,
    OLDER,
    OLDEST,
    STAMP,
    building,
    colours,
    failing_at,
    live,
    mark,
    real_run,
    run,
    running_test,
    stage,
    suite,
    t,
)

from wisebench import runlog, screen

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


def to_stage(name):
    """The keys that select the stage `name` in the stage list: `gg`, then `j` down to it."""
    return ["g", "g", *["j"] * NAMES.index(name)]


def spaced(text):
    """A pattern for `text` with each run of spaces in it matching any run of spaces."""
    return r" +".join(re.escape(word) for word in text.split())


def line_holding(text, needle):
    """The index of the first line of `text` holding `needle`."""
    return next(i for i, line in enumerate(text.split("\n")) if needle in line)


@pytest.mark.parametrize(("size", "small"), [((79, 24), True), ((80, 23), True), ((80, 24), False)])
def test_below_80_columns_or_24_rows_the_view_says_terminal_too_small(show, size, small):
    text = show([run()], IDLE, size=size)
    assert ("terminal too small" in text) is small
    assert ("1 Run" in text) is not small


def test_the_tabs_are_run_queue_and_history_and_run_names_a_past_runs_record(show):
    assert re.search(r"1 Run +2 Queue +3 History", show([run(), run(OLDER)], IDLE))
    assert re.search(rf"1 Run +{OLDER} +2 Queue", show([run(), run(OLDER)], IDLE, "["))


@pytest.mark.parametrize(
    ("bench", "badge", "timer"),
    [(LIVE, "RUNNING", "on"), (IDLE, "IDLE", "on"), (DISABLED, "DISABLED", "off")],
)
def test_the_status_bar_is_the_badge_and_the_timer_and_the_footer_offers_the_keys(
    show, bench, badge, timer
):
    text = show([run()], bench)
    assert re.search(rf"{badge} +timer {timer}\b", text)
    assert re.search(r"\? keys", text)


@pytest.mark.parametrize(
    ("runs", "bench", "keys", "header", "prefixed"),
    [
        ([live("build")], LIVE, [], rf"PR #31 +a1b2c3d +record {STAMP}", False),
        ([run()], IDLE, [], rf"last run +PR #31 +a1b2c3d +record {STAMP}", True),
        (
            [live("build"), failing_at(OLDER, "bundle")],
            LIVE,
            ["["],
            rf"PR #31 +a1b2c3d +record {OLDER} +failed +ended {lc(4, 2)}",
            False,
        ),
    ],
    ids=["live", "idle", "past"],
)
def test_the_run_header_names_the_run_and_its_state(show, runs, bench, keys, header, prefixed):
    text = show(runs, bench, *keys)
    assert re.search(header, text)
    assert ("last run" in text) is prefixed


def test_a_stage_row_is_glyph_name_start_and_took_and_a_stage_not_started_has_no_times(show):
    text = show([live("host_suite")], LIVE, now=t(3, 21, 5))
    assert re.search(r"stage +start +took", text)
    assert re.search(rf"✓ checkout +{lc(3)} +0m02s", text)
    assert re.search(rf"● host suite +{lc(3, 20)} +1m05s", text)
    assert re.search(r"· target suite(?! +\d)", text)


def test_a_suite_pane_is_its_counts_then_a_header_per_section_with_fold_glyphs(show):
    text = show([running_test(2)], LIVE, size=(120, 30))
    for pattern in (
        r"target suite +3 of 6 +2 passed +0 failed",
        r"▸ ✓ setup install_candidate",
        r"▸ ✓ t\.py::test_0 +1m00s",
        r"▾ ● t\.py::test_2",
        r"· t\.py::test_4",
    ):
        assert re.search(pattern, text), pattern
    assert "out 2" in text


def test_a_live_build_pane_is_its_progress_and_its_running_tasks(show):
    text = show([building(2)], LIVE, now=t(3, 15, 10))
    assert re.search(r"build +task 1204 of 3311 +36%", text)
    assert "running now" in text
    assert re.search(r"r0 +do_compile +4m10s", text)
    assert re.search(r"r1 +do_compile +4m09s", text)


def test_the_idle_summary_is_the_result_its_end_and_length_and_a_row_per_suite(show):
    text = show([FINISHED], IDLE)
    assert re.search(rf"passed +ended {lc(4, 2)}, took 62m", text)
    assert re.search(r"host suite +2 passed", text)
    assert re.search(r"target suite +6 passed", text)


def test_a_history_row_is_record_what_sha_result_took_and_the_trailing_column(show):
    text = show(HISTORY, LIVE, "3", size=(100, 24))
    for pattern in (
        r"record +what +sha +result +took",
        rf"{STAMP} +PR #31 +a1b2c3d +running ",
        rf"{OLDER} +PR #31 +a1b2c3d +failed +62m +bundle",
        rf"{OLDEST} +PR #31 +a1b2c3d +bench error +62m +precondition",
        rf"{HAND.record} +feature/x +a1b2c3d +passed +62m +by hand",
    ):
        assert re.search(pattern, text), pattern


def test_the_history_footer_counts_the_records_and_names_the_directory_from_where_it_runs(show):
    assert f"{len(HISTORY)} records in ../records/" in show(HISTORY, LIVE, "3")


def test_a_parsed_real_record_draws_its_stages_summary_and_target_suite(show):
    parsed = real_run()
    summary = show([parsed], IDLE)
    for name in ("checkout", "precondition", "build", "bundle", "host suite", "target suite"):
        assert re.search(rf"✓ {name} ", summary), name
    assert re.search(r"host suite +2 passed", summary)
    assert re.search(r"target suite +4 passed", summary)

    target = show([parsed], IDLE, *to_stage("target_suite"))
    assert re.search(r"target suite +4 of 4 +4 passed +0 failed", target)
    assert "✓ setup install_candidate" in target


def test_a_stage_left_open_in_a_dead_run_reads_a_question_mark_and_a_dash_for_took(show):
    states = ["passed", "passed", "open"] + ["not-started"] * 4
    assert re.search(rf"\? build +{lc(3, 10)} +—", show([run(states=states, result=None)], IDLE))


DIRTY = "x\x1b[0m\x07y\x9b31mz"
SOURCES = {
    "a log line": (
        [live("build", build=stage("build", "open", t(3, 10), lines=[DIRTY]))],
        [],
        {},
    ),
    "a merge-queue title": ([run()], ["2"], {"queue": [{**MERGE_ENTRY, "title": DIRTY}]}),
    "a message carrying an exception's text": ([run()], ["2"], {"queue": RuntimeError(DIRTY)}),
}


@pytest.mark.parametrize(("runs", "keys", "read"), SOURCES.values(), ids=SOURCES)
def test_no_esc_bel_or_8_bit_csi_from_the_text_shown_reaches_the_terminal(show, runs, keys, read):
    text = show(runs, LIVE, *keys, size=(140, 24), **read)
    assert re.search(r"x.*0m.*y.*31mz", text)
    assert not {"\x1b", "\x07", "\x9b"} & set(text)


def test_a_failed_tests_crash_line_and_traceback_lines_are_drawn_under_its_output(show):
    failed = suite(
        "host_suite",
        ["passed", "failed"],
        {1: ("out line",)},
        {1: ("t.py:88: AssertionError", "E   boom")},
    )
    text = show([run(host_suite=failed)], IDLE, *to_stage("host_suite"))
    assert (
        line_holding(text, "out line")
        < line_holding(text, "t.py:88: AssertionError")
        < line_holding(text, "E   boom")
    )


def test_a_failed_test_with_only_failure_lines_still_shows_its_fold_glyph(show):
    failed = suite("host_suite", ["failed"], failures={0: ("t.py:1: AssertionError",)})
    assert "▾ ✗ case_00" in show([run(host_suite=failed)], IDLE, *to_stage("host_suite"))


def test_a_past_build_labels_its_output(show):
    tasks = tuple(runlog.Task(f"r{i}", "do_compile", t(3, 11, i)) for i in range(3))
    progress = runlog.BuildProgress(1204, 3311, tasks, phase="task")
    lines = [f"NOTE: line {i}" for i in range(80)]
    done = stage("build", "passed", t(3, 10), t(3, 40), lines=lines, progress=progress)
    assert "this stage's output" in show([run(build=done)], IDLE, *to_stage("build"))


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
def test_a_builds_head_is_its_phase_text_until_the_first_count(show, lines, phase):
    text = show([parsed_build(*lines)], LIVE)
    assert re.search(r"build +" + spaced(phase) + r"(?!\S)", text)


def test_the_queue_draws_its_rows_and_the_preview(show):
    text = show(
        [live("build")],
        LIVE,
        *":run feature-c",
        queue=[HAND_ENTRY],
        preview=CLEAN,
        size=(100, 24),
    )
    assert re.search(r"▶.*running, build", text)
    assert re.search(r"by hand +feature-a", text)
    for line in (
        "will queue",
        'resolves to 1234567 "Fix the thing", clean tree',
        "joins as 3, after feature-a",
    ):
        assert line in text, line


def test_a_queue_with_nothing_in_it_draws_no_rows(show):
    text = show([run()], IDLE, "2", queue=[])
    assert "by hand" not in text and "merge queue" not in text


def test_the_run_prompt_shows_the_hints_enter_queue_it_and_esc_cancel(show):
    text = show([run()], IDLE, *":run feature-c", queue=[])
    assert ":run feature-c" in text
    assert "Enter queue it  Esc cancel" in text


@pytest.mark.parametrize("keys", [[], ["2"], ["3"]], ids=["run", "queue", "history"])
def test_a_message_is_drawn_on_every_tab(show, keys):
    text = show([run()], IDLE, *":disable", "enter", *keys)
    assert "no [run] unit in the bench's settings file" in text


def test_help_lists_the_keys_under_three_headings(show):
    text = show([run()], IDLE, "?", size=(100, 24))
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


@pytest.mark.parametrize(("keys", "pending"), [("2d", "d"), ("g", "g"), ("3z", "z")])
def test_a_pending_key_is_drawn_in_the_status_bar(show, keys, pending):
    assert re.search(rf"timer \w+ +{pending}\b", show([run()], IDLE, *keys))


def test_no_pending_text_is_drawn_when_no_key_waits(show):
    assert re.search(r"timer on *\n", show([run()], IDLE, "2"))


PALETTE = {
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
    "a suite, its log focused": ([running_test(2, failed=(1,))], ["enter"], (100, 30)),
    "a live build": ([building(3)], [], (100, 30)),
    "the Queue with a preview": ([live("build")], [*":run feature-c"], (100, 30)),
    "History": (HISTORY, ["3"], (100, 30)),
    "the command line open": ([run()], [":"], (100, 30)),
    "Help": ([run()], ["?"], (100, 30)),
    ":enable's question": ([run()], [*":enable", "enter"], (100, 30)),
    "terminal too small": ([run()], [], (60, 20)),
}


@pytest.mark.parametrize(("runs", "keys", "size"), SCREENS.values(), ids=SCREENS)
def test_every_colour_drawn_is_one_of_the_palettes(show, runs, keys, size):
    drawn = show(
        runs, LIVE, *keys, queue=[HAND_ENTRY, MERGE_ENTRY], preview=CLEAN, size=size, look=colours
    )
    assert drawn <= PALETTE
