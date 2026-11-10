"""`status.View.key` and `View.tick`: focus, movement, folds, follow, search and prompts."""

import curses
from datetime import UTC, datetime

import pytest

from wisebench import status

STAMP = "20261110T030715Z"
OLDER = "20261109T030715Z"
OLDEST = "20261108T030715Z"
NAMES = ("checkout", "precondition", "build", "bundle", "host_suite", "target_suite", "posted")
NOW = datetime(2026, 11, 10, 10, 7, 30, tzinfo=UTC)
SIZE = (24, 80)
RIGHT = 45
LIVE = status.Bench(live=True, disabled=False, timer="on")
IDLE = status.Bench(live=False, disabled=False, timer="on")
DISABLED = status.Bench(live=False, disabled=True, timer="off")
ESC = "\x1b"
ENTER = "\n"
CD = "\x04"
CU = "\x15"


def t(minute, second=0):
    return datetime(2026, 11, 10, 3, minute, second, tzinfo=UTC)


def stage(name, state="passed", index=0, lines=(), sections=()):
    ended = None if state in ("open", "not-started") else t(5 * index + 2)
    started = None if state == "not-started" else t(5 * index)
    return status.Stage(
        name, state, started, ended, None, tuple(lines), tuple(sections), None,
        len(sections), len(sections), 0, 0,
    )  # fmt: skip


def suite(name, outcomes, bodies=None, failures=None, lead=(), names=None, state="passed", index=4):
    """A sectioned stage: one test section per outcome, bodies and failures keyed by position."""
    sections = []
    for position, outcome in enumerate(outcomes):
        label = names[position] if names else f"case_{position:02d}"
        sections.append(
            status.Section(
                (name, "test", label, 0),
                "test",
                label,
                outcome,
                None,
                tuple((bodies or {}).get(position, ())),
                tuple((failures or {}).get(position, ())),
            )
        )
    built = stage(name, state, index, lead, sections)
    return built


def sid(stage_name, label):
    return (stage_name, "test", label, 0)


def build_run(record, states, result, **replaced):
    stages = []
    for index, (name, state) in enumerate(zip(NAMES, states, strict=True)):
        stages.append(replaced.get(name, stage(name, state, index)))
    ended = None if result is None else t(40)
    return status.Run(record, "run", "main", 31, t(0), result, ended, tuple(stages))


def done(record=STAMP, **replaced):
    return build_run(record, ["passed"] * 7, "passed", **replaced)


def failing_at(record, name):
    cut = NAMES.index(name)
    states = ["passed"] * cut + ["failed"] + ["not-started"] * (6 - cut)
    return build_run(record, states, "failed")


def live(open_name, record=STAMP, **replaced):
    cut = NAMES.index(open_name)
    states = ["passed"] * cut + ["open"] + ["not-started"] * (6 - cut)
    return build_run(record, states, None, **replaced)


def keys(view, runs, *presses, bench=IDLE, size=SIZE):
    """Send each press (a string is sent character by character); return the last result."""
    result = None
    for press in presses:
        for key in [press] if isinstance(press, int) else press:
            result = view.key(key, runs, bench, size)
    return result


def arrive(runs, name, bench=IDLE, size=SIZE):
    """A view whose stage selection reached `name` by `j` presses, so its log defaults are set."""
    view = status.View()
    for _ in range(len(NAMES) + 1):
        if view.stage == name:
            break
        keys(view, runs, "j", bench=bench, size=size)
    assert view.stage == name
    return view


def in_log(runs, name, bench=IDLE, size=SIZE):
    view = arrive(runs, name, bench, size)
    keys(view, runs, ENTER, bench=bench, size=size)
    return view


def draw(view, runs, bench=IDLE, size=SIZE):
    rows, cols = size
    screen = status.render(view, runs, bench, rows, cols, NOW)
    cells = []
    for row in screen:
        line = []
        for text, style in row:
            line.extend((ch, style) for ch in text)
        assert len(line) == cols
        cells.append(line)
    return cells


def text(cells, row, start=0):
    return "".join(ch for ch, _ in cells[row][start:])


def cursor(view, runs, bench=IDLE, size=SIZE):
    """(row, text) of the one log row drawn with the selection style across the right pane."""
    cells = draw(view, runs, bench, size)
    found = [
        row
        for row in range(3, size[0] - 1)
        if all("selection" in style for _, style in cells[row][RIGHT:])
    ]
    assert len(found) == 1, found
    others = [
        row
        for row in range(3, size[0] - 1)
        if row != found[0] and any("selection" in style for _, style in cells[row][RIGHT:])
    ]
    assert others == []
    return found[0], text(cells, found[0], RIGHT).strip()


