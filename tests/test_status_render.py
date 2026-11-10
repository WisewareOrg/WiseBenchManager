"""`wisebench.screen.render`: a run model, a view and a clock drawn as styled screen rows."""

from datetime import timedelta

import pytest
from status_helpers import (
    DISABLED,
    IDLE,
    LIVE,
    STAMP,
    draw,
    real_run,
    run,
    section,
    stage,
    t,
)

from wisebench import bench_state, runlog
from wisebench.screen import LEFT, RIGHT

pytestmark = pytest.mark.usefixtures("local_zone")


def lc(hour, minute=0, second=0):
    """The `HH:MM:SS` a UTC time reads as in the pinned zone, three hours ahead of UTC."""
    return f"{(hour + 3) % 24:02d}:{minute:02d}:{second:02d}"


# ---------------------------------------------------------------- layout


def test_the_stage_list_rule_and_log_pane_sit_at_the_canvas_columns():
    """The canvas of record puts the rule at column 39 and the right pane at column 42."""
    live = run(result=None, build=stage("build", "open", t(3, 0, 15), lines=["marker"]))
    shown = draw([live], LIVE, stage="build")
    assert shown.text(3).index("│") == 39
    assert shown.text(22).index("marker") == 42


# ---------------------------------------------------------------- tab line and status bar


def test_the_tab_line_names_the_active_tab_in_inverse_and_a_past_runs_record():
    """Row 0 has four forms: the run tab, the history tab, help (none active), a past run."""
    newest, older = run(), run(record="20261109T030715Z")
    clock = lc(10, 7, 30)

    on_run = draw([newest, older])
    assert on_run.text(0) == "bench   1 Run  3 History ".ljust(80 - len(clock)) + clock
    assert {on_run.style(0, c) for c in range(7, 14)} == {frozenset({"inverse"})}
    assert not any("inverse" in on_run.style(0, c) for c in range(14, 80))

    on_history = draw([newest, older], tab="history")
    assert not any("inverse" in on_history.style(0, c) for c in range(7, 14))
    assert {on_history.style(0, c) for c in range(14, 25)} == {frozenset({"inverse"})}

    on_help = draw([newest, older], tab="help")
    assert on_help.text(0, 0, 25) == "bench   1 Run  3 History "
    assert not any("inverse" in on_help.style(0, c) for c in range(80))

    past = draw([newest, older], record=older.record)
    assert past.text(0, 7, 14 + len(older.record) + 2) == f" 1 Run  {older.record} "
    assert {past.style(0, c) for c in range(7, 7 + 9 + len(older.record))} == {
        frozenset({"inverse"})
    }


@pytest.mark.parametrize(
    ("bench", "badge", "ground"),
    [
        (bench_state.Bench(True, False, "on"), " RUNNING ", "badge_running"),
        (bench_state.Bench(False, True, "on"), " DISABLED ", "badge_disabled"),
        (bench_state.Bench(False, False, "on"), " IDLE ", "badge_idle"),
        (bench_state.Bench(True, True, "on"), " RUNNING ", "badge_running"),
    ],
)
def test_the_status_bar_badge_is_running_over_disabled_over_idle(bench, badge, ground):
    shown = draw([run()], bench)
    assert shown.text(23).startswith(f"{badge}  timer on")
    assert {shown.style(23, c) for c in range(len(badge))} == {frozenset({"bold", ground})}
    assert shown.text(23).endswith("? keys")


@pytest.mark.parametrize(("timer", "tone"), [("on", "green"), ("off", "red"), ("—", "grey")])
def test_the_status_bar_timer_word_is_green_red_or_grey(timer, tone):
    shown = draw([run()], bench_state.Bench(False, False, timer))
    start = shown.text(23).index("timer") + len("timer ")
    assert shown.text(23)[start : start + len(timer)] == timer
    assert shown.style(23, start) == frozenset({tone})


