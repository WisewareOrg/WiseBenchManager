"""`status.View.key` and `View.tick`: focus, movement, folds, follow, search and prompts."""

import curses

import pytest
from status_helpers import (
    DISABLED,
    IDLE,
    LIVE,
    NAMES,
    OLDER,
    OLDEST,
    SIZE,
    STAMP,
    draw,
    failing_at,
    finished,
    live,
    mark,
    rep,
    run,
    stage,
    suite,
    t,
)

from wisebench import runlog, status

ESC = "\x1b"
ENTER = "\n"
CD = "\x04"
CU = "\x15"


def sid(stage_name, label):
    return (stage_name, "test", label, 0)


def keys(view, runs, *presses, bench=IDLE, size=SIZE):
    """Send each press (a string is sent character by character); return the last result."""
    result = None
    for press in presses:
        for key in [press] if isinstance(press, int) else press:
            result = view.key(key, runs, bench, size)
    return result


def arrive(runs, name, bench=IDLE, size=SIZE):
    """A view whose stage selection reached `name` by `j` presses, so its log defaults are set;
    `None` is a fresh view on the summary."""
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


def cursor(view, runs, bench=IDLE, size=SIZE):
    """(row, text) of the log row under the cursor."""
    return draw(runs, bench, view=view, size=size).cursor()


def shown(view, runs, bench=IDLE, size=SIZE):
    return cursor(view, runs, bench, size)[1]


def tab_line(view, runs, bench=IDLE):
    return draw(runs, bench, view=view).text(0)


MANY = run(host_suite=suite("host_suite", ["passed"] * 30, {i: (f"out {i}",) for i in range(30)}))
RUNS = [MANY]


PLAIN = [run(build=stage("build", "passed", t(3, 10), lines=("one", "two")))]


@pytest.mark.parametrize(
    ("runs", "name", "bench", "focus"),
    [
        (RUNS, None, IDLE, "stages"),
        ([live("build")], "posted", LIVE, "stages"),
    ],
    ids=["summary", "stage with no log rows"],
)
def test_enter_moves_focus_into_a_log_that_has_rows_and_nowhere_else(runs, name, bench, focus):
    view = arrive(runs, name, bench)
    keys(view, runs, ENTER, bench=bench)
    assert view.focus == focus
    keys(view, runs, ESC, bench=bench)
    assert view.focus == "stages"


@pytest.mark.parametrize(
    ("start", "presses", "expected"),
    [
        (None, "j", "checkout"),
        (None, "k", "posted"),
        ("bundle", "j", "host_suite"),
        ("host_suite", "kk", "build"),
        ("build", "Gj", "posted"),
        ("build", "ggk", "checkout"),
        ("build", CD, "posted"),
        ("build", CU, "checkout"),
    ],
)
def test_stage_list_movement_steps_jumps_and_clamps(start, presses, expected):
    view = arrive(RUNS, start)
    keys(view, RUNS, presses)
    assert view.stage == expected


@pytest.mark.parametrize("press", ["j", "k", "gg", "G", CD, CU])
def test_every_stage_list_movement_key_ends_follow(press):
    runs = [live("host_suite")]
    view = status.View()
    view.tick(runs, LIVE, SIZE)
    assert view.follow is True
    keys(view, runs, press, bench=LIVE)
    assert view.follow is False


def growing(count, stage_name="host_suite"):
    outcomes = ["passed"] * (count - 1) + ["open"]
    return live(stage_name, **{stage_name: suite(stage_name, outcomes, state="open")})


@pytest.mark.parametrize(
    ("presses", "case"),
    [
        ("j", 1),
        ("jkk", 0),
        ("Gj", 29),
        ("jjjG", 29),
        ("jjjGgg", 0),
        ("jjgj", 2),
        ("jjgk", 2),
    ],
    ids=[
        "j",
        "clamp top",
        "clamp bottom",
        "G",
        "gg",
        "pending g with j",
        "pending g with k",
    ],
)
def test_log_movement_keys_move_the_cursor_and_never_the_stage(presses, case):
    view = in_log(RUNS, "host_suite")
    keys(view, RUNS, presses)
    assert f"case_{case:02d}" in shown(view, RUNS)
    assert view.stage == "host_suite"