def shown(view, runs, bench=IDLE, size=SIZE):
    return cursor(view, runs, bench, size)[1]


def tab_line(view, runs, bench=IDLE, size=SIZE):
    return text(draw(view, runs, bench, size), 0)


MANY = done(
    host_suite=suite("host_suite", ["passed"] * 30, bodies={i: (f"out {i}",) for i in range(30)})
)
RUNS = [MANY]


# ---------------------------------------------------------------- focus and Enter / Esc


def test_enter_moves_focus_into_a_log_with_rows_and_esc_moves_it_back():
    view = arrive(RUNS, "host_suite")
    assert view.focus == "stages"
    keys(view, RUNS, ENTER)
    assert view.focus == "log"
    keys(view, RUNS, ESC)
    assert view.focus == "stages"
    keys(view, RUNS, ESC)
    assert view.focus == "stages"


def test_enter_on_a_plain_pane_with_lines_enters_the_log():
    runs = [done(build=stage("build", "passed", 2, lines=("one", "two")))]
    view = arrive(runs, "build")
    keys(view, runs, ENTER)
    assert view.focus == "log"


def test_enter_with_no_log_rows_does_nothing():
    summary = status.View()
    keys(summary, RUNS, ENTER)
    assert summary.focus == "stages"
    assert summary.stage is None
    runs = [live("build")]
    pending = arrive(runs, "posted", bench=LIVE)
    keys(pending, runs, ENTER, bench=LIVE)
    assert pending.focus == "stages"


def test_movement_keys_in_the_log_do_not_change_the_selected_stage():
    view = in_log(RUNS, "host_suite")
    keys(view, RUNS, "jjkG")
    assert view.stage == "host_suite"


# ---------------------------------------------------------------- stage list movement


def test_j_from_no_selection_selects_the_first_stage_and_k_the_last():
    first = status.View()
    keys(first, RUNS, "j")
    assert first.stage == "checkout"
    last = status.View()
    keys(last, RUNS, "k")
    assert last.stage == "posted"


def test_stage_j_and_k_step_one_and_clamp_at_both_ends():
    view = arrive(RUNS, "bundle")
    keys(view, RUNS, "j")
    assert view.stage == "host_suite"
    keys(view, RUNS, "kk")
    assert view.stage == "build"
    keys(view, RUNS, "G", "j")
    assert view.stage == "posted"
    keys(view, RUNS, "gg", "k")
    assert view.stage == "checkout"


def test_stage_gg_and_capital_g_jump_to_the_first_and_last_stage():
    view = arrive(RUNS, "build")
    keys(view, RUNS, "G")
    assert view.stage == "posted"
    keys(view, RUNS, "gg")
    assert view.stage == "checkout"


def test_stage_half_pages_clamp_at_the_ends_of_a_seven_row_list():
    view = arrive(RUNS, "build")
    keys(view, RUNS, CD)
    assert view.stage == "posted"
    keys(view, RUNS, CU)
    assert view.stage == "checkout"


def test_stage_list_movement_ends_follow_and_a_log_key_does_not():
    runs = [growing(5)]
    view = status.View()
    view.tick(runs, LIVE, SIZE)
    assert view.follow is True
    keys(view, runs, ENTER, "j", bench=LIVE)
    assert view.follow is True
    keys(view, runs, ESC, "k", bench=LIVE)
    assert view.follow is False


@pytest.mark.parametrize("press", ["j", "k", "gg", "G", CD, CU])
def test_every_stage_list_movement_key_ends_follow(press):
    runs = [live("host_suite")]
    view = status.View()
    view.tick(runs, LIVE, SIZE)
    keys(view, runs, press, bench=LIVE)
    assert view.follow is False


# ---------------------------------------------------------------- log movement and the cursor


def test_the_log_cursor_is_drawn_on_the_first_row_before_any_key():
    view = in_log(RUNS, "host_suite")
    row, line = cursor(view, RUNS)
    assert row == 3
    assert "case_00" in line


def test_log_j_and_k_move_one_row_and_clamp():
    view = in_log(RUNS, "host_suite")
    keys(view, RUNS, "j")
    assert "case_01" in shown(view, RUNS)
    keys(view, RUNS, "kk")
    assert "case_00" in shown(view, RUNS)
    keys(view, RUNS, "G", "j")
    assert "case_29" in shown(view, RUNS)