def test_an_open_prompt_takes_the_last_row_and_moves_the_status_bar_up_one():
    runs = [run()]
    closed = draw(runs, tab="history", directory="records")
    assert closed.text(23).startswith(" IDLE ")
    assert closed.text(22).startswith("1 records in records/")

    for typed in (":q", "/abc"):
        opened = draw(runs, tab="history", directory="records", prompt=typed)
        assert opened.text(23).startswith(f"{typed} ")
        assert opened.style(23, len(typed)) == frozenset({"inverse"})
        assert opened.text(22).startswith(" IDLE ")
        assert opened.text(21).startswith("1 records in records/")


# ---------------------------------------------------------------- Run header and stage list


def test_the_run_header_has_four_forms_and_a_missing_sha_reads_a_dash():
    newest = run()
    past = run(
        record="20261028T081502Z",
        pr=28,
        result="failed",
        ended=t(6, 16, 40),
        sha="77aa12c0000000000000000000000000000000ff",
    )
    base = f"PR #31  a1b2c3d  record {STAMP}"

    assert draw([newest], LIVE).text(1).rstrip() == base
    assert draw([newest], IDLE).text(1).rstrip() == f"last run  {base}"
    assert draw([newest], DISABLED).text(1).rstrip() == base
    other = draw([newest, past], LIVE, record=past.record)
    assert other.text(1).rstrip() == (
        f"PR #28  77aa12c  record {past.record}  failed  ended {lc(6, 16, 40)}"
    )
    bare = run(kind="test", pr=None, ref="feature/x", sha=None, result=None)
    assert draw([bare], LIVE).text(1).rstrip() == f"feature/x  —  record {STAMP}"


STATES = run(
    result=None,
    checkout=stage("checkout", "passed", t(3, 0, 9), t(3, 0, 11)),
    precondition=stage("precondition", "error", t(3, 0, 11), t(3, 0, 15)),
    build=stage("build", "open", t(3, 0, 15)),
    bundle=stage("bundle", "not-started"),
    host_suite=stage("host_suite", "skipped"),
    target_suite=stage("target_suite", "failed", t(3, 20, 0), t(3, 25, 9)),
    posted=stage("posted", "passed", t(3, 25, 20), t(3, 25, 22)),
)


def test_a_stage_row_is_glyph_name_start_and_took_under_a_column_header():
    shown = draw([STATES], LIVE, now=t(3, 12, 15))
    assert shown.text(2, 0, LEFT).rstrip() == "stage            start     took"
    left = [shown.text(r, 0, LEFT).rstrip() for r in range(3, 10)]
    assert left[0] == f"{'✓ checkout':<17}{lc(3, 0, 9)}  0m02s"
    assert left[1] == f"{'✗ precondition':<17}{lc(3, 0, 11)}  0m04s"
    assert left[2] == f"{'● build':<17}{lc(3, 0, 15)}  12m00s"
    assert left[3] == "· bundle"
    assert left[4].startswith("– host suite")
    assert left[5] == f"{'✗ target suite':<17}{lc(3, 20, 0)}  5m09s"
    assert left[6] == f"{'✓ posted':<17}{lc(3, 25, 20)}  0m02s"


@pytest.mark.parametrize(
    ("row", "tone"),
    [
        (3, {"green"}),
        (4, {"red", "bold"}),
        (5, {"amber", "bold"}),
        (6, {"grey"}),
        (7, {"grey"}),
        (8, {"red", "bold"}),
    ],
    ids=["passed", "error", "open", "not-started", "skipped", "failed"],
)
def test_each_stage_state_has_its_glyph_colour(row, tone):
    shown = draw([STATES], LIVE, now=t(3, 12, 15))
    assert shown.tone(row, 0) == frozenset(tone)