@pytest.mark.parametrize(
    ("size", "first", "second", "back"),
    [((24, 80), 9, 18, 9), ((40, 80), 17, 29, 12)],
)
def test_log_half_pages_move_by_half_of_rows_minus_five(size, first, second, back):
    view = in_log(RUNS, "host_suite", size=size)
    for press, case in ((CD, first), (CD, second), (CU, back)):
        keys(view, RUNS, press, size=size)
        assert f"case_{case:02d}" in shown(view, RUNS, size=size)
    keys(view, RUNS, CU, CU, CU, size=size)
    assert "case_00" in shown(view, RUNS, size=size)


def test_the_cursor_is_drawn_on_the_pane_s_last_row_at_the_last_case():
    view = in_log(RUNS, "host_suite")
    keys(view, RUNS, "G")
    assert cursor(view, RUNS)[0] == 21


FAILED_SUITE = suite(
    "host_suite",
    ["passed", "failed", "passed"],
    bodies={0: ("a0",), 1: ("b0", "b1")},
    failures={1: ("crash",)},
)


def running(outcomes):
    return [live("host_suite", host_suite=suite("host_suite", outcomes, state="open"))]


@pytest.mark.parametrize(
    ("runs", "bench", "case"),
    [
        ([run(host_suite=FAILED_SUITE)], IDLE, "case_01"),
        (running(["passed", "open", "failed"]), LIVE, "case_02"),
        (running(["passed", "open", "passed"]), LIVE, "case_01"),
    ],
    ids=["first failure", "failure beats running", "running test"],
)
def test_the_default_cursor_is_the_first_failed_section_else_the_running_one(runs, bench, case):
    view = in_log(runs, "host_suite", bench=bench)
    assert case in shown(view, runs, bench)


FOLD_RUNS = [run(host_suite=FAILED_SUITE, target_suite=suite("target_suite", ["passed", "passed"]))]
CASES = ("case_00", "case_01", "case_02")


@pytest.mark.parametrize(
    ("presses", "folds", "line"),
    [
        ("za", {sid("host_suite", "case_01"): False}, "▸"),
        ("zaza", {sid("host_suite", "case_01"): True}, "▾"),
        ("jza", {sid("host_suite", "case_01"): False}, "▸"),
        ("zM", {sid("host_suite", n): False for n in CASES}, None),
        ("zR", {sid("host_suite", n): True for n in CASES}, None),
    ],
    ids=["za on a header", "za twice", "za on a body line", "zM scoped to the stage", "zR"],
)
def test_fold_keys_set_toggles_for_the_selected_stage_only(presses, folds, line):
    view = in_log(FOLD_RUNS, "host_suite")
    keys(view, FOLD_RUNS, presses)
    assert view.folds == folds
    if line:
        assert shown(view, FOLD_RUNS).startswith(line)
        assert "case_01" in shown(view, FOLD_RUNS)


def test_a_discarded_pending_z_toggles_nothing():
    view = in_log(FOLD_RUNS, "host_suite")
    assert keys(view, FOLD_RUNS, "zqa") is False
    assert view.folds == {}


def test_a_toggle_survives_a_reparse_because_it_is_keyed_by_section_id():
    def build():
        return [run(host_suite=suite("host_suite", ["passed"] * 3, {0: ("body",)}))]

    runs = build()
    view = in_log(runs, "host_suite")
    keys(view, runs, "za")
    assert shown(view, runs).startswith("▾")
    reparsed = build()
    view.tick(reparsed, IDLE, SIZE)
    assert view.folds[sid("host_suite", "case_00")] is True
    assert shown(view, reparsed).startswith("▾")