def test_log_gg_and_capital_g_go_to_the_first_and_last_row():
    view = in_log(RUNS, "host_suite")
    keys(view, RUNS, "jjj", "G")
    assert "case_29" in shown(view, RUNS)
    keys(view, RUNS, "gg")
    assert "case_00" in shown(view, RUNS)


@pytest.mark.parametrize(
    ("size", "first", "second", "back"),
    [((24, 80), 9, 18, 9), ((40, 80), 17, 29, 12)],
)
def test_log_half_pages_move_by_half_of_rows_minus_five(size, first, second, back):
    view = in_log(RUNS, "host_suite", size=size)
    keys(view, RUNS, CD, size=size)
    assert f"case_{first:02d}" in shown(view, RUNS, size=size)
    keys(view, RUNS, CD, size=size)
    assert f"case_{second:02d}" in shown(view, RUNS, size=size)
    keys(view, RUNS, CU, size=size)
    assert f"case_{back:02d}" in shown(view, RUNS, size=size)
    keys(view, RUNS, CU, CU, CU, size=size)
    assert "case_00" in shown(view, RUNS, size=size)


def test_the_pane_scrolls_only_as_far_as_keeps_the_cursor_in_view():
    view = in_log(RUNS, "host_suite")
    keys(view, RUNS, "G")
    assert view.log_scroll == 11
    assert cursor(view, RUNS)[0] == 21
    keys(view, RUNS, "gg")
    assert view.log_scroll == 0
    keys(view, RUNS, "j" * 19)
    assert view.log_scroll == 1
    keys(view, RUNS, "k" * 18)
    assert view.log_scroll == 1
    keys(view, RUNS, "k")
    assert view.log_scroll == 0


def test_the_cursor_is_drawn_while_the_stage_list_has_focus():
    view = arrive(RUNS, "host_suite")
    assert view.focus == "stages"
    assert cursor(view, RUNS)[0] == 3


# ---------------------------------------------------------------- default cursor


FAILED_SUITE = suite(
    "host_suite",
    ["passed", "failed", "passed"],
    bodies={0: ("a0",), 1: ("b0", "b1")},
    failures={1: ("crash",)},
)


def test_the_default_cursor_is_the_first_failed_section_header():
    runs = [done(host_suite=FAILED_SUITE)]
    view = in_log(runs, "host_suite")
    line = shown(view, runs)
    assert "case_01" in line
    assert line.startswith("▾")
    mixed = suite("host_suite", ["passed", "open", "failed"], state="open")
    runs = [live("host_suite", host_suite=mixed)]
    view = in_log(runs, "host_suite", bench=LIVE)
    assert "case_02" in shown(view, runs, LIVE)


def test_the_default_cursor_without_a_failure_is_the_running_section_header():
    running = suite("host_suite", ["passed", "open", "passed"], state="open")
    runs = [live("host_suite", host_suite=running)]
    view = in_log(runs, "host_suite", bench=LIVE)
    assert "case_01" in shown(view, runs, LIVE)


# ---------------------------------------------------------------- folds


def test_za_on_a_header_toggles_that_section_and_a_second_za_toggles_back():
    runs = [done(host_suite=FAILED_SUITE)]
    view = in_log(runs, "host_suite")
    keys(view, runs, "za")
    assert view.folds[sid("host_suite", "case_01")] is False
    assert shown(view, runs).startswith("▸")
    keys(view, runs, "za")
    assert view.folds[sid("host_suite", "case_01")] is True
    assert shown(view, runs).startswith("▾")


def test_za_on_a_body_line_folds_its_section_and_moves_the_cursor_to_the_header():
    runs = [done(host_suite=FAILED_SUITE)]
    view = in_log(runs, "host_suite")
    keys(view, runs, "j")
    assert shown(view, runs) == "b0"
    keys(view, runs, "za")
    assert view.folds[sid("host_suite", "case_01")] is False
    line = shown(view, runs)
    assert "case_01" in line
    assert line.startswith("▸")


def test_za_in_the_stage_list_does_nothing():
    runs = [done(host_suite=FAILED_SUITE)]
    view = arrive(runs, "host_suite")
    keys(view, runs, "za")
    assert view.folds == {}


def test_capital_zr_and_zm_set_every_toggle_of_the_selected_stage_only():
    other = suite("target_suite", ["passed", "passed"], index=5)
    runs = [done(host_suite=FAILED_SUITE, target_suite=other)]
    view = in_log(runs, "host_suite")
    keys(view, runs, "zM")
    assert view.folds == {sid("host_suite", n): False for n in ("case_00", "case_01", "case_02")}
    keys(view, runs, "zR")
    assert view.folds == {sid("host_suite", n): True for n in ("case_00", "case_01", "case_02")}


