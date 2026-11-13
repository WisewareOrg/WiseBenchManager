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
    mark,
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
    return {grid.text(y)[:17].strip(): grid.cells[y][0][2] for y in range(3, SIZE[0] - 2)}


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
    assert {summary.cells[y][0][2] for y in range(3, SIZE[0] - 2)} == {None}


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
    assert "1 Run  2 Queue  3 History" in newest.text(0)
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
    assert re.search(header, grid.text(2))


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
    bar = grid.span(r"▉+")
    assert len(bar) == 33
    assert not any("█" in grid.text(y) for y in range(SIZE[0]))
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
    progress = runlog.BuildProgress(1204, 3311, tasks, phase="task")
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
    head = [*grid.span(r"build  task"), *grid.span(r"▉+")]
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


TABS = {
    "run": (lambda: drawn([run()], IDLE), r"^last run  PR #31"),
    "empty run": (lambda: drawn([], IDLE), r"^0 records in"),
    "history": (lambda: drawn(HISTORY, LIVE, tab="history"), r"^record +what +sha"),
    "help": (lambda: drawn([run()], tab="help"), r"^keys"),
}


@pytest.mark.parametrize(("scene", "first"), TABS.values(), ids=TABS)
def test_a_rule_sits_under_the_tab_line_and_over_the_status_bar_with_the_content_between(
    scene, first
):
    grid, frame = scene()
    rule = "─" * SIZE[1]
    for y in (1, frame.layout.bar - 1):
        assert grid.text(y) == rule
        assert {cell[1] for cell in grid.cells[y]} == {screen.RULE}
    assert re.search(first, grid.text(2))


def test_the_history_footer_is_the_row_above_the_lower_rule():
    grid, frame = drawn(HISTORY, LIVE, tab="history")
    assert grid.text(frame.layout.bar - 2).startswith(f"{len(HISTORY)} records in")


def parsed_build(*lines):
    """A live run parsed from a log whose build stage has started and printed `lines`."""
    text = mark("run started kind=test ref=main") + mark("stage build started", 0, 1)
    return runlog.parse_run(STAMP, text + "".join(f"{x}\n" for x in lines), {})


def build_head(*lines):
    """The grid of a live run parsed from `lines`, and the row its stage's head starts on."""
    grid, frame = drawn([parsed_build(*lines)], LIVE)
    return grid, frame.layout.pane_top


KAS_INFO = "2026-11-10 03:00:02 - INFO     - NOTE: "
COUNTED = "NOTE: Running setscene task 1000 of 3300 (/l/a.bb:do_fetch_setscene)"
HEADS = {
    "nothing yet": (["NOTE: Executing"], "starting", 0),
    "cache loaded": (["Loading cache...done."], "parsing recipes", 0),
    "cache loaded, kas prefix": ([KAS_INFO + "Loading cache...done."], "parsing recipes", 0),
    "recipes parsed": (
        ["Loading cache...done.", "Parsing recipes...done."],
        "initialising tasks",
        0,
    ),
    "the last phase line wins": (
        ["Parsing recipes...done.", "Loading cache...done."],
        "parsing recipes",
        0,
    ),
    "a setscene count replaces the phase": (
        ["Parsing recipes...done.", COUNTED],
        "setscene 1000 of 3300  30%",
        10,
    ),
    "a phase line after a count leaves the counts": (
        ["NOTE: Running task 5 of 10 (/l/b.bb:do_compile)", "Parsing recipes...done."],
        "task 5 of 10  50%",
        16,
    ),
    "a task count replaces the phase": (
        ["Loading cache...done.", "NOTE: Running task 5 of 10 (/l/b.bb:do_compile)"],
        "task 5 of 10  50%",
        16,
    ),
}


@pytest.mark.parametrize(("lines", "text", "filled"), HEADS.values(), ids=HEADS)
def test_a_builds_head_is_its_phase_text_until_the_first_count_over_a_bar_of_33_ticks(
    lines, text, filled
):
    grid, y = build_head(*lines)
    assert grid.text(y)[screen._PANE :].rstrip() == f"build  {text}"
    bar = grid.cells[y + 1][screen._PANE : screen._PANE + screen._BAR_CELLS]
    assert {cell[0] for cell in bar} == {"▉"}
    assert [cell[1] for cell in bar].count(screen.RULE) == 33 - filled


def test_a_running_now_row_widens_its_recipe_and_task_columns_to_the_longest_name_plus_two():
    stamp = "2026-11-10 03:11:00 - INFO     - NOTE: "
    run = parsed_build(
        stamp + "Running task 5 of 9 (/l/a.bb:do_rootfs)",
        stamp + "recipe core-image-base-1.0-r0: task do_rootfs: Started",
        stamp + "recipe zlib-1.3-r0: task do_package_write_ipk: Started",
    )
    size = (SIZE[0], 120)
    frame = view.resolve(view.State(), view.Snapshot([run], LIVE, NOW, DIRECTORY), *size)
    grid = Grid(*size)
    screen.draw(grid, frame)
    grid.find(
        rf"^.{{{screen._PANE}}}"
        + re.escape(f"{'core-image-base':<17}{'do_rootfs':<22}")
        + r"\d+m\d\ds"
    )
    grid.find(
        rf"^.{{{screen._PANE}}}"
        + re.escape(f"{'zlib':<17}{'do_package_write_ipk':<22}")
        + r"\d+m\d\ds"
    )