def test_the_cursor_is_re_found_by_section_id_when_rows_arrive_before_it():
    view = in_log(RUNS, "host_suite")
    keys(view, RUNS, "jjjjj")
    assert "case_05" in shown(view, RUNS)
    names = ["fresh_a", "fresh_b", *(f"case_{i:02d}" for i in range(30))]
    grown = [run(host_suite=suite("host_suite", ["passed"] * 32, names=names))]
    view.tick(grown, IDLE, SIZE)
    assert "case_05" in shown(view, grown)


def test_a_cursor_on_a_line_folded_away_by_a_reparse_lands_on_its_header():
    running = suite("host_suite", ["open"], {0: ("x0", "x1")}, state="open")
    runs = [live("host_suite", host_suite=running)]
    view = in_log(runs, "host_suite", bench=LIVE)
    keys(view, runs, "j", bench=LIVE)
    assert shown(view, runs, LIVE) == "x0"
    after = [run(host_suite=suite("host_suite", ["passed"], {0: ("x0", "x1")}))]
    view.tick(after, IDLE, SIZE)
    assert "case_00" in shown(view, after)
    assert shown(view, after).startswith("▸")


@pytest.mark.parametrize(
    ("runs", "bench", "name", "follow"),
    [
        ([live("build")], LIVE, "build", True),
        ([failing_at(STAMP, "build")], IDLE, None, True),
        ([failing_at(STAMP, "bundle")], DISABLED, "bundle", None),
        ([run()], DISABLED, None, None),
        (
            [run(states=["passed", "passed", "open"] + ["not-started"] * 4, result=None)],
            DISABLED,
            "build",
            None,
        ),
    ],
    ids=["live", "idle", "disabled with a failure", "disabled clean", "disabled and stranded"],
)
def test_a_fresh_view_opens_on_the_stage_the_bench_state_names(runs, bench, name, follow):
    view = status.View()
    view.tick(runs, bench, SIZE)
    assert view.stage == name
    if follow is not None:
        assert view.follow is follow


def test_follow_rides_the_running_stage_and_ends_on_the_summary():
    view = status.View()
    view.tick([live("host_suite")], LIVE, SIZE)
    assert view.stage == "host_suite"
    view.tick([live("target_suite")], LIVE, SIZE)
    assert view.stage == "target_suite"
    view.tick([run()], IDLE, SIZE)
    assert view.stage is None


def test_follow_that_a_stage_movement_stopped_leaves_the_selection_alone():
    view = status.View()
    view.tick([live("host_suite")], LIVE, SIZE)
    keys(view, [growing(5)], ENTER, "j", ESC, bench=LIVE)
    assert view.follow is True
    keys(view, [live("host_suite")], "j", bench=LIVE)
    assert view.stage == "target_suite"
    view.tick([live("posted")], LIVE, SIZE)
    assert view.stage == "target_suite"
    view.tick([run()], IDLE, SIZE)
    assert view.stage == "target_suite"


def test_capital_g_in_the_log_keeps_the_cursor_on_the_last_row_as_rows_arrive():
    view = status.View()
    view.tick([growing(25)], LIVE, SIZE)
    keys(view, [growing(25)], ENTER, "G", bench=LIVE)
    view.tick([growing(28)], LIVE, SIZE)
    row, line = cursor(view, [growing(28)], LIVE)
    assert (row, "case_27" in line) == (21, True)


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
    target = suite("target_suite", ["failed", "passed", "open", "passed"], state="open")
    moved = live(
        "target_suite", host_suite=suite("host_suite", ["passed"] * 3), target_suite=target
    )
    view.tick([moved], LIVE, SIZE)
    assert view.stage == "target_suite"
    assert view.focus == "log"
    assert "case_03" in shown(view, [moved], LIVE)


PAST = [live("host_suite"), failing_at(OLDER, "build"), run(OLDEST)]


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
    keys(view, PAST, "]]", bench=LIVE)
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