def test_a_toggle_survives_a_reparse_because_it_is_keyed_by_section_id():
    runs = [done(host_suite=suite("host_suite", ["passed"] * 3, bodies={0: ("body",)}))]
    view = in_log(runs, "host_suite")
    keys(view, runs, "za")
    assert shown(view, runs).startswith("▾")
    reparsed = [done(host_suite=suite("host_suite", ["passed"] * 3, bodies={0: ("body",)}))]
    view.tick(reparsed, IDLE, SIZE)
    assert view.folds[sid("host_suite", "case_00")] is True
    assert shown(view, reparsed).startswith("▾")


# ---------------------------------------------------------------- cursor re-found after a re-parse


def test_the_cursor_is_re_found_by_section_id_when_rows_arrive_before_it():
    view = in_log(RUNS, "host_suite")
    keys(view, RUNS, "jjjjj")
    assert "case_05" in shown(view, RUNS)
    names = ["fresh_a", "fresh_b", *(f"case_{i:02d}" for i in range(30))]
    grown = [done(host_suite=suite("host_suite", ["passed"] * 32, names=names))]
    view.tick(grown, IDLE, SIZE)
    assert "case_05" in shown(view, grown)


def test_a_cursor_on_a_line_folded_away_by_a_reparse_lands_on_its_header():
    running = suite("host_suite", ["open"], bodies={0: ("x0", "x1")}, state="open")
    runs = [live("host_suite", host_suite=running)]
    view = in_log(runs, "host_suite", bench=LIVE)
    keys(view, runs, "j", bench=LIVE)
    assert shown(view, runs, LIVE) == "x0"
    finished = suite("host_suite", ["passed"], bodies={0: ("x0", "x1")})
    after = [done(host_suite=finished)]
    view.tick(after, IDLE, SIZE)
    line = shown(view, after)
    assert "case_00" in line
    assert line.startswith("▸")


# ---------------------------------------------------------------- opening states and follow


def test_a_fresh_view_on_a_live_run_selects_the_running_stage():
    runs = [live("build")]
    view = status.View()
    view.tick(runs, LIVE, SIZE)
    assert view.stage == "build"
    assert view.follow is True


def test_a_fresh_view_on_an_idle_bench_shows_the_summary():
    view = status.View()
    view.tick([failing_at(STAMP, "build")], IDLE, SIZE)
    assert view.stage is None
    assert view.follow is True


def test_a_fresh_view_on_a_disabled_bench_selects_the_first_failed_stage():
    view = status.View()
    view.tick([failing_at(STAMP, "bundle")], DISABLED, SIZE)
    assert view.stage == "bundle"


def test_a_disabled_bench_with_no_failed_stage_shows_the_summary():
    view = status.View()
    view.tick([done()], DISABLED, SIZE)
    assert view.stage is None


def test_a_disabled_bench_selects_a_stage_left_open_when_nothing_failed():
    stranded = build_run(STAMP, ["passed", "passed", "open"] + ["not-started"] * 4, None)
    view = status.View()
    view.tick([stranded], DISABLED, SIZE)
    assert view.stage == "build"


def test_follow_rides_the_running_stage_across_a_stage_change():
    view = status.View()
    view.tick([live("host_suite")], LIVE, SIZE)
    assert view.stage == "host_suite"
    view.tick([live("target_suite")], LIVE, SIZE)
    assert view.stage == "target_suite"


def test_follow_moves_to_the_summary_when_the_run_ends():
    view = status.View()
    view.tick([live("target_suite")], LIVE, SIZE)
    assert view.stage == "target_suite"
    view.tick([done()], IDLE, SIZE)
    assert view.stage is None


def test_follow_that_a_stage_movement_stopped_leaves_the_selection_alone():
    view = status.View()
    view.tick([live("host_suite")], LIVE, SIZE)
    keys(view, [live("host_suite")], "j", bench=LIVE)
    assert view.stage == "target_suite"
    view.tick([live("posted")], LIVE, SIZE)
    assert view.stage == "target_suite"
    view.tick([done()], IDLE, SIZE)
    assert view.stage == "target_suite"


def growing(count, stage_name="host_suite"):
    outcomes = ["passed"] * (count - 1) + ["open"]
    return live(stage_name, **{stage_name: suite(stage_name, outcomes, state="open")})