def test_a_tinted_stage_row_keeps_its_tint_when_selected_and_a_plain_one_takes_the_selection():
    runs = [
        run(
            result=None,
            build=stage("build", "open", t(3, 0, 15)),
            target_suite=stage("target_suite", "failed", t(3, 20, 0), t(3, 25, 9)),
        )
    ]

    def ground(row, **view):
        shown = draw(runs, LIVE, now=t(3, 12, 15), **view)
        cells = {frozenset(shown.style(row, c) & {"running_tint", "failed_tint", "selection"})
                 for c in range(LEFT)}  # fmt: skip
        assert len(cells) == 1, "the ground covers the pane's whole row span"
        assert not shown.style(row, LEFT) & {"running_tint", "failed_tint", "selection"}
        return next(iter(cells))

    assert ground(3) == frozenset()
    assert ground(5) == {"running_tint"}
    assert ground(8) == {"failed_tint"}
    assert ground(3, stage="checkout") == {"selection"}
    assert ground(5, stage="build") == {"running_tint"}
    assert ground(8, stage="target_suite") == {"failed_tint"}


def test_durations_are_unbounded_minutes_and_a_stage_left_open_in_a_dead_run_reads_question_dash():
    long = stage("bundle", "passed", t(3, 5, 0), t(4, 7, 5))
    stuck = stage("build", "open", t(3, 0, 15))
    shown = draw([run(kind="test", result=None, build=stuck, bundle=long)], IDLE)
    assert shown.text(5, 0, LEFT).rstrip() == f"{'? build':<17}{lc(3, 0, 15)}  —"
    assert shown.text(6, 0, LEFT).rstrip() == f"{'✓ bundle':<17}{lc(3, 5, 0)}  62m05s"


# ---------------------------------------------------------------- Run right pane


def target_suite_open():
    name = "target_suite"
    sections = (
        section(name, "setup", "install_candidate", "passed", t(3, 31, 12), ["put bundle"]),
        section(name, "test", "t.py::a", "passed", t(3, 34, 52), ["a out"]),
        section(name, "test", "t.py::b", "open", t(3, 36, 34), ["reading rauc"]),
        section(name, "test", "t.py::c", "not-started"),
    )
    open_stage = stage(name, "open", t(3, 31, 12), sections=sections)
    return run(result=None, target_suite=open_stage), sections


def test_a_sectioned_pane_has_a_summary_derived_from_its_tests_fold_glyphs_and_a_hint():
    live, _ = target_suite_open()
    shown = draw([live], LIVE, size=(24, 120), now=t(3, 37, 38), stage="target_suite")
    assert shown.right(2) == "target suite  2 of 3  1 passed  0 failed"
    assert shown.right(3) == "▸ ✓ setup install_candidate  3m40s"
    assert shown.right(4) == "▸ ✓ t.py::a  1m42s"
    assert shown.right(5) == "▾ ● t.py::b  1m04s"
    assert shown.right(6) == "      reading rauc"
    assert shown.right(7) == "  · t.py::c"
    assert shown.right(22) == "Enter into the log   za fold   Esc back to stages"
    assert shown.tone(3, RIGHT + 4) == frozenset({"blue"})


def test_a_view_fold_toggle_overrides_the_derived_fold_state():
    live, sections = target_suite_open()
    shown = draw(
        [live],
        LIVE,
        size=(24, 120),
        now=t(3, 37, 38),
        stage="target_suite",
        folds={sections[0].id: True, sections[2].id: False},
    )
    assert shown.right(3) == "▾ ✓ setup install_candidate  3m40s"
    assert shown.right(4) == "      put bundle"
    assert shown.right(5) == "▸ ✓ t.py::a  1m42s"
    assert shown.right(6) == "▸ ● t.py::b  1m04s"
    assert shown.right(7) == "  · t.py::c"