def test_a_running_now_row_keeps_its_elapsed_time_at_80_columns_by_cutting_the_recipe_first():
    stamp = "2026-11-10 03:11:00 - INFO     - NOTE: "
    grid, _ = build_head(
        stamp + "Running task 5 of 9 (/l/a.bb:do_rootfs)",
        stamp + "recipe core-image-base-1.0-r0: task do_package_write_ipk: Started",
    )
    y = grid.find(rf"^.{{{screen._PANE}}}core-image")[0]
    shown = re.fullmatch(r"(\S+) {2,}(\S+) {2,}(\d+m\d\ds) *", grid.text(y)[screen._PANE :])
    assert shown is not None, grid.text(y)
    assert shown.group(1, 2) == ("core-image-b", "do_package_write_ipk")


def test_a_running_now_row_at_80_columns_spills_the_excess_into_the_task_column_past_the_recipe():
    stamp = "2026-11-10 03:11:00 - INFO     - NOTE: "
    grid, _ = build_head(
        stamp + "Running task 5 of 9 (/l/a.bb:do_rootfs)",
        stamp + "recipe core-image-base-1.0-r0: task do_package_qa_setscene: Started",
    )
    y = grid.find(rf"^.{{{screen._PANE}}}core-image")[0]
    row = grid.text(y)[screen._PANE :].rstrip()
    assert re.fullmatch(r"core-image-b {2}do_package_qa_setsce {2}\d+m\d\ds", row), row
    assert len(row) <= SIZE[1] - screen._PANE


# -- the Queue tab ----------------------------------------------------------------------

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
HAND_ENTRY_2 = {**HAND_ENTRY, "id": 4, "added": "2026-11-10T03:40:00+00:00", "ref": "feature-b"}
MERGE_ENTRY = {
    "kind": "run",
    "ref": "refs/heads/gh-readonly-queue/main/pr-14-cafef00d",
    "sha": "9f8e7d6" + "0" * 33,
    "pr": 14,
}
CLEAN = {"sha": "1234567" + "0" * 33, "subject": "Fix the thing", "clean": True}


def queue_drawn(runs=None, bench=IDLE, queue=(), keys=(), preview=None, size=SIZE, **state):
    """The Queue tab over `runs` and `queue`, after pressing `keys` on it: (grid, frame)."""
    runs = [run()] if runs is None else runs
    chosen = view.State(tab="queue", **state)
    read = None if queue is None else list(queue)
    snap = view.Snapshot(runs, bench, NOW, DIRECTORY, queue=read, preview=preview)
    for key in keys:
        view.handle(chosen, key, view.resolve(chosen, snap, *size))
    frame = view.resolve(chosen, snap, *size)
    grid = Grid(*size)
    screen.draw(grid, frame)
    return grid, frame


def test_the_active_queue_tab_is_inverted_and_the_other_tabs_are_not():
    grid, _ = queue_drawn()
    assert {cell[2] for cell in grid.span(r"2 Queue")} == {screen.TEXT}
    assert {cell[2] for cell in grid.span(r"1 Run")} == {None}
    assert {cell[2] for cell in grid.span(r"3 History")} == {None}


def test_the_queue_header_names_the_columns_in_dim_and_the_rows_follow_in_order():
    grid, _ = queue_drawn(queue=[HAND_ENTRY, HAND_ENTRY_2, MERGE_ENTRY])
    head, _, _ = grid.find(r"source +what +sha +added")
    assert {cell[1] for cell in grid.span(r"source +what +sha +added") if cell[0] != " "} == {
        screen.DIM
    }
    first, _, _ = grid.find(r"^1 +by hand +feature-a +1111111 +06:15")
    second, _, _ = grid.find(r"^2 +by hand +feature-b +1111111 +06:40")
    third, _, _ = grid.find(r"^3 +merge queue +PR #14 +9f8e7d6 *$")
    assert head < first and (second, third) == (first + 1, first + 2)


def test_by_hand_is_blue_and_the_cursor_row_is_on_the_selection():
    grid, _ = queue_drawn(queue=[HAND_ENTRY, MERGE_ENTRY])
    assert {cell[1] for cell in grid.span(r"by hand")} == {screen.BLUE}
    assert {cell[2] for cell in grid.span(r"by hand +feature-a +1111111 +06:15")} == {
        screen.SELECTION
    }
    assert {cell[2] for cell in grid.span(r"merge queue +PR #14 +9f8e7d6")} == {None}


def test_moving_the_cursor_moves_the_selection_with_it():
    grid, _ = queue_drawn(queue=[HAND_ENTRY, MERGE_ENTRY], keys="j")
    assert {cell[2] for cell in grid.span(r"merge queue +PR #14 +9f8e7d6")} == {screen.SELECTION}
    assert {cell[2] for cell in grid.span(r"by hand +feature-a +1111111 +06:15")} == {None}