def test_capital_g_in_the_log_keeps_the_cursor_on_the_last_row_as_rows_arrive():
    view = status.View()
    view.tick([growing(25)], LIVE, SIZE)
    keys(view, [growing(25)], ENTER, "G", bench=LIVE)
    view.tick([growing(28)], LIVE, SIZE)
    assert "case_27" in shown(view, [growing(28)], LIVE)
    assert view.log_scroll == 27 - 18


def test_another_log_movement_key_stops_the_g_follow():
    view = status.View()
    view.tick([growing(5)], LIVE, SIZE)
    keys(view, [growing(5)], ENTER, "G", bench=LIVE)
    view.tick([growing(8)], LIVE, SIZE)
    assert "case_07" in shown(view, [growing(8)], LIVE)
    keys(view, [growing(8)], "k", bench=LIVE)
    view.tick([growing(10)], LIVE, SIZE)
    assert "case_06" in shown(view, [growing(10)], LIVE)


def test_the_g_follow_carries_across_a_followed_stage_change():
    first = growing(5)
    view = status.View()
    view.tick([first], LIVE, SIZE)
    keys(view, [first], ENTER, "G", bench=LIVE)
    target = suite("target_suite", ["failed", "passed", "open", "passed"], state="open", index=5)
    moved = live(
        "target_suite", host_suite=suite("host_suite", ["passed"] * 3), target_suite=target
    )
    view.tick([moved], LIVE, SIZE)
    assert view.stage == "target_suite"
    assert view.focus == "log"
    assert "case_03" in shown(view, [moved], LIVE)


# ---------------------------------------------------------------- [ ] 1 and History


PAST = [live("host_suite"), failing_at(OLDER, "build"), done(OLDEST)]


def test_bracket_keys_step_between_runs_and_open_each_in_its_past_form():
    view = status.View()
    view.tick(PAST, LIVE, SIZE)
    assert OLDER not in tab_line(view, PAST, LIVE)
    keys(view, PAST, "[", bench=LIVE)
    assert OLDER in tab_line(view, PAST, LIVE)
    assert view.stage == "build"
    assert view.follow is False
    keys(view, PAST, "[", bench=LIVE)
    assert OLDEST in tab_line(view, PAST, LIVE)
    assert view.stage is None
    keys(view, PAST, "]", bench=LIVE)
    assert OLDER in tab_line(view, PAST, LIVE)
    keys(view, PAST, "]", bench=LIVE)
    line = tab_line(view, PAST, LIVE)
    assert OLDER not in line
    assert OLDEST not in line
    assert view.stage == "host_suite"


def test_bracket_keys_do_nothing_at_the_ends():
    view = status.View()
    view.tick(PAST, LIVE, SIZE)
    before = tab_line(view, PAST, LIVE)
    assert keys(view, PAST, "]", bench=LIVE) is False
    assert tab_line(view, PAST, LIVE) == before
    assert view.stage == "host_suite"
    keys(view, PAST, "[[", bench=LIVE)
    oldest = tab_line(view, PAST, LIVE)
    assert OLDEST in oldest
    assert keys(view, PAST, "[", bench=LIVE) is False
    assert tab_line(view, PAST, LIVE) == oldest


def test_one_returns_to_the_newest_run_in_its_opening_state_and_follows():
    view = status.View()
    view.tick(PAST, LIVE, SIZE)
    keys(view, PAST, "[", "3", bench=LIVE)
    assert view.tab == "history"
    keys(view, PAST, "1", bench=LIVE)
    assert view.tab == "run"
    assert view.follow is True
    assert view.stage == "host_suite"
    assert OLDER not in tab_line(view, PAST, LIVE)


def test_h_and_l_switch_between_run_and_history_without_wrapping():
    view = status.View()
    keys(view, PAST, "h")
    assert view.tab == "run"
    keys(view, PAST, "l")
    assert view.tab == "history"
    keys(view, PAST, "l")
    assert view.tab == "history"
    keys(view, PAST, "h")
    assert view.tab == "run"


HISTORY = [
    status.Run(f"20261{i:03d}T000000Z", "run", "main", 1, t(0), "passed", t(5), ())
    for i in range(39, -1, -1)
]


def test_three_opens_history_selecting_the_newest_record():
    view = status.View()
    keys(view, HISTORY, "3")
    assert view.tab == "history"
    assert view.history_record == HISTORY[0].record


def history_view(size=SIZE):
    view = status.View()
    keys(view, HISTORY, "3", size=size)
    return view


def test_history_j_and_k_move_the_selection_by_record_and_clamp():
    view = history_view()
    keys(view, HISTORY, "jj")
    assert view.history_record == HISTORY[2].record
    keys(view, HISTORY, "kkk")
    assert view.history_record == HISTORY[0].record
    keys(view, HISTORY, "G", "j")
    assert view.history_record == HISTORY[39].record