def test_a_failed_test_opens_with_its_output_and_failure_lines_after_the_stages_own_lines():
    name = "target_suite"
    sections = (
        section(name, "test", "t.py::a", "passed", t(3, 40, 0)),
        section(
            name,
            "test",
            "t.py::b",
            "failed",
            t(3, 50, 0),
            ["out line"],
            ["t.py:88: AssertionError", "E   boom"],
        ),
    )
    failed = stage(
        name, "failed", t(3, 31, 12), t(3, 51, 0), lines=["lead line"], sections=sections
    )
    shown = draw([run(result="failed", target_suite=failed)], IDLE, size=(24, 120), stage=name)
    assert shown.right(2) == "target suite  2 of 2  1 passed  1 failed"
    assert shown.right(3) == "lead line"
    assert shown.right(4) == "  ✓ t.py::a  10m00s"
    assert shown.right(5) == "▾ ✗ t.py::b  1m00s"
    assert shown.right(6) == "      out line"
    assert shown.right(7) == "      t.py:88: AssertionError"
    assert shown.right(8) == "      E   boom"


def test_a_suite_left_open_in_a_dead_run_reads_grey_and_its_last_test_has_no_duration():
    live, _ = target_suite_open()
    shown = draw([live], IDLE, size=(24, 120), now=t(3, 37, 38), stage="target_suite")
    assert shown.tone(2, RIGHT) == frozenset({"grey", "bold"})
    assert shown.right(5) == "▾ ? t.py::b  —"
    assert draw([live], LIVE, size=(24, 120), now=t(3, 37, 38), stage="target_suite").tone(
        2, RIGHT
    ) == frozenset({"amber", "bold"})


def test_the_last_test_of_a_stage_with_no_end_reads_a_dash_for_its_duration():
    name = "target_suite"
    last = section(name, "test", "t.py::a", "passed", t(3, 34, 52))
    stranded = stage(name, "error", t(3, 31, 12), None, sections=[last])
    shown = draw([run(result="error", target_suite=stranded)], IDLE, stage=name, size=(24, 120))
    assert shown.right(3) == "  ✓ t.py::a  —"


def test_a_plain_pane_is_a_label_over_bottom_aligned_lines_clipped_at_the_pane_edge():
    lines = ["NOTE: one", "NOTE: two", "x" * 60]
    live = run(result=None, build=stage("build", "open", t(3, 0, 15), lines=lines))
    shown = draw([live], LIVE, stage="build")
    assert shown.right(19) == "this stage's output"
    assert shown.right(20) == "NOTE: one"
    assert shown.right(21) == "NOTE: two"
    assert shown.text(22, RIGHT) == "x" * (80 - RIGHT)
    assert [shown.right(r) for r in range(2, 19)] == [""] * 17
    pane = "".join(shown.right(r) for r in range(2, 23))
    assert not any(glyph in pane for glyph in "▾▸") and "Enter into" not in pane

    waiting = draw([run(bundle=stage("bundle", "not-started"))], IDLE, stage="bundle")
    assert [waiting.right(r) for r in range(2, 23) if waiting.right(r)] == ["this stage's output"]


def build_progress(tasks):
    return runlog.BuildProgress(1204, 3311, tuple(tasks))


def test_the_build_header_floors_the_percent_rounds_the_bar_and_lists_running_tasks():
    tasks = [
        runlog.Task("curl", "do_configure", t(3, 11, 56)),
        runlog.Task("systemd", "do_compile", None),
        runlog.Task("linux-yocto", "do_compile", t(3, 8, 5)),
    ]
    building = stage(
        "build",
        "open",
        t(3, 0, 15),
        lines=["NOTE: Running task 1204 of 3311 (linux-yocto_6.6.bb:do_compile)"],
        progress=build_progress(tasks),
    )
    shown = draw(
        [run(result=None, build=building)], LIVE, size=(24, 120), now=t(3, 12, 15), stage="build"
    )
    assert shown.right(2) == "build  task 1204 of 3311  36%"
    assert shown.text(3, RIGHT, RIGHT + 33) == "█" * 33
    assert {shown.tone(3, RIGHT + c) for c in range(12)} == {frozenset({"amber"})}
    assert {shown.tone(3, RIGHT + c) for c in range(12, 33)} == {frozenset({"bar_empty"})}
    assert shown.right(4) == "running now"
    assert shown.right(5) == f"{'linux-yocto':<14}{'do_compile':<14}4m10s"
    assert shown.right(6) == f"{'curl':<14}{'do_configure':<14}0m19s"
    assert shown.right(7) == f"{'systemd':<14}{'do_compile':<14}—"
    assert shown.right(21) == "this stage's output"
    assert shown.right(22).startswith("NOTE: Running task 1204 of 3311")


