"""`screen.draw`: a resolved frame drawn onto a surface in the canvas's layout and palette."""

import re

import pytest
from status_helpers import (
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
    plain,
    real_run,
    run,
    running_test,
    suite,
    t,
)

from wisebench import screen, view

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
    frame = view.resolve(view.State(**state), view.Snapshot(runs, bench, now), *SIZE)
    grid = Grid(*SIZE)
    screen.draw(grid, frame)
    return grid, frame


def stage_row(glyph, name, start="", took=""):
    """A stage row as the canvas columns it: glyph and name in 17, start in 10, then took."""
    return f"{glyph} {name}".ljust(17) + start.ljust(10) + took


SCENES = {
    "live build": lambda: drawn([building(3)], LIVE),
    "live suite": lambda: drawn([running_test(3, failed=(1,))], LIVE),
    "clean suite": lambda: drawn([running_test(2)], LIVE),
    "disabled": lambda: drawn([failing_at(STAMP, "bundle")], DISABLED),
    "idle": lambda: drawn([FINISHED], IDLE),
    "plain": lambda: drawn([live("build", build=plain("build", 3))], LIVE),
    "history": lambda: drawn(HISTORY, LIVE, tab="history"),
    "help": lambda: drawn([run()], tab="help"),
}

COLOURS = {
    "passed stage row green": ("idle", r"✓ checkout +\S+ +\S+", "GREEN", None, None),
    "running stage row amber bold on its tint": (
        "live build",
        r"● build +\S+ +\S+",
        "AMBER",
        "RUNNING_TINT",
        True,
    ),
    "failed stage row red bold on its tint": (
        "disabled",
        r"✗ bundle +\S+ +\S+",
        "RED",
        "FAILED_TINT",
        True,
    ),
    "stage not started dim": ("live build", r"· bundle", "DIM", None, None),
    "stage list header dim": ("idle", r"stage +start +took", "DIM", None, None),
    "run title bright bold": ("live build", r"PR #31", "BRIGHT", None, True),
    "active tab inverse": ("live build", r"1 Run", "GROUND", "TEXT", None),
    "inactive tab dim": ("live build", r"3 History", "DIM", None, None),
    "RUNNING badge": ("live build", r"RUNNING", "GROUND", "AMBER", True),
    "IDLE badge": ("idle", r"IDLE", "BRIGHT", "RULE", True),
    "DISABLED badge": ("disabled", r"DISABLED", "GROUND", "RED", True),
    "timer on": ("live build", r"timer (on)", "GREEN", None, None),
    "timer off": ("disabled", r"timer (off)", "RED", None, None),
    "passed section green": ("live suite", r"✓ t\.py::test_0", "GREEN", None, None),
    "failed section red bold": ("live suite", r"✗ t\.py::test_1", "RED", None, True),
    "running section amber bold": ("live suite", r"● t\.py::test_3", "AMBER", None, True),
    "section not started dim": ("live suite", r"· t\.py::test_4", "DIM", None, None),
    "the word setup blue": ("live suite", r"(setup) install_candidate", "BLUE", None, None),
    "running suite name amber bold": ("live suite", r"target suite(?=  \d)", "AMBER", None, True),
    "suite head passed count green": ("live suite", r"2 passed(?=  1 failed)", "GREEN", None, None),
    "suite head failed count red": ("live suite", r"1 failed", "RED", None, None),
    "no failures dim": ("clean suite", r"0 failed", "DIM", None, None),
    "log output light grey": ("plain", r"build line 0", "LOG", None, None),
    "idle result green bold": ("idle", r"passed(?=  ended)", "GREEN", None, True),
    "idle suite counts plain text": ("idle", r"host suite +(2 passed)", "TEXT", None, False),
    "History running amber": ("history", r"running", "AMBER", None, None),
    "History failed red": ("history", r"\bfailed\b", "RED", None, None),
    "History bench error red": ("history", r"bench error", "RED", None, None),
    "History passed green": ("history", r"\bpassed\b", "GREEN", None, None),
    "History by hand blue": ("history", r"by hand", "BLUE", None, None),
    "History failing stage dim": ("history", r"failed +\S+ +(bundle)", "DIM", None, None),
    "help title bright bold": ("help", r"^(keys)", "BRIGHT", None, True),
    "help heading blue": ("help", r"^(move)", "BLUE", None, None),
    "help key amber": ("help", r"j k(?= +down)", "AMBER", None, None),
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
        view.resolve(view.State(stage="host_suite"), view.Snapshot([LONG], IDLE, NOW), *SIZE)
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