def test_history_gg_and_capital_g_go_to_the_first_and_last_record():
    view = history_view()
    keys(view, HISTORY, "G")
    assert view.history_record == HISTORY[39].record
    keys(view, HISTORY, "gg")
    assert view.history_record == HISTORY[0].record


@pytest.mark.parametrize(("size", "half"), [((24, 80), 10), ((40, 80), 18)])
def test_history_half_pages_move_by_half_of_rows_minus_four(size, half):
    view = history_view(size)
    keys(view, HISTORY, CD, size=size)
    assert view.history_record == HISTORY[half].record
    keys(view, HISTORY, CU, size=size)
    assert view.history_record == HISTORY[0].record


def test_the_history_window_scrolls_just_enough_and_draws_the_selected_row():
    view = history_view()
    keys(view, HISTORY, "G")
    assert view.history_scroll == 20
    cells = draw(view, HISTORY)
    assert text(cells, 21).startswith(HISTORY[39].record)
    assert "selection" in cells[21][0][1]
    keys(view, HISTORY, "gg")
    assert view.history_scroll == 0


def test_history_enter_opens_an_older_run_in_its_past_form():
    view = status.View()
    view.tick(PAST, LIVE, SIZE)
    keys(view, PAST, "3", "j", ENTER, bench=LIVE)
    assert view.tab == "run"
    assert OLDER in tab_line(view, PAST, LIVE)
    assert view.stage == "build"
    assert view.follow is False


def test_history_enter_on_the_newest_run_opens_its_current_form_without_following():
    view = status.View()
    keys(view, PAST, "[", "3", bench=LIVE)
    keys(view, PAST, "k", ENTER, bench=LIVE)
    assert view.tab == "run"
    line = tab_line(view, PAST, LIVE)
    assert OLDER not in line
    assert OLDEST not in line
    assert view.stage == "host_suite"
    assert view.follow is False


def test_history_movement_leaves_the_run_selection_alone():
    view = arrive(PAST, "build", bench=LIVE)
    keys(view, PAST, "3", "jj", bench=LIVE)
    assert view.stage == "build"


# ---------------------------------------------------------------- Help and quit


@pytest.mark.parametrize("origin", ["run", "history"])
def test_question_mark_opens_help_and_question_mark_or_esc_returns_to_the_origin(origin):
    view = status.View()
    if origin == "history":
        keys(view, PAST, "3")
    keys(view, PAST, "?")
    assert view.tab == "help"
    keys(view, PAST, "?")
    assert view.tab == origin
    keys(view, PAST, "?", ESC)
    assert view.tab == origin


def test_help_ignores_every_key_except_back_quit_and_colon():
    view = status.View()
    keys(view, PAST, "?")
    for press in ("j", "k", "1", "3", "h", "l", "/", "[", "]", ENTER, "G", CD):
        assert keys(view, PAST, press) is False
        assert view.tab == "help"
        assert view.prompt is None
    assert keys(view, PAST, "q") is True


def test_colon_opens_the_command_line_in_help():
    view = status.View()
    keys(view, PAST, "?", ":")
    assert view.tab == "help"
    assert view.prompt == ":"


def test_q_quits_from_run_and_from_history():
    assert keys(status.View(), PAST, "q") is True
    view = status.View()
    keys(view, PAST, "3")
    assert keys(view, PAST, "q") is True


def test_a_resize_key_neither_quits_nor_changes_the_tab():
    view = status.View()
    assert keys(view, PAST, curses.KEY_RESIZE) is False
    assert view.tab == "run"


# ---------------------------------------------------------------- the command line


def test_colon_opens_the_prompt_and_typed_keys_append_including_q():
    view = status.View()
    assert keys(view, PAST, ":") is False
    assert view.prompt == ":"
    assert keys(view, PAST, "q") is False
    assert view.prompt == ":q"
    keys(view, PAST, "j")
    assert view.prompt == ":qj"
    assert view.tab == "run"


@pytest.mark.parametrize("enter", [ENTER, curses.KEY_ENTER])
def test_colon_q_enter_quits(enter):
    view = status.View()
    keys(view, PAST, ":q")
    assert keys(view, PAST, enter) is True


def test_any_other_colon_text_closes_the_line_without_acting():
    view = status.View()
    keys(view, PAST, ":run")
    assert keys(view, PAST, ENTER) is False
    assert view.prompt is None
    assert view.tab == "run"
    keys(view, PAST, ":")
    assert keys(view, PAST, ENTER) is False
    assert view.prompt is None