def test_running_now_is_hidden_when_the_build_is_not_live_and_capped_to_leave_three_output_rows():
    many = [runlog.Task(f"r{i:02d}", "do_compile", t(3, 1, i)) for i in range(20)]
    output = ["l1", "l2", "l3"]
    building = stage("build", "open", t(3, 0, 15), lines=output, progress=build_progress(many))
    live = draw(
        [run(result=None, build=building)], LIVE, size=(24, 120), now=t(3, 12, 15), stage="build"
    )
    assert [live.right(r)[:3] for r in range(5, 19)] == [f"r{i:02d}" for i in range(14)]
    assert live.right(19) == "this stage's output"
    assert [live.right(r) for r in (20, 21, 22)] == output

    done = stage("build", "passed", t(3, 0, 15), t(3, 40, 0), progress=build_progress(many))
    past = draw([run(build=done)], IDLE, size=(24, 120), stage="build")
    pane = [past.right(r) for r in range(2, 23)]
    assert pane[0] == "build  task 1204 of 3311  36%"
    assert past.text(3, RIGHT, RIGHT + 33) == "█" * 33
    assert not any("running now" in line or "do_compile" in line for line in pane)


def test_the_summary_shows_the_result_word_end_time_floored_minutes_and_one_row_per_suite():
    def tests(name, outcomes):
        return stage(
            name,
            "failed" if "failed" in outcomes else "passed",
            sections=[section(name, "test", f"t{i}", o) for i, o in enumerate(outcomes)],
        )

    finished = run(
        kind="test",
        result="failed",
        started=t(3, 1, 0),
        ended=t(4, 3, 59),
        host_suite=tests("host_suite", ["passed"] * 2),
        target_suite=tests("target_suite", ["passed"] * 6 + ["failed", "skipped"]),
    )
    shown = draw([finished], IDLE)
    assert shown.right(2) == f"failed  ended {lc(4, 3, 59)}, took 62m"
    assert shown.right(3) == "host suite    2 passed"
    assert shown.right(4) == "target suite  6 passed  1 failed"
    assert shown.right(5) == ""

    clean = draw([run(kind="test", host_suite=tests("host_suite", ["passed"] * 2))], IDLE)
    assert clean.right(3) == "host suite    2 passed"
    assert clean.right(4) == ""


# ---------------------------------------------------------------- result colours


def test_a_failed_run_reads_red_a_passed_one_green_and_a_running_one_amber():
    summary = {
        "failed": draw([run(result="failed")], IDLE).tone(2, RIGHT),
        "passed": draw([run()], IDLE).tone(2, RIGHT),
        "running": draw([run(result=None)], LIVE).tone(2, RIGHT),
    }
    assert summary == {
        "failed": frozenset({"red", "bold"}),
        "passed": frozenset({"green", "bold"}),
        "running": frozenset({"amber", "bold"}),
    }
    history = {
        "failed": draw([run(result="failed")], IDLE, tab="history").tone(2, 49),
        "passed": draw([run()], IDLE, tab="history").tone(2, 49),
        "running": draw([run(result=None)], LIVE, tab="history").tone(2, 49),
    }
    assert history == {
        "failed": frozenset({"red"}),
        "passed": frozenset({"green"}),
        "running": frozenset({"amber"}),
    }


# ---------------------------------------------------------------- History


def hist(record, ref="main", pr=31, kind="run", result="passed", minutes=58, fail=None, state=""):
    """A run lasting `minutes` whole minutes plus 59 s; `fail` names its failing stage."""
    replaced = {fail: stage(fail, state or "failed")} if fail else {}
    return run(
        record=record,
        ref=ref,
        pr=pr,
        kind=kind,
        result=result,
        ended=t(3, 0, 0) + timedelta(minutes=minutes, seconds=59) if result else None,
        **replaced,
    )


