"""`screen.draw`: a resolved frame drawn onto a surface in the canvas's layout and palette."""

import re

import pytest
from status_helpers import (
    DIRECTORY,
    DISABLED,
    IDLE,
    LIVE,
    LONG,
    MANY,
    NOW,
    OLDER,
    OLDEST,
    SIZE,
    STAMP,
    Grid,
    building,
    failing_at,
    live,
    real_run,
    run,
    running_test,
    stage,
    suite,
    t,
)

from wisebench import runlog, screen, status, view

pytestmark = pytest.mark.usefixtures("local_zone")

HAND = run(record="20261107T030715Z", kind="test", ref="feature/x", pr=None)
ERRORED = run(OLDEST, states=["passed", "error"] + ["not-started"] * 5, result="error")
HISTORY = [live("build"), failing_at(OLDER, "bundle"), ERRORED, HAND]
FINISHED = run(
    host_suite=suite("host_suite", ["passed"] * 2),
    target_suite=suite("target_suite", ["passed"] * 6),
)


def lc(hour, minute=0, second=0):
    """The `HH:MM:SS` a UTC time reads as in the pinned zone, three hours ahead of UTC."""
    return f"{(hour + 3) % 24:02d}:{minute:02d}:{second:02d}"


def drawn(runs, bench=IDLE, now=NOW, **state):
    """`runs` resolved through a State built from `state` and drawn on a Grid: (grid, frame)."""
    frame = view.resolve(view.State(**state), view.Snapshot(runs, bench, now, DIRECTORY), *SIZE)
    grid = Grid(*SIZE)
    screen.draw(grid, frame)
    return grid, frame


def stage_row(glyph, name, start="", took=""):
    """A stage row as the canvas columns it: glyph and name in 17, start in 10, then took."""
    return f"{glyph} {name}".ljust(17) + start.ljust(10) + took


def test_the_256_colour_fallback_is_the_nearest_xterm_index_of_each_canvas_colour():
    assert status._XTERM == {
        screen.GROUND: 232,
        screen.TEXT: 252,
        screen.DIM: 245,
        screen.LOG: 248,
        screen.RULE: 237,
        screen.BRIGHT: 231,
        screen.GREEN: 108,
        screen.AMBER: 180,
        screen.RED: 168,
        screen.BLUE: 75,
        screen.RUNNING_TINT: 235,
        screen.FAILED_TINT: 235,
        screen.SELECTION: 235,
    }


def stage_grounds(grid):
    """The ground of each stage-list row's first cell, by the row's text."""
    return {grid.text(y)[:17].strip(): grid.cells[y][0][2] for y in range(2, SIZE[0] - 1)}


def test_only_the_selected_stage_row_is_grounded_and_the_summary_grounds_none():
    failed, _ = drawn([failing_at(STAMP, "bundle")], DISABLED, stage="checkout")
    grounds = stage_grounds(failed)
    assert grounds["✓ checkout"] == screen.SELECTION
    assert grounds["✗ bundle"] is None
    assert failed.span(r"✗ bundle +\S+ +\S+")[0][1] == screen.RED

    running, _ = drawn([building(3)], LIVE, stage="checkout")
    assert stage_grounds(running)["● build"] is None
    assert running.span(r"● build +\S+ +\S+")[0][1] == screen.AMBER

    summary, frame = drawn([failing_at(STAMP, "bundle")], IDLE)
    assert frame.stage is None
    assert {summary.cells[y][0][2] for y in range(2, SIZE[0] - 1)} == {None}


SCENES = {
    "live build": lambda: drawn([building(3)], LIVE),
    "live suite": lambda: drawn([running_test(3, failed=(1,))], LIVE),
    "disabled": lambda: drawn([failing_at(STAMP, "bundle")], DISABLED),
    "history": lambda: drawn(HISTORY, LIVE, tab="history"),
    "idle": lambda: drawn([FINISHED], IDLE),
    "selected stage": lambda: drawn([FINISHED], IDLE, stage="checkout"),
}

COLOURS = {
    "a passed stage is green": ("idle", r"✓ checkout +\S+ +\S+", "GREEN", None, None),
    "the selected running stage is amber bold on the running tint": (
        "live build",
        r"● build +\S+ +\S+",
        "AMBER",
        "RUNNING_TINT",
        True,
    ),
    "the selected failed stage is red bold on the failed tint": (
        "disabled",
        r"✗ bundle +\S+ +\S+",
        "RED",
        "FAILED_TINT",
        True,
    ),
    "a stage not started is dim": ("live build", r"· bundle", "DIM", None, None),
    "the word setup is blue": ("live suite", r"(setup) install_candidate", "BLUE", None, None),
    "by hand is blue": ("history", r"by hand", "BLUE", None, None),
    "the selected stage row is on the selection": (
        "selected stage",
        r"✓ checkout +\S+ +\S+",
        "GREEN",
        "SELECTION",
        None,
    ),
}