@pytest.mark.parametrize(
    ("tab", "press", "expected"),
    [
        ("run", "h", "run"),
        ("run", "l", "history"),
        ("history", "l", "history"),
        ("history", "h", "run"),
    ],
)
def test_h_and_l_switch_between_run_and_history_without_wrapping(tab, press, expected):
    view = status.View()
    if tab == "history":
        keys(view, PAST, "3")
    keys(view, PAST, press)
    assert view.tab == expected


HISTORY = [
    runlog.Run(f"20261{i:03d}T000000Z", "run", "main", 1, t(0), "passed", t(5), ())
    for i in range(39, -1, -1)
]


@pytest.mark.parametrize(
    ("presses", "index"),
    [("jj", 2), ("jjkkk", 0), ("Gj", 39)],
)
def test_history_opens_on_the_newest_record_and_j_k_gg_g_move_it_and_clamp(presses, index):
    view = status.View()
    keys(view, HISTORY, "3", presses)
    assert view.tab == "history"
    assert view.history_record == HISTORY[index].record


@pytest.mark.parametrize(("size", "half"), [((24, 80), 10), ((40, 80), 18)])
def test_history_half_pages_move_by_half_of_rows_minus_four(size, half):
    view = status.View()
    keys(view, HISTORY, "3", CD, size=size)
    assert view.history_record == HISTORY[half].record
    keys(view, HISTORY, CU, size=size)
    assert view.history_record == HISTORY[0].record


def test_the_history_window_keeps_the_selected_record_on_screen():
    view = status.View()
    keys(view, HISTORY, "3", "G")
    shown_rows = draw(HISTORY, view=view)
    (row,) = [r for r in range(2, 22) if "selection" in shown_rows.style(r, 0)]
    assert shown_rows.text(row).startswith(HISTORY[39].record)
    keys(view, HISTORY, "gg")
    assert draw(HISTORY, view=view).text(2).startswith(HISTORY[0].record)


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


@pytest.mark.parametrize("press", ["j", "k", "1", "3", "h", "l", "/", "[", "]", ENTER, "G", CD])
def test_help_ignores_every_key_except_back_quit_and_colon(press):
    view = status.View()
    keys(view, PAST, "?")
    assert keys(view, PAST, press) is False
    assert (view.tab, view.prompt) == ("help", None)
    assert keys(view, PAST, "q") is True


QUITS = [("q",), ("3", "q"), (":q", ENTER), (":q", curses.KEY_ENTER)]


@pytest.mark.parametrize("presses", QUITS)
def test_q_and_colon_q_enter_quit(presses):
    assert keys(status.View(), PAST, *presses) is True


PROMPTS = [
    ((curses.KEY_RESIZE,), "run", None),
    (("?", ":"), "help", ":"),
    ((":", "q"), "run", ":q"),
    ((":q", "\x01"), "run", ":q"),
    ((":q", curses.KEY_UP), "run", ":q"),
    ((":q", "j"), "run", ":qj"),
    ((":run", ENTER), "run", None),
    ((":", ENTER), "run", None),
    ((":q", "\x7f"), "run", ":"),
    ((":q", "\b", "\b"), "run", None),
    ((":q", curses.KEY_BACKSPACE, curses.KEY_BACKSPACE), "run", None),
]


@pytest.mark.parametrize(("presses", "tab", "prompt"), PROMPTS)
def test_the_command_line_takes_text_and_acts_on_nothing_but_quit(presses, tab, prompt):
    view = status.View()
    assert keys(view, PAST, *presses) is False
    assert (view.tab, view.prompt) == (tab, prompt)


@pytest.mark.parametrize(
    ("setup", "presses", "prompt"),
    [
        (None, "/", None),
        ("history", "/", None),
        ("stage", "/ab", "/ab"),
        ("stage", ("/", ESC), None),
    ],
    ids=["no stage", "on History", "types", "Esc"],
)
def test_slash_opens_only_with_a_selected_stage_on_run(setup, presses, prompt):
    view = arrive(PAST, "build" if setup else None, bench=LIVE)
    if setup == "history":
        keys(view, PAST, "3", bench=LIVE)
    keys(view, PAST, *([presses] if isinstance(presses, str) else presses), bench=LIVE)
    assert view.prompt == prompt