def test_the_running_row_is_a_marker_a_tint_and_the_amber_running_stage():
    grid, _ = queue_drawn([live("build")], LIVE, queue=[HAND_ENTRY], keys="j")
    assert grid.span(r"▶")[0][1] == screen.AMBER
    for pattern in (r"▶", r"merge queue +PR #31 +a1b2c3d", r"running, build"):
        assert {cell[2] for cell in grid.span(pattern)} == {screen.RUNNING_TINT}
    assert {cell[1] for cell in grid.span(r"running, build")} == {screen.AMBER}
    grid.find(r"^2 +by hand +feature-a")


def test_a_running_hand_run_reads_by_hand_in_blue_on_the_running_row():
    hand_run = live("build", kind="test", pr=None, ref="feature-z")
    grid, _ = queue_drawn([hand_run], LIVE, queue=[HAND_ENTRY], keys="j")
    assert {cell[1] for cell in grid.span(r"by hand +feature-z")[:7]} == {screen.BLUE}
    grid.find(r"running, build")


def test_a_queue_with_nothing_in_it_draws_no_rows_and_no_footer():
    for queue in (None, []):
        grid, _ = queue_drawn(queue=queue)
        text = "".join(grid.text(y) for y in range(SIZE[0]))
        assert "by hand" not in text and "merge queue" not in text
        assert "updated" not in text and " ago" not in text


def test_the_preview_box_sits_above_the_status_bar_below_the_rows_with_a_blue_title():
    state = {"prompt": ":run feature-c", "preview": ("feature-c", CLEAN)}
    grid, frame = queue_drawn(queue=[HAND_ENTRY, HAND_ENTRY_2], **state)
    title, _, _ = grid.find(r"will queue")
    second, _, _ = grid.find(r'resolves to 1234567 "Fix the thing", clean tree')
    third, _, _ = grid.find(r"joins as 3, after feature-b")
    last_row, _, _ = grid.find(r"^2 +by hand +feature-b")
    assert (second, third) == (title + 1, title + 2)
    assert last_row < title and third < frame.layout.bar
    assert {cell[1] for cell in grid.span(r"will queue")} == {screen.BLUE}


def test_a_dirty_checkout_and_an_unresolvable_ref_are_drawn_as_the_frame_words_them():
    dirty = {**CLEAN, "clean": False}
    grid, _ = queue_drawn(prompt=":run x", preview=("x", dirty))
    grid.find(r'resolves to 1234567 "Fix the thing", uncommitted changes in the checkout')
    grid.find(r"joins as 1$")
    grid, _ = queue_drawn(prompt=":run nope", preview=("nope", "fatal: bad revision"))
    grid.find(r"nope does not resolve")
    assert "will queue" not in "".join(grid.text(y) for y in range(SIZE[0]))


def test_no_preview_box_without_a_result_for_the_typed_ref():
    grid, _ = queue_drawn(prompt=":run feature-c", preview=("other", CLEAN))
    assert "will queue" not in "".join(grid.text(y) for y in range(SIZE[0]))


def test_the_run_prompt_shows_the_hints_enter_queue_it_and_esc_cancel_and_no_tab_complete():
    grid, frame = queue_drawn(prompt=":run feature-c")
    row = grid.text(frame.layout.prompt)
    assert row.startswith(":run feature-c")
    assert re.search(r"Enter queue it  Esc cancel", row)
    assert "Tab complete" not in "".join(grid.text(y) for y in range(SIZE[0]))


def test_a_message_is_drawn_in_the_prompt_row_with_the_status_bar_above_it():
    grid, frame = queue_drawn(message="no queued run 3")
    assert frame.prompt is None and frame.layout.prompt == SIZE[0] - 1
    assert "no queued run 3" in grid.text(frame.layout.prompt)
    assert "IDLE" in grid.text(frame.layout.bar)
    assert frame.layout.bar == SIZE[0] - 2


def test_a_message_is_drawn_on_every_tab():
    for tab in ("run", "history"):
        grid, frame = drawn([run()], tab=tab, message="queued 5 1234567")
        assert "queued 5 1234567" in grid.text(frame.layout.prompt)


def test_the_help_lists_the_new_keys_and_no_longer_h_l():
    grid, _ = drawn([run()], tab="help")
    grid.find(r"Tab S-Tab +next / prev view")
    grid.find(r"1-3 +jump to view")
    grid.find(r"dd +remove hand-queued run")
    grid.find(r":run ref +queue a ref")
    text = "".join(grid.text(y) for y in range(SIZE[0]))
    assert "prev / next view" not in text


@pytest.mark.parametrize("size", [SIZE, (10, 40), (3, 5)])
def test_the_queue_tab_draws_on_any_size_with_rows_a_preview_and_a_message(size):
    queue_drawn([live("build")], LIVE, [HAND_ENTRY, MERGE_ENTRY], size=size)
    queue_drawn(queue=None, size=size)
    queue_drawn(
        queue=[HAND_ENTRY], prompt=":run feature-c", preview=("feature-c", CLEAN), size=size
    )
    queue_drawn(queue=[HAND_ENTRY], message="queued 5 1234567", size=size)