@pytest.mark.parametrize(("scene", "pattern", "fg", "bg", "bold"), COLOURS.values(), ids=COLOURS)
def test_each_meaning_is_drawn_in_its_canvas_colour(scene, pattern, fg, bg, bold):
    grid, _ = SCENES[scene]()
    cells = grid.span(pattern)
    inked = [cell for cell in cells if cell[0] != " "]
    assert {cell[1] for cell in inked} == {getattr(screen, fg)}
    if bg is not None:
        assert {cell[2] for cell in cells} == {getattr(screen, bg)}
    if bold is not None:
        assert {cell[3] for cell in inked} == {bold}


def test_the_tab_line_is_the_tabs_a_past_runs_record_and_the_local_clock():
    newest, _ = drawn([run(), run(OLDER)])
    assert re.search(r"1 Run +3 History", newest.text(0))
    assert newest.text(0).rstrip().endswith(lc(10, 7, 30))

    past, _ = drawn([run(), run(OLDER)], record=OLDER)
    past.find(rf"1 Run  {OLDER}")

    on_help, _ = drawn([run()], tab="help")
    assert screen.TEXT not in {cell[2] for cell in on_help.cells[0]}


@pytest.mark.parametrize(
    ("bench", "badge", "timer"),
    [(LIVE, "RUNNING", "on"), (IDLE, "IDLE", "on"), (DISABLED, "DISABLED", "off")],
)
def test_the_status_bar_is_the_badge_the_timer_and_keys_on_the_last_row(bench, badge, timer):
    grid, _ = drawn([run()], bench)
    bar = grid.text(SIZE[0] - 1)
    assert re.match(rf" ?{badge} +timer {timer}\b", bar)
    assert bar.rstrip().endswith("? keys")


def test_an_open_prompt_takes_the_last_row_under_the_status_bar():
    grid, _ = drawn([run()], prompt=":q")
    assert grid.text(SIZE[0] - 1).startswith(":q")
    assert "IDLE" in grid.text(SIZE[0] - 2)


@pytest.mark.parametrize(
    ("runs", "bench", "state", "header"),
    [
        ([live("build")], LIVE, {}, rf"^PR #31  a1b2c3d  record {STAMP}"),
        ([run()], IDLE, {}, rf"^last run  PR #31  a1b2c3d  record {STAMP}"),
        (
            [live("build"), failing_at(OLDER, "bundle")],
            LIVE,
            {"record": OLDER},
            rf"^PR #31  a1b2c3d  record {OLDER}  failed  ended {lc(4, 2)}",
        ),
    ],
    ids=["live", "idle", "past"],
)
def test_the_run_header_names_the_run_and_its_state(runs, bench, state, header):
    grid, _ = drawn(runs, bench, **state)
    assert re.search(header, grid.text(1))


def test_a_stage_row_is_glyph_name_start_and_took_under_the_column_header():
    grid, _ = drawn([live("host_suite")], LIVE, now=t(3, 21, 5))
    grid.find(r"^stage {12}start {5}took")
    grid.find("^" + re.escape(stage_row("✓", "checkout", lc(3), "0m02s")))
    grid.find("^" + re.escape(stage_row("●", "host suite", lc(3, 20), "1m05s")))
    grid.find(r"^· target suite {20}")


def test_a_suite_pane_is_its_counts_then_a_header_per_section_with_fold_glyphs():
    grid, _ = drawn([running_test(2)], LIVE)
    grid.find(r"target suite  3 of 6  2 passed  0 failed")
    grid.find(r"▸ ✓ setup install_candidate")
    _, passed_at, _ = grid.find(r"▸ ✓ t\.py::test_0  1m00s")
    running, running_at, _ = grid.find(r"▾ ● t\.py::test_2")
    assert grid.text(running + 1)[running_at:].startswith(" " * 6 + "out 2")
    _, waiting_at, _ = grid.find(r"· t\.py::test_4")
    assert passed_at == running_at == waiting_at - 2