def test_esc_closes_the_prompt():
    view = status.View()
    keys(view, PAST, ":q", ESC)
    assert view.prompt is None


@pytest.mark.parametrize("backspace", ["\x7f", "\b", curses.KEY_BACKSPACE])
def test_backspace_deletes_a_character_and_closes_on_an_empty_line(backspace):
    view = status.View()
    keys(view, PAST, ":q")
    keys(view, PAST, backspace)
    assert view.prompt == ":"
    keys(view, PAST, backspace)
    assert view.prompt is None


def test_slash_opens_only_with_a_selected_stage_on_run():
    plain = status.View()
    keys(plain, PAST, "/")
    assert plain.prompt is None
    history = arrive(PAST, "build", bench=LIVE)
    keys(history, PAST, "3", "/", bench=LIVE)
    assert history.prompt is None
    run = arrive(PAST, "build", bench=LIVE)
    keys(run, PAST, "/ab", bench=LIVE)
    assert run.prompt == "/ab"
    keys(run, PAST, ESC, bench=LIVE)
    assert run.prompt is None


def test_slash_backspace_on_an_empty_search_closes_the_line():
    view = arrive(PAST, "build", bench=LIVE)
    keys(view, PAST, "/", "\x7f", bench=LIVE)
    assert view.prompt is None


# ---------------------------------------------------------------- pending g and z


def test_a_pending_g_followed_by_another_key_is_discarded_with_that_key():
    view = in_log(RUNS, "host_suite")
    keys(view, RUNS, "jj")
    keys(view, RUNS, "gj")
    assert "case_02" in shown(view, RUNS)
    keys(view, RUNS, "g")
    keys(view, RUNS, "k")
    assert "case_02" in shown(view, RUNS)


def test_a_pending_z_followed_by_another_key_is_discarded_with_that_key():
    view = in_log(RUNS, "host_suite")
    assert keys(view, RUNS, "zq") is False
    keys(view, RUNS, "a")
    assert view.folds == {}


# ---------------------------------------------------------------- search


NEEDLE = suite(
    "host_suite",
    ["passed"] * 30,
    bodies={i: ("out 7 needle" if i == 7 else f"out {i}",) for i in range(30)},
    lead=("preamble text",),
)
NEEDLE_RUNS = [done(host_suite=NEEDLE)]


def test_search_into_a_folded_section_unfolds_it_and_takes_the_cursor_and_focus():
    view = arrive(NEEDLE_RUNS, "host_suite")
    keys(view, NEEDLE_RUNS, "/needle", ENTER)
    assert view.prompt is None
    assert view.focus == "log"
    assert view.folds[sid("host_suite", "case_07")] is True
    assert shown(view, NEEDLE_RUNS) == "out 7 needle"


def test_search_matches_a_section_header_name():
    view = in_log(NEEDLE_RUNS, "host_suite")
    keys(view, NEEDLE_RUNS, "/case_12", ENTER)
    assert "case_12" in shown(view, NEEDLE_RUNS)


def test_search_wraps_to_a_stages_own_leading_lines():
    view = in_log(NEEDLE_RUNS, "host_suite")
    keys(view, NEEDLE_RUNS, "G", "/preamble", ENTER)
    assert shown(view, NEEDLE_RUNS) == "preamble text"


HITS = done(
    host_suite=suite("host_suite", ["passed"] * 3, bodies={i: (f"hit {i}",) for i in range(3)})
)


def test_n_and_capital_n_step_through_matches_and_wrap_both_ways():
    runs = [HITS]
    view = in_log(runs, "host_suite")
    keys(view, runs, "/hit", ENTER)
    seen = [shown(view, runs)]
    for _ in range(3):
        keys(view, runs, "n")
        seen.append(shown(view, runs))
    for _ in range(3):
        keys(view, runs, "N")
        seen.append(shown(view, runs))
    assert seen == ["hit 0", "hit 1", "hit 2", "hit 0", "hit 2", "hit 1", "hit 0"]


def test_n_works_from_the_stage_list_and_moves_focus_to_the_log():
    runs = [HITS]
    view = in_log(runs, "host_suite")
    keys(view, runs, "/hit", ENTER, ESC)
    assert view.focus == "stages"
    keys(view, runs, "n")
    assert view.focus == "log"


def test_a_search_is_case_sensitive_and_a_miss_closes_the_prompt_and_moves_nothing():
    runs = [HITS]
    view = in_log(runs, "host_suite")
    keys(view, runs, "j")
    before = (shown(view, runs), dict(view.folds), view.focus, view.stage)
    keys(view, runs, "/HIT", ENTER)
    assert view.prompt is None
    assert (shown(view, runs), dict(view.folds), view.focus, view.stage) == before