def test_a_history_row_is_exactly_eighty_cells_with_a_four_cell_took_and_a_header_and_footer():
    only = hist(STAMP, result="failed", minutes=999, fail="target_suite")
    other = hist("20261109T030715Z")
    shown = draw([only, other], IDLE, tab="history", directory="records")
    assert shown.text(1).rstrip() == (
        "record".ljust(18) + "what".ljust(22) + "sha".ljust(9) + "result".ljust(13) + "took"
    )
    assert shown.text(2) == (
        STAMP.ljust(18) + "PR #31".ljust(22) + "a1b2c3d".ljust(9) + "failed".ljust(13)
        + "999m" + "  " + "target suite"
    )  # fmt: skip
    assert shown.text(3, 62, 66) == " 58m"
    assert shown.text(22).rstrip() == "2 records in records/"


def test_history_results_took_and_the_trailing_column_in_each_case():
    now = t(4, 0, 0)
    running = run(record="20261110T030715Z", result=None, started=t(3, 22, 0))
    runs = [
        running,
        hist("20261109T030715Z"),
        hist("20261108T030715Z", result="error", minutes=14, fail="precondition", state="error"),
        run(record="20261107T030715Z", result=None),
        hist("20261106T030715Z", kind="test", ref="feature/rauc-retry", pr=None),
        hist(
            "20261105T030715Z", kind="test", ref="main", pr=None, result="failed", fail="host_suite"
        ),
    ]
    shown = draw(runs, LIVE, tab="history", size=(24, 120), now=now)
    for index, (result, took, trailing) in enumerate(
        [
            ("running", " 38m", ""),
            ("passed", " 58m", ""),
            ("bench error", " 14m", "precondition"),
            ("no result", "   —", ""),
            ("passed", " 58m", "by hand"),
            ("failed", " 58m", "host suite  by hand"),
        ]
    ):
        row = shown.text(2 + index)
        assert row[49:62].rstrip() == result, row
        assert row[62:66] == took, row
        assert row[68:].rstrip() == trailing, row
    assert shown.text(6)[18:40].rstrip() == "feature/rauc-retry"


def test_the_record_column_widens_for_a_suffixed_id_and_the_what_column_clips_at_twenty():
    long_ref = "feature/" + "r" * 22
    first = hist(
        "20261110T030715Z-2", ref=long_ref, pr=None, kind="test", result="failed",
        fail="target_suite",
    )  # fmt: skip
    second = hist("20261110T030715Z")
    shown = draw([first, second], IDLE, tab="history", history_record=first.record)
    assert shown.text(1, 0, 20) == "record".ljust(20)
    assert shown.text(2, 0, 42) == first.record.ljust(20) + long_ref[:20] + "  "
    assert shown.text(2, 42, 51) == "a1b2c3d".ljust(9)
    assert shown.text(3, 0, 20) == second.record.ljust(20)
    assert {"selection" in shown.style(2, c) for c in range(80)} == {True}
    assert {"selection" in shown.style(3, c) for c in range(80)} == {False}


# ---------------------------------------------------------------- Help, empty, text


def test_help_lists_the_built_keys_in_three_columns_and_none_of_the_unbuilt_commands():
    shown = draw([run()], IDLE, tab="help", size=(24, 120))
    width = (120 - 6) // 3
    starts = (0, width + 3, 2 * (width + 3))
    assert shown.text(1).rstrip() == "keys"
    for start, heading in zip(starts, ("move", "runs and log", "commands"), strict=True):
        assert shown.text(2, start, start + width).rstrip() == heading
    expected = (
        [
            ("h l", "prev / next view"),
            ("1 3", "jump to view"),
            ("j k", "down / up"),
            ("gg G", "top / bottom"),
            ("^d ^u", "half page"),
            ("Enter", "open selection"),
        ],
        [
            ("[ ]", "older / newer run"),
            ("1", "back to the live run"),
            ("za", "fold / unfold a stage"),
            ("zR zM", "unfold / fold all"),
            ("/", "search"),
            ("n N", "next / prev match"),
            ("G", "follow the live log"),
        ],
        [(":q", "quit"), ("?", "this screen"), ("q", "quit")],
    )
    for start, entries in zip(starts, expected, strict=True):
        for offset, (key, words) in enumerate(entries):
            assert shown.text(3 + offset, start, start + width).rstrip() == f"{key:<10}{words}"
    body = "".join(shown.text(r) for r in range(3, 22))
    assert not any(gone in body for gone in ("dd ", ":run", ":disable", ":enable", ":edit", "1-4"))