def test_a_live_build_pane_is_its_progress_bar_running_tasks_and_bottom_aligned_output():
    grid, frame = drawn([building(2)], LIVE, now=t(3, 15, 10))
    grid.find(r"build  task 1204 of 3311  36%")
    bar = grid.span(r"█+")
    assert len(bar) == 33
    assert {cell[1] for cell in bar[:12]} == {screen.AMBER}
    assert {cell[1] for cell in bar[12:]} == {screen.RULE}
    grid.find("running now")
    grid.find(re.escape(f"{'r0':<14}{'do_compile':<14}4m10s"))
    label, _, _ = grid.find("this stage's output")
    line, _, _ = grid.find("NOTE: Running task")
    assert line == label + 1 == frame.layout.pane_bottom - 1


def test_the_idle_summary_is_the_result_its_end_and_length_and_a_row_per_suite():
    grid, _ = drawn([FINISHED], IDLE)
    grid.find(rf"passed  ended {lc(4, 2)}, took 62m")
    grid.find(r"host suite    2 passed")
    grid.find(r"target suite  6 passed")


def test_the_log_rows_drawn_are_the_frames_window_and_only_the_cursor_row_is_selected():
    anchor = (
        view.resolve(
            view.State(stage="host_suite"), view.Snapshot([LONG], IDLE, NOW, DIRECTORY), *SIZE
        )
        .rows[25]
        .anchor
    )
    grid, frame = drawn([LONG], stage="host_suite", focus="log", cursor=anchor)
    assert frame.scroll > 0
    top = frame.layout.pane_top + frame.head_height
    for offset, row in enumerate(frame.rows[frame.scroll : frame.scroll + frame.room]):
        assert row.text in grid.text(top + offset)
    cursor_row = top + frame.cursor - frame.scroll
    column = grid.text(cursor_row).index(frame.rows[frame.cursor].text)
    selected = [y for y in range(SIZE[0]) if grid.cells[y][column][2] == screen.SELECTION]
    assert selected == [cursor_row]


def test_the_log_cursor_row_is_selected_only_while_the_log_has_focus():
    anchor = (
        view.resolve(
            view.State(stage="host_suite"), view.Snapshot([LONG], IDLE, NOW, DIRECTORY), *SIZE
        )
        .rows[25]
        .anchor
    )
    def log_grounds(grid):
        """Every ground drawn in the log pane's columns."""
        return {cell[2] for row in grid.cells[2:-1] for cell in row[screen._PANE :]}

    stages, frame = drawn([LONG], stage="host_suite", focus="stages", cursor=anchor)
    assert screen.SELECTION not in log_grounds(stages)
    assert frame.focus == "stages"
    log, frame = drawn([LONG], stage="host_suite", focus="log", cursor=anchor)
    assert screen.SELECTION in log_grounds(log)


def test_the_history_rows_drawn_are_the_frames_window_and_the_selected_one_is_selected():
    grid, frame = drawn(MANY, tab="history", history_record=MANY[30].record)
    assert frame.history_scroll > 0
    shown = [
        r.record for r in MANY if any(grid.text(y).startswith(r.record) for y in range(SIZE[0]))
    ]
    window = MANY[frame.history_scroll : frame.history_scroll + frame.history_room]
    assert shown == [r.record for r in window]
    selected = [y for y in range(SIZE[0]) if grid.cells[y][0][2] == screen.SELECTION]
    assert selected == [grid.find("^" + MANY[30].record)[0]]
    assert {cell[2] for cell in grid.cells[selected[0]]} == {screen.SELECTION}


def test_a_history_row_is_record_what_sha_result_took_and_the_trailing_column():
    grid, _ = drawn(HISTORY, LIVE, tab="history")
    grid.find(r"^record +what +sha +result +took")
    grid.find(rf"^{STAMP} +PR #31 +a1b2c3d +running ")
    grid.find(rf"^{OLDER} +PR #31 +a1b2c3d +failed +62m +bundle")
    grid.find(rf"^{OLDEST} +PR #31 +a1b2c3d +bench error +62m +precondition")
    grid.find(rf"^{HAND.record} +feature/x +a1b2c3d +passed +62m +by hand")


def test_help_is_the_keys_title_over_three_headed_columns():
    grid, _ = drawn([run()], tab="help")
    for heading in ("move", "runs and log", "commands"):
        grid.find(heading)
    grid.find(r"\[ \] +older / newer run")


def test_a_parsed_real_record_draws_its_stages_summary_and_target_suite():
    parsed = real_run()
    summary, _ = drawn([parsed], IDLE)
    for name in ("checkout", "precondition", "build", "bundle", "host suite", "target suite"):
        summary.find(rf"^✓ {name} ")
    summary.find(r"host suite    2 passed")
    summary.find(r"target suite  4 passed")

    target, _ = drawn([parsed], IDLE, stage="target_suite")
    target.find(r"target suite  4 of 4  4 passed  0 failed")
    target.find(r"✓ setup install_candidate")