def test_an_empty_search_closes_the_prompt_and_moves_nothing():
    runs = [HITS]
    view = in_log(runs, "host_suite")
    before = (shown(view, runs), dict(view.folds), view.focus)
    keys(view, runs, "/", ENTER)
    assert view.prompt is None
    assert (shown(view, runs), dict(view.folds), view.focus) == before


def test_n_and_capital_n_do_nothing_without_a_selection_a_pattern_or_on_history():
    nothing = status.View()
    keys(nothing, [HITS], "nN")
    assert (nothing.stage, nothing.focus, nothing.folds) == (None, "stages", {})
    fresh = arrive([HITS], "host_suite")
    keys(fresh, [HITS], "nN")
    assert fresh.folds == {}
    assert fresh.focus == "stages"
    searched = in_log([HITS], "host_suite")
    keys(searched, [HITS], "/hit", ENTER, "3")
    record = searched.history_record
    keys(searched, [HITS], "nN")
    assert searched.tab == "history"
    assert searched.history_record == record


# ---------------------------------------------------------------- plain panes


LONG = done(build=stage("build", "passed", 2, lines=tuple(f"line_{i:02d}" for i in range(60))))
LONG_RUNS = [LONG]


def test_a_plain_pane_opens_with_the_cursor_on_its_last_line_on_the_panes_last_row():
    view = arrive(LONG_RUNS, "build")
    row, line = cursor(view, LONG_RUNS)
    assert (row, line) == (22, "line_59")
    assert view.log_scroll == 40


def test_a_plain_pane_shorter_than_the_pane_stays_bottom_aligned_under_its_label():
    short = done(build=stage("build", "passed", 2, lines=("a", "b", "c", "d", "e")))
    runs = [short]
    view = arrive(runs, "build")
    assert view.log_scroll == 0
    assert cursor(view, runs) == (22, "e")
    cells = draw(view, runs)
    assert text(cells, 17, RIGHT).strip() == "this stage's output"
    assert text(cells, 18, RIGHT).strip() == "a"


def test_plain_pane_movement_keys_move_the_line_cursor():
    view = in_log(LONG_RUNS, "build")
    keys(view, LONG_RUNS, "k")
    assert shown(view, LONG_RUNS) == "line_58"
    assert view.log_scroll == 40
    keys(view, LONG_RUNS, "gg")
    assert cursor(view, LONG_RUNS) == (3, "line_00")
    keys(view, LONG_RUNS, "j")
    assert shown(view, LONG_RUNS) == "line_01"
    keys(view, LONG_RUNS, "G")
    assert shown(view, LONG_RUNS) == "line_59"


def test_a_plain_pane_scrolls_just_enough_to_keep_the_cursor_in_view():
    view = in_log(LONG_RUNS, "build")
    keys(view, LONG_RUNS, "gg")
    assert view.log_scroll == 0
    keys(view, LONG_RUNS, "j" * 20)
    assert view.log_scroll == 1
    assert cursor(view, LONG_RUNS) == (22, "line_20")
    keys(view, LONG_RUNS, "k" * 19)
    assert view.log_scroll == 1
    keys(view, LONG_RUNS, "k")
    assert view.log_scroll == 0


def test_searching_a_plain_pane_takes_the_cursor_and_scrolls_to_the_match():
    view = in_log(LONG_RUNS, "build")
    keys(view, LONG_RUNS, "/line_17", ENTER)
    assert view.prompt is None
    assert cursor(view, LONG_RUNS) == (3, "line_17")
    assert view.log_scroll == 17


def build_lines(count):
    lines = tuple(f"line_{i:02d}" for i in range(count))
    return live("build", build=stage("build", "open", 2, lines=lines))


def test_a_plain_pane_follows_new_lines_until_a_log_movement_key_is_pressed():
    first = [build_lines(40)]
    view = status.View()
    view.tick(first, LIVE, SIZE)
    assert view.stage == "build"
    assert cursor(view, first, LIVE) == (22, "line_39")

    grown = [build_lines(45)]
    view.tick(grown, LIVE, SIZE)
    assert cursor(view, grown, LIVE) == (22, "line_44")

    keys(view, grown, ENTER, "k", bench=LIVE)
    assert shown(view, grown, LIVE) == "line_43"
    more = [build_lines(50)]
    view.tick(more, LIVE, SIZE)
    assert shown(view, more, LIVE) == "line_43"