NEEDLE = suite(
    "host_suite",
    ["passed"] * 30,
    {i: ("out 7 needle" if i == 7 else f"out {i}",) for i in range(30)},
    lead=("preamble text",),
)
NEEDLE_RUNS = [run(host_suite=NEEDLE)]


def test_search_into_a_folded_section_unfolds_it_and_takes_the_cursor_and_focus():
    view = arrive(NEEDLE_RUNS, "host_suite")
    keys(view, NEEDLE_RUNS, "/needle", ENTER)
    assert view.prompt is None
    assert view.focus == "log"
    assert view.folds[sid("host_suite", "case_07")] is True
    assert shown(view, NEEDLE_RUNS) == "out 7 needle"


@pytest.mark.parametrize(
    ("presses", "line"),
    [(("/case_12", ENTER), "case_12"), (("G", "/preamble", ENTER), "preamble text")],
    ids=["section header name", "wrap to the stage's own lines"],
)
def test_search_matches_a_section_header_and_wraps_to_leading_lines(presses, line):
    view = in_log(NEEDLE_RUNS, "host_suite")
    keys(view, NEEDLE_RUNS, *presses)
    assert line in shown(view, NEEDLE_RUNS)


HITS = run(host_suite=suite("host_suite", ["passed"] * 3, {i: (f"hit {i}",) for i in range(3)}))


def test_n_and_capital_n_step_through_matches_and_wrap_both_ways():
    runs = [HITS]
    view = in_log(runs, "host_suite")
    keys(view, runs, "/hit", ENTER)
    seen = [shown(view, runs)]
    for press in "nnnNNN":
        keys(view, runs, press)
        seen.append(shown(view, runs))
    assert seen == ["hit 0", "hit 1", "hit 2", "hit 0", "hit 2", "hit 1", "hit 0"]
    keys(view, runs, ESC)
    assert view.focus == "stages"
    keys(view, runs, "n")
    assert view.focus == "log"


@pytest.mark.parametrize("search", ["/HIT", "/"], ids=["case-sensitive miss", "empty"])
def test_a_search_that_finds_nothing_closes_the_prompt_and_moves_nothing(search):
    runs = [HITS]
    view = in_log(runs, "host_suite")
    keys(view, runs, "j")
    before = (shown(view, runs), dict(view.folds), view.focus, view.stage)
    keys(view, runs, search, ENTER)
    assert view.prompt is None
    assert (shown(view, runs), dict(view.folds), view.focus, view.stage) == before


def test_n_and_capital_n_do_nothing_without_a_selection_a_pattern_or_on_history():
    nothing = status.View()
    keys(nothing, [HITS], "nN")
    assert (nothing.stage, nothing.focus, nothing.folds) == (None, "stages", {})
    fresh = arrive([HITS], "host_suite")
    keys(fresh, [HITS], "nN")
    assert (fresh.folds, fresh.focus) == ({}, "stages")
    searched = in_log([HITS], "host_suite")
    keys(searched, [HITS], "/hit", ENTER, "3")
    record = searched.history_record
    keys(searched, [HITS], "nN")
    assert (searched.tab, searched.history_record) == ("history", record)


LONG = run(
    build=stage("build", "passed", t(3, 10), lines=tuple(f"line_{i:02d}" for i in range(60)))
)
LONG_RUNS = [LONG]


def test_a_plain_pane_opens_with_the_cursor_on_its_last_line_on_the_panes_last_row():
    view = arrive(LONG_RUNS, "build")
    assert cursor(view, LONG_RUNS) == (22, "line_59")


@pytest.mark.parametrize(
    ("presses", "row", "line"),
    [
        ("", 22, "line_59"),
        ("k", 21, "line_58"),
        ("gg", 3, "line_00"),
        ("ggj", 4, "line_01"),
        ("ggG", 22, "line_59"),
        ("/line_17\n", 3, "line_17"),
    ],
    ids=["default", "k", "gg", "j", "G", "search"],
)
def test_plain_pane_keys_move_the_line_cursor_and_keep_it_in_view(presses, row, line):
    view = in_log(LONG_RUNS, "build")
    keys(view, LONG_RUNS, presses)
    assert cursor(view, LONG_RUNS) == (row, line)