def test_with_no_runs_run_reads_a_zero_record_line_and_history_its_header_and_footer():
    run_view = draw([], IDLE, directory="records")
    assert run_view.text(1).rstrip() == "0 records in records/"
    assert [run_view.text(r).strip() for r in range(2, 23)] == [""] * 21

    history = draw([], IDLE, tab="history", directory="records")
    assert history.text(1).startswith("record")
    assert history.text(22).rstrip() == "0 records in records/"
    assert [history.text(r).strip() for r in range(2, 22)] == [""] * 20


def test_tabs_expand_to_eight_column_stops_and_other_controls_become_caret_pairs():
    pane = 80 - RIGHT
    odd = "x" * (pane - 1) + "\x1b"
    lines = ["a\tb", "esc\x1b[0m", "del\x7f", "\x01", odd]
    live = run(result=None, build=stage("build", "open", t(3, 0, 15), lines=lines))
    shown = draw([live], LIVE, stage="build")
    assert shown.right(17) == "this stage's output"
    assert shown.right(18) == "a       b"
    assert shown.right(19) == "esc^[[0m"
    assert shown.right(20) == "del^?"
    assert shown.right(21) == "^A"
    assert shown.right(22) == "x" * (pane - 1) + "^"


# ---------------------------------------------------------------- size


@pytest.mark.parametrize(("rows", "cols"), [(24, 79), (23, 80)])
def test_a_terminal_below_eighty_by_twenty_four_draws_one_line_and_blank_rows(rows, cols):
    shown = draw([run()], LIVE, size=(rows, cols))
    assert shown.text(0) == "terminal too small".ljust(cols)
    assert [shown.text(r) for r in range(1, rows)] == [" " * cols] * (rows - 1)


def test_a_long_ref_is_clipped_to_the_eighty_columns_in_every_tab():
    long_ref = "z" * 90
    runs = [run(ref=long_ref, pr=None, kind="test")]
    for tab in ("run", "history", "help"):
        assert "terminal too small" not in draw(runs, LIVE, tab=tab).text(0)
    assert draw(runs, LIVE).text(1) == (long_ref + "  a1b2c3d").ljust(80)[:80]


# ---------------------------------------------------------------- a parsed real log


def test_a_parsed_real_log_draws_its_stage_list_and_suite_summaries():
    parsed = real_run()
    shown = draw([parsed], IDLE, now=t(4, 0, 0), size=(24, 120))
    assert [shown.text(r, 2, 14).strip() for r in range(3, 9)] == [
        "checkout",
        "precondition",
        "build",
        "bundle",
        "host suite",
        "target suite",
    ]
    assert shown.right(2) == f"passed  ended {lc(3, 17, 28)}, took 10m"
    assert shown.right(3) == "host suite    2 passed"
    assert shown.right(4) == "target suite  4 passed"
    assert draw([parsed], IDLE, stage="target_suite", size=(24, 120)).right(2) == (
        "target suite  4 of 4  4 passed  0 failed"
    )


def test_every_screen_the_module_draws_is_exactly_the_requested_width():
    for tab in ("run", "history", "help"):
        for size in ((24, 80), (30, 100), (24, 200)):
            draw([run(), run(record="20261109T030715Z")], LIVE, size=size, tab=tab)