def test_a_stage_left_open_in_a_dead_run_reads_a_question_mark_and_a_dash_for_took():
    states = ["passed", "passed", "open"] + ["not-started"] * 4
    grid, _ = drawn([run(states=states, result=None)], IDLE)
    grid.find(rf"\? build +{lc(3, 10)} +—")


def test_tabs_expand_and_control_characters_are_drawn_as_caret_pairs():
    lines = ["a\tb", "esc\x1b[0m", "del\x7f", "\x01"]
    grid, _ = drawn([live("build", build=stage("build", "open", t(3, 10), lines=lines))], LIVE)
    for shown in ("a       b", r"esc\^\[\[0m", r"del\^\?", r"\^A"):
        grid.find(shown)


def test_a_failed_tests_crash_line_and_traceback_lines_are_drawn_under_its_output():
    failed = suite(
        "host_suite",
        ["passed", "failed"],
        {1: ("out line",)},
        {1: ("t.py:88: AssertionError", "E   boom")},
    )
    grid, _ = drawn([run(host_suite=failed)], IDLE, stage="host_suite")
    output, _, _ = grid.find("out line")
    crash, _, _ = grid.find(r"t\.py:88: AssertionError")
    boom, _, _ = grid.find("E   boom")
    assert (crash, boom) == (output + 1, output + 2)


def test_a_failed_test_with_only_failure_lines_still_shows_its_fold_glyph():
    failed = suite("host_suite", ["failed"], failures={0: ("t.py:1: AssertionError",)})
    grid, _ = drawn([run(host_suite=failed)], IDLE, stage="host_suite")
    grid.find(r"▾ ✗ case_00")


def test_the_history_footer_counts_the_records_and_names_the_directory():
    grid, _ = drawn(HISTORY, LIVE, tab="history")
    grid.find(rf"^{len(HISTORY)} records in {DIRECTORY}/")


def test_the_stage_list_is_34_wide_with_its_divider_at_column_35_and_the_pane_at_column_37():
    grid, _ = drawn([live("build", build=stage("build", "open", t(3, 10), lines=["marker"]))], LIVE)
    assert grid.text(3).index("│") == 35
    assert grid.find("marker")[1] == 37


def past_build(lines=2):
    """A run whose build passed, with bitbake progress and `lines` output lines."""
    tasks = tuple(runlog.Task(f"r{i}", "do_compile", t(3, 11, i)) for i in range(3))
    progress = runlog.BuildProgress(1204, 3311, tasks)
    output = [f"NOTE: line {i}" for i in range(lines)]
    done = stage("build", "passed", t(3, 10), t(3, 40), lines=output, progress=progress)
    return run(build=done)


def test_a_past_builds_label_and_log_window_start_below_its_head_and_fill_the_room():
    grid, frame = drawn([past_build(lines=80)], IDLE, stage="build")
    top, room = frame.layout.pane_top, frame.room
    first, last = frame.rows[frame.scroll].text, frame.rows[frame.scroll + room - 1].text

    assert grid.find("this stage's output")[0] == top + frame.head_height - 1
    assert grid.find(re.escape(first))[0] == top + frame.head_height
    assert grid.find(re.escape(last))[0] == top + frame.head_height + room - 1


def test_a_passed_past_builds_head_is_not_drawn_amber():
    grid, _ = drawn([past_build()], IDLE, stage="build")
    head = [*grid.span(r"build  task"), *grid.span(r"█+")]
    assert screen.AMBER not in {cell[1] for cell in head}


def test_changing_the_stage_resets_the_scroll_so_the_new_stages_rows_above_the_cursor_show():
    outcomes = ["passed"] * 5 + ["failed"] + ["passed"] * 24
    runs = [
        run(
            host_suite=suite("host_suite", ["passed"] * 30),
            target_suite=suite("target_suite", outcomes),
        )
    ]
    first = resolve_frame(runs, view.State(stage="host_suite"))
    state = view.State(stage="host_suite", focus="log", cursor=first.rows[25].anchor, scroll=0)
    scrolled = resolve_frame(runs, state)
    state.scroll = scrolled.scroll
    assert scrolled.scroll > 0

    view.handle(state, "\x1b", scrolled)
    view.handle(state, "j", resolve_frame(runs, state))
    frame = resolve_frame(runs, state)

    assert (frame.stage.name, frame.cursor) == ("target_suite", 5)
    assert frame.scroll == 0


def resolve_frame(runs, state):
    """The frame for `runs` in `state` at the default size."""
    return view.resolve(state, view.Snapshot(runs, IDLE, NOW, DIRECTORY), *SIZE)