def test_a_plain_pane_shorter_than_the_pane_stays_bottom_aligned_under_its_label():
    runs = [run(build=stage("build", "passed", t(3, 10), lines=("a", "b", "c", "d", "e")))]
    view = arrive(runs, "build")
    assert cursor(view, runs) == (22, "e")
    shown_rows = draw(runs, view=view)
    assert shown_rows.right(17) == "this stage's output"
    assert shown_rows.right(18) == "a"


def build_lines(count):
    lines = tuple(f"line_{i:02d}" for i in range(count))
    return live("build", build=stage("build", "open", t(3, 10), lines=lines))


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


def build_prefix(*, build_open=False):
    """`run started` through the build stage, as `BenchTest.run` writes it, with kas output."""
    out = mark("run started kind=run ref=main pr=7", 0)
    for minute, name in enumerate(("checkout", "precondition"), 1):
        out += mark(f"stage {name} started", minute) + mark(f"stage {name} passed", minute, 30)
    out += mark("stage build started", 3) + "".join(f"NOTE: build step {i}\n" for i in range(40))
    if not build_open:
        out += mark("stage build passed", 20)
        out += mark("stage bundle started", 21) + mark("stage bundle passed", 22)
    return out


FAILURE = {
    "reprcrash": {"path": "/w/t.py", "lineno": 12, "message": "assert 1 == 2"},
    "reprtraceback": {
        "reprentries": [{"type": "ReprEntry", "data": {"lines": [">   assert 1 == 2"]}}]
    },
    "sections": [],
}
TESTS = tuple(f"test_{i:02d}" for i in range(30))


def suite_stage(name, tests, started_at, running=None, failed=None):
    """A suite stage whose tests are queued, the ones before `running` finished."""
    out = mark(f"stage {name} started", started_at)
    out += "".join(mark(f"test queued {name}.py::{test}", started_at, 1) for test in tests)
    reports = []
    for position, test in enumerate(tests):
        nodeid = f"{name}.py::{test}"
        if running is not None and position > running:
            break
        out += mark(f"test started {nodeid}", started_at, 2 + position)
        out += f"output of {test}\n"
        if position == running:
            reports.append(rep(nodeid, "setup", "passed"))
            break
        bad = position == failed
        reports += finished(nodeid, "failed" if bad else "passed", FAILURE if bad else None)
    return out, reports


def test_following_from_the_build_into_a_long_suite_puts_the_running_test_on_screen():
    view = status.View()
    building = runlog.parse_run(STAMP, build_prefix(build_open=True), {})
    view.tick([building], LIVE, SIZE)
    assert view.stage == "build"

    text, reports = suite_stage("host_suite", TESTS, 23, running=1)
    host = runlog.parse_run(STAMP, build_prefix() + text, {"host_suite": reports})
    view.tick([host], LIVE, SIZE)

    assert view.stage == "host_suite"
    assert "test_01" in shown(view, [host], LIVE)


def test_history_enter_on_a_failed_past_run_puts_its_failed_test_on_screen():
    building = runlog.parse_run(STAMP, build_prefix(build_open=True), {})
    text, reports = suite_stage("host_suite", TESTS[:25], 23, failed=3)
    text += mark("stage host_suite failed reason=host_suite failed", 40) + mark("run failed", 41)
    past = runlog.parse_run(OLDER, build_prefix() + text, {"host_suite": reports})
    runs = [building, past]
    view = status.View()
    view.tick(runs, LIVE, SIZE)
    assert view.stage == "build"

    keys(view, runs, "3", "j", ENTER, bench=LIVE)

    assert view.tab == "run"
    assert OLDER in tab_line(view, runs, LIVE)
    assert "test_03" in shown(view, runs, LIVE)
