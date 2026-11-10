"""`wisebench.status.render`: a run model, a view and a clock drawn as styled screen rows."""

import curses
import time
from datetime import UTC, datetime, timedelta

import pytest

from wisebench import status

STAMP = "20261110T030715Z"
SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
NAMES = ("checkout", "precondition", "build", "bundle", "host_suite", "target_suite", "posted")
SHOWN = {"host_suite": "host suite", "target_suite": "target suite"}
TINTS = {"running_tint", "failed_tint", "selection"}
LEFT = 39
RIGHT = 45
LIVE = status.Bench(live=True, disabled=False, timer="on")
IDLE = status.Bench(live=False, disabled=False, timer="on")
DISABLED = status.Bench(live=False, disabled=True, timer="off")


def t(hour, minute=0, second=0):
    """2026-11-10 at the given UTC time."""
    return datetime(2026, 11, 10, hour, minute, second, tzinfo=UTC)


NOW = t(10, 7, 30)
RUN_START = t(3, 0, 0)
RUN_END = t(4, 2, 0)


def lc(hour, minute=0, second=0):
    """The `HH:MM:SS` a UTC time reads as in the pinned zone, three hours ahead of UTC."""
    return f"{(hour + 3) % 24:02d}:{minute:02d}:{second:02d}"


@pytest.fixture(autouse=True)
def local_zone(monkeypatch):
    """Pin the process time zone to UTC+3 so local clock text is deterministic."""
    monkeypatch.setenv("TZ", "ABC-3")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def stage(
    name,
    state="passed",
    started=None,
    ended=None,
    sha=None,
    lines=(),
    sections=(),
    progress=None,
    done=0,
    queued=0,
    passed=0,
    failed=0,
):
    return status.Stage(
        name, state, started, ended, sha, tuple(lines), tuple(sections), progress,
        done, queued, passed, failed,
    )  # fmt: skip


def section(stage_name, kind, name, outcome, started=None, lines=(), failure=()):
    return status.Section(
        (stage_name, kind, name, 0), kind, name, outcome, started, tuple(lines), tuple(failure)
    )


def run(
    record=STAMP,
    kind="run",
    ref="main",
    pr=31,
    started=RUN_START,
    result="passed",
    ended=RUN_END,
    sha=SHA,
    **replaced,
):
    """A run whose stages all passed, one every five minutes; `replaced` swaps stages by name."""
    names = NAMES if kind == "run" else NAMES[:-1]
    stages = []
    for index, name in enumerate(names):
        begin = t(3, 5 * index, 0)
        built = stage(
            name, "passed", begin, begin + timedelta(seconds=2), sha if name == "checkout" else None
        )
        stages.append(replaced.get(name, built))
    return status.Run(record, kind, ref, pr, started, result, ended, tuple(stages))


class Screen:
    """A rendered screen expanded to per-cell characters and styles."""

    def __init__(self, rows, cols):
        assert len(rows) > 0
        self.cells = []
        for row in rows:
            cells = []
            for text, style in row:
                assert isinstance(style, frozenset), style
                cells.extend((ch, style) for ch in text)
            assert len(cells) == cols
            self.cells.append(cells)

    def __len__(self):
        return len(self.cells)

    def text(self, row, start=0, end=None):
        return "".join(ch for ch, _ in self.cells[row][start:end])

    def style(self, row, col):
        return self.cells[row][col][1]

    def tone(self, row, col):
        """The style at a cell without tint or selection ground."""
        return self.style(row, col) - TINTS

    def right(self, row):
        return self.text(row, RIGHT).rstrip()


def draw(runs, bench=IDLE, rows=24, cols=80, now=NOW, **view):
    screen = status.render(status.View(**view), runs, bench, rows, cols, now)
    assert len(screen) == rows
    return Screen(screen, cols)


# ---------------------------------------------------------------- tab line and status bar


def test_the_tab_line_names_the_active_tab_in_inverse_and_a_past_runs_record():
    """Row 0 has four forms: the run tab, the history tab, help (none active), a past run."""
    newest, older = run(), run(record="20261109T030715Z")
    clock = lc(10, 7, 30)

    on_run = draw([newest, older])
    assert on_run.text(0) == "bench   1 Run  3 History ".ljust(80 - len(clock)) + clock
    assert on_run.style(0, 0) == frozenset({"bold"})
    assert {on_run.style(0, c) for c in range(7, 14)} == {frozenset({"inverse"})}
    assert {on_run.style(0, c) for c in range(14, 25)} == {frozenset({"grey"})}
    assert {on_run.style(0, c) for c in range(80 - len(clock), 80)} == {frozenset({"grey"})}

    on_history = draw([newest, older], tab="history")
    assert {on_history.style(0, c) for c in range(7, 14)} == {frozenset({"grey"})}
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
        (status.Bench(True, False, "on"), " RUNNING ", "badge_running"),
        (status.Bench(False, True, "on"), " DISABLED ", "badge_disabled"),
        (status.Bench(False, False, "on"), " IDLE ", "badge_idle"),
        (status.Bench(True, True, "on"), " RUNNING ", "badge_running"),
    ],
)
def test_the_status_bar_badge_is_running_over_disabled_over_idle(bench, badge, ground):
    shown = draw([run()], bench)
    assert shown.text(23).startswith(f"{badge}  timer on")
    assert {shown.style(23, c) for c in range(len(badge))} == {frozenset({"bold", ground})}


@pytest.mark.parametrize(("timer", "tone"), [("on", "green"), ("off", "red"), ("—", "grey")])
def test_the_status_bar_timer_word_is_green_red_or_grey_and_keys_sits_at_the_right(timer, tone):
    shown = draw([run()], status.Bench(False, False, timer))
    bar = shown.text(23)
    start = bar.index("timer")
    assert bar[start : start + 6 + len(timer)] == f"timer {timer}"
    assert shown.style(23, start) == frozenset({"grey"})
    assert shown.style(23, start + 6) == frozenset({tone})
    assert bar.endswith("? keys")
    assert shown.style(23, 80 - 6) == frozenset({"inverse"})


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

    live = draw([newest], LIVE)
    assert live.text(1).rstrip() == base
    assert live.style(1, 0) == frozenset({"bold"})
    assert live.style(1, len("PR #31  ")) == frozenset({"grey"})

    idle = draw([newest], IDLE)
    assert idle.text(1).rstrip() == f"last run  {base}"
    assert idle.style(1, 0) == frozenset({"grey"})
    assert idle.style(1, len("last run  ")) == frozenset({"bold"})

    assert draw([newest], DISABLED).text(1).rstrip() == base

    other = draw([newest, past], LIVE, record=past.record)
    tail = f"PR #28  77aa12c  record {past.record}  failed  ended {lc(6, 16, 40)}"
    assert other.text(1).rstrip() == tail
    word = tail.index("failed")
    assert other.style(1, word) == frozenset({"bold", "red"})
    assert other.style(1, word + len("failed  ")) == frozenset({"grey"})

    bare = run(kind="test", pr=None, ref="feature/x", sha=None, result=None, ended=None)
    assert draw([bare], LIVE).text(1).rstrip() == f"feature/x  —  record {STAMP}"


def test_each_stage_glyph_has_its_colour_and_a_stage_row_aligns_with_the_column_header():
    build = stage("build", "open", t(3, 0, 15))
    shown = draw(
        [
            run(
                result=None,
                ended=None,
                checkout=stage("checkout", "passed", t(3, 0, 9), t(3, 0, 11)),
                precondition=stage("precondition", "error", t(3, 0, 11), t(3, 0, 15)),
                build=build,
                bundle=stage("bundle", "not-started"),
                host_suite=stage("host_suite", "skipped"),
                target_suite=stage("target_suite", "failed", t(3, 20, 0), t(3, 25, 9)),
                posted=stage("posted", "passed", t(3, 25, 20), t(3, 25, 22)),
            )
        ],
        LIVE,
        now=t(3, 12, 15),
    )
    assert shown.text(2, 0, LEFT).rstrip() == "stage            start     took"
    assert shown.style(2, 0) == frozenset({"grey"})
    left = [shown.text(r, 0, LEFT).rstrip() for r in range(3, 10)]
    assert left[0] == f"{'✓ checkout':<17}{lc(3, 0, 9)}  0m02s"
    assert left[1] == f"{'✗ precondition':<17}{lc(3, 0, 11)}  0m04s"
    assert left[2] == f"{'● build':<17}{lc(3, 0, 15)}  12m00s"
    assert left[3] == "· bundle"
    assert left[4].startswith("– host suite")
    assert left[5] == f"{'✗ target suite':<17}{lc(3, 20, 0)}  5m09s"
    assert left[6] == f"{'✓ posted':<17}{lc(3, 25, 20)}  0m02s"
    glyph = {
        3: {"green"},
        4: {"red", "bold"},
        5: {"amber", "bold"},
        6: {"grey"},
        7: {"grey"},
        8: {"red", "bold"},
        9: {"green"},
    }
    for row, expected in glyph.items():
        assert shown.tone(row, 0) == frozenset(expected), row
    assert {shown.tone(6, c) for c in range(2, 10)} == {frozenset({"grey"})}


def test_a_tinted_stage_row_keeps_its_tint_when_selected_and_a_plain_one_takes_the_selection():
    runs = [
        run(
            result=None,
            ended=None,
            build=stage("build", "open", t(3, 0, 15)),
            target_suite=stage("target_suite", "failed", t(3, 20, 0), t(3, 25, 9)),
        )
    ]

    def grounds(row, **view):
        shown = draw(runs, LIVE, now=t(3, 12, 15), **view)
        cells = {frozenset(shown.style(row, c) & TINTS) for c in range(LEFT)}
        assert len(cells) == 1, "the ground covers the pane's whole row span"
        assert not shown.style(row, LEFT) & TINTS
        return next(iter(cells))

    assert grounds(3) == frozenset()
    assert grounds(5) == {"running_tint"}
    assert grounds(8) == {"failed_tint"}
    assert grounds(3, stage="checkout") == {"selection"}
    assert grounds(5, stage="build") == {"running_tint"}
    assert grounds(8, stage="target_suite") == {"failed_tint"}


def test_durations_are_unbounded_minutes_and_a_stage_left_open_in_a_dead_run_reads_question_dash():
    long = stage("bundle", "passed", t(3, 5, 0), t(4, 7, 5))
    stuck = stage("build", "open", t(3, 0, 15))
    shown = draw([run(kind="test", result=None, ended=None, build=stuck, bundle=long)], IDLE)
    assert shown.text(5, 0, LEFT).rstrip() == f"{'? build':<17}{lc(3, 0, 15)}  —"
    assert shown.tone(5, 0) == frozenset({"grey"})
    assert not shown.style(5, 0) & TINTS
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
    open_stage = stage(
        name,
        "open",
        t(3, 31, 12),
        sections=sections,
        done=3,
        queued=6,
        passed=2,
        failed=0,
    )
    return run(result=None, ended=None, target_suite=open_stage), sections


def test_a_sectioned_pane_has_a_summary_fold_glyphs_by_state_a_blue_setup_and_a_hint():
    live, _ = target_suite_open()
    shown = draw([live], LIVE, cols=120, now=t(3, 37, 38), stage="target_suite")
    assert shown.right(2) == "target suite  3 of 6  2 passed  0 failed"
    assert shown.tone(2, RIGHT) == frozenset({"amber", "bold"})
    assert shown.tone(2, RIGHT + len("target suite  3 of 6  ")) == frozenset({"green"})
    assert shown.tone(2, RIGHT + len("target suite  3 of 6  2 passed  ")) == frozenset({"grey"})
    assert shown.right(3) == "▸ ✓ setup install_candidate  3m40s"
    assert shown.right(4) == "▸ ✓ t.py::a  1m42s"
    assert shown.right(5) == "▾ ● t.py::b  1m04s"
    assert shown.right(6) == "      reading rauc"
    assert shown.right(7) == "  · t.py::c"
    assert shown.tone(3, RIGHT) == frozenset({"grey"})
    assert shown.tone(3, RIGHT + 2) == frozenset({"green"})
    assert shown.tone(3, RIGHT + 4) == frozenset({"blue"})
    assert shown.tone(3, RIGHT + 4 + len("setup ")) != frozenset({"blue"})
    assert shown.tone(3, RIGHT + len("▸ ✓ setup install_candidate  ")) == frozenset({"grey"})
    assert shown.tone(5, RIGHT + 2) == frozenset({"amber", "bold"})
    assert shown.tone(7, RIGHT + 2) == frozenset({"grey"})
    assert shown.tone(6, RIGHT + 6) == frozenset({"lightgrey"})
    assert shown.right(22) == "Enter into the log   za fold   Esc back to stages"
    assert shown.tone(22, RIGHT) == frozenset({"grey"})


def test_a_view_fold_toggle_overrides_the_derived_fold_state():
    live, sections = target_suite_open()
    shown = draw(
        [live],
        LIVE,
        cols=120,
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
        name,
        "failed",
        t(3, 31, 12),
        t(3, 51, 0),
        lines=["lead line"],
        sections=sections,
        done=2,
        queued=2,
        passed=1,
        failed=1,
    )
    past = run(result="failed", target_suite=failed)
    shown = draw([past], IDLE, cols=120, stage="target_suite")
    assert shown.right(2) == "target suite  2 of 2  1 passed  1 failed"
    assert shown.tone(2, RIGHT) == frozenset({"red", "bold"})
    assert shown.tone(2, RIGHT + len("target suite  2 of 2  1 passed  ")) == frozenset({"red"})
    assert shown.right(3) == "lead line"
    assert shown.tone(3, RIGHT) == frozenset({"lightgrey"})
    assert shown.right(4) == "  ✓ t.py::a  10m00s"
    assert shown.right(5) == "▾ ✗ t.py::b  1m00s"
    assert shown.tone(5, RIGHT + 2) == frozenset({"red", "bold"})
    assert shown.right(6) == "      out line"
    assert shown.right(7) == "      t.py:88: AssertionError"
    assert shown.right(8) == "      E   boom"
    assert shown.tone(8, RIGHT + 6) == frozenset({"lightgrey"})


def test_a_plain_pane_is_a_label_over_bottom_aligned_lines_clipped_at_the_pane_edge():
    lines = ["NOTE: one", "NOTE: two", "x" * 60]
    live = run(result=None, ended=None, build=stage("build", "open", t(3, 0, 15), lines=lines))
    shown = draw([live], LIVE, stage="build")
    assert shown.right(19) == "this stage's output"
    assert shown.tone(19, RIGHT) == frozenset({"grey"})
    assert shown.right(20) == "NOTE: one"
    assert shown.right(21) == "NOTE: two"
    assert shown.text(22, RIGHT) == "x" * 35
    assert shown.tone(22, RIGHT) == frozenset({"lightgrey"})
    assert [shown.right(r) for r in range(2, 19)] == [""] * 17
    pane = "".join(shown.right(r) for r in range(2, 23))
    assert not any(glyph in pane for glyph in "▾▸") and "Enter into" not in pane

    waiting = draw([run(bundle=stage("bundle", "not-started"))], IDLE, stage="bundle")
    shown_rows = [waiting.right(r) for r in range(2, 23) if waiting.right(r)]
    assert shown_rows == ["this stage's output"]


def build_progress(tasks):
    return status.BuildProgress(1204, 3311, tuple(tasks))


def test_the_build_header_floors_the_percent_rounds_the_bar_and_lists_running_tasks():
    tasks = [
        status.Task("curl", "do_configure", t(3, 11, 56)),
        status.Task("systemd", "do_compile", None),
        status.Task("linux-yocto", "do_compile", t(3, 8, 5)),
    ]
    building = stage(
        "build",
        "open",
        t(3, 0, 15),
        lines=["NOTE: Running task 1204 of 3311 (linux-yocto_6.6.bb:do_compile)"],
        progress=build_progress(tasks),
    )
    shown = draw(
        [run(result=None, ended=None, build=building)], LIVE, cols=120, now=t(3, 12, 15),
        stage="build",
    )  # fmt: skip
    assert shown.right(2) == "build  task 1204 of 3311  36%"
    assert shown.tone(2, RIGHT) == frozenset({"amber", "bold"})
    assert shown.text(3, RIGHT, RIGHT + 33) == "█" * 33
    assert {shown.tone(3, RIGHT + c) for c in range(12)} == {frozenset({"amber"})}
    assert {shown.tone(3, RIGHT + c) for c in range(12, 33)} == {frozenset({"bar_empty"})}
    assert shown.right(4) == "running now"
    assert shown.tone(4, RIGHT) == frozenset({"grey"})
    assert shown.right(5) == f"{'linux-yocto':<14}{'do_compile':<14}4m10s"
    assert shown.right(6) == f"{'curl':<14}{'do_configure':<14}0m19s"
    assert shown.right(7) == f"{'systemd':<14}{'do_compile':<14}—"
    assert shown.right(21) == "this stage's output"
    assert shown.right(22).startswith("NOTE: Running task 1204 of 3311")


def test_running_now_is_hidden_when_the_build_is_not_live_and_capped_to_leave_three_output_rows():
    many = [status.Task(f"r{i:02d}", "do_compile", t(3, 1, i)) for i in range(20)]
    output = ["l1", "l2", "l3"]
    building = stage("build", "open", t(3, 0, 15), lines=output, progress=build_progress(many))
    live = draw(
        [run(result=None, ended=None, build=building)], LIVE, cols=120, now=t(3, 12, 15),
        stage="build",
    )  # fmt: skip
    shown_recipes = [live.right(r)[:3] for r in range(5, 19)]
    assert shown_recipes == [f"r{i:02d}" for i in range(14)]
    assert live.right(19) == "this stage's output"
    assert [live.right(r) for r in (20, 21, 22)] == output

    done = stage("build", "passed", t(3, 0, 15), t(3, 40, 0), progress=build_progress(many))
    past = draw([run(build=done)], IDLE, cols=120, stage="build")
    pane = [past.right(r) for r in range(2, 23)]
    assert pane[0] == "build  task 1204 of 3311  36%"
    assert past.text(3, RIGHT, RIGHT + 33) == "█" * 33
    assert not any("running now" in line or "do_compile" in line for line in pane)


def test_the_summary_shows_the_result_word_end_time_floored_minutes_and_one_row_per_suite():
    finished = run(
        kind="test",
        result="failed",
        started=t(3, 1, 0),
        ended=t(4, 3, 59),
        host_suite=stage("host_suite", queued=2, passed=2),
        target_suite=stage("target_suite", "failed", queued=7, passed=6, failed=1),
    )
    shown = draw([finished], IDLE)
    assert shown.right(2) == f"failed  ended {lc(4, 3, 59)}, took 62m"
    assert shown.tone(2, RIGHT) == frozenset({"red", "bold"})
    assert shown.tone(2, RIGHT + len("failed  ")) == frozenset({"grey"})
    assert shown.right(3) == "host suite    2 passed"
    assert shown.tone(3, RIGHT) == frozenset({"grey"})
    assert shown.right(4) == "target suite  6 passed  1 failed"
    assert shown.tone(4, RIGHT + len("target suite  6 passed  ")) == frozenset({"red"})
    assert shown.right(5) == ""

    clean = run(kind="test", host_suite=stage("host_suite", queued=2, passed=2))
    assert draw([clean], IDLE).tone(2, RIGHT) == frozenset({"green", "bold"})
    assert draw([clean], IDLE).right(3) == "host suite    2 passed"
    assert draw([clean], IDLE).right(4) == ""


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
    assert shown.style(1, 0) == frozenset({"grey"})
    row = shown.text(2)
    assert row == (
        STAMP.ljust(18) + "PR #31".ljust(22) + "a1b2c3d".ljust(9) + "failed".ljust(13)
        + "999m" + "  " + "target suite"
    )  # fmt: skip
    assert shown.tone(2, 49) == frozenset({"red"})
    assert shown.tone(2, 68) == frozenset({"grey"})
    assert shown.text(3, 62, 66) == " 58m"
    assert shown.tone(3, 49) == frozenset({"green"})
    assert shown.text(22).rstrip() == "2 records in records/"
    assert shown.style(22, 0) == frozenset({"grey"})


def test_history_results_took_and_the_trailing_column_in_each_case():
    now = t(4, 0, 0)
    running = run(record="20261110T030715Z", result=None, ended=None, started=t(3, 22, 0))
    runs = [
        running,
        hist("20261109T030715Z"),
        hist("20261108T030715Z", result="error", minutes=14, fail="precondition", state="error"),
        run(record="20261107T030715Z", result=None, ended=None),
        hist("20261106T030715Z", kind="test", ref="feature/rauc-retry", pr=None),
        hist(
            "20261105T030715Z", kind="test", ref="main", pr=None, result="failed", fail="host_suite"
        ),
    ]
    shown = draw(runs, LIVE, tab="history", cols=120, now=now)
    rows = {r: shown.text(r) for r in range(2, 8)}
    for index, (result, tone, took, trailing) in enumerate(
        [
            ("running", "amber", " 38m", ""),
            ("passed", "green", " 58m", ""),
            ("bench error", "red", " 14m", "precondition"),
            ("no result", "grey", "   —", ""),
            ("passed", "green", " 58m", "by hand"),
            ("failed", "red", " 58m", "host suite  by hand"),
        ]
    ):
        row = rows[2 + index]
        assert row[49:62].rstrip() == result, row
        assert shown.tone(2 + index, 49) == frozenset({tone})
        assert row[62:66] == took, row
        assert row[68:].rstrip() == trailing, row
    assert rows[6][18:40].rstrip() == "feature/rauc-retry"
    assert shown.tone(6, 68) == frozenset({"blue"})
    assert shown.tone(7, 68) == frozenset({"grey"})
    assert shown.tone(7, 68 + len("host suite  ")) == frozenset({"blue"})
    assert shown.tone(4, 68) == frozenset({"grey"})


def test_the_record_column_widens_for_a_suffixed_id_and_a_selected_long_row_clips_plainly():
    long_ref = "feature/" + "r" * 22
    first = hist(
        "20261110T030715Z-2", ref=long_ref, pr=None, kind="test", result="failed",
        fail="target_suite",
    )  # fmt: skip
    second = hist("20261110T030715Z")
    shown = draw(
        [first, second], IDLE, tab="history", history_record=first.record, directory="records"
    )
    assert shown.text(1, 0, 20) == "record".ljust(20)
    assert shown.text(2, 0, 42) == first.record.ljust(20) + long_ref[:22]
    assert shown.text(2, 42, 51) == "a1b2c3d".ljust(9)
    assert shown.text(2, 70) == "target sui"
    assert shown.text(3, 0, 20) == second.record.ljust(20)
    assert {"selection" in shown.style(2, c) for c in range(80)} == {True}
    assert {"selection" in shown.style(3, c) for c in range(80)} == {False}


# ---------------------------------------------------------------- Help, empty, text


def test_help_has_three_columns_of_amber_keys_with_the_unbuilt_commands_removed():
    shown = draw([run()], IDLE, tab="help", cols=120)
    width = (120 - 6) // 3
    starts = (0, width + 3, 2 * (width + 3))
    assert shown.text(1).rstrip() == "keys"
    assert shown.style(1, 0) == frozenset({"bold"})
    assert not any("inverse" in shown.style(0, c) for c in range(120))
    for start, heading in zip(starts, ("move", "runs and log", "commands"), strict=True):
        assert shown.text(2, start, start + width).rstrip() == heading
        assert shown.tone(2, start) == frozenset({"blue"})
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
            row = 3 + offset
            assert shown.text(row, start, start + width).rstrip() == f"{key:<10}{words}"
            assert shown.tone(row, start) == frozenset({"amber"})
            assert shown.tone(row, start + 10) == frozenset()
    body = "".join(shown.text(r) for r in range(3, 22))
    assert not any(gone in body for gone in ("dd ", ":run", ":disable", ":enable", ":edit", "1-4"))

    narrow = draw([run()], IDLE, tab="help")
    assert narrow.text(3, 0, 24) == "h l       prev / next vi"
    assert narrow.text(3, 27, 51) == "[ ]       older / newer "
    assert narrow.text(3, 54, 78) == ":q        quit".ljust(24)


def test_with_no_runs_run_reads_a_zero_record_line_and_history_its_header_and_footer():
    run_view = draw([], IDLE, directory="records")
    assert run_view.text(1).rstrip() == "0 records in records/"
    assert run_view.style(1, 0) == frozenset({"grey"})
    assert [run_view.text(r).strip() for r in range(2, 23)] == [""] * 21

    history = draw([], IDLE, tab="history", directory="records")
    assert history.text(1).startswith("record")
    assert history.text(22).rstrip() == "0 records in records/"
    assert [history.text(r).strip() for r in range(2, 22)] == [""] * 20


def test_tabs_expand_to_eight_column_stops_and_other_controls_become_caret_pairs():
    odd = "x" * 34 + "\x1b"
    lines = ["a\tb", "esc\x1b[0m", "del\x7f", "\x01", odd]
    live = run(result=None, ended=None, build=stage("build", "open", t(3, 0, 15), lines=lines))
    shown = draw([live], LIVE, stage="build")
    assert shown.right(17) == "this stage's output"
    assert shown.right(18) == "a       b"
    assert shown.right(19) == "esc^[[0m"
    assert shown.right(20) == "del^?"
    assert shown.right(21) == "^A"
    assert shown.right(22) == "x" * 34 + "^"


# ---------------------------------------------------------------- style table and size


def test_the_attributes_only_style_table_maps_each_style_name_and_ors_a_compound():
    expected = {
        "selection": curses.A_REVERSE,
        "inverse": curses.A_REVERSE,
        "badge_running": curses.A_REVERSE,
        "badge_idle": curses.A_REVERSE,
        "badge_disabled": curses.A_REVERSE,
        "bold": curses.A_BOLD,
        "grey": curses.A_DIM,
        "lightgrey": curses.A_DIM,
        "bar_empty": curses.A_DIM,
        "green": curses.A_NORMAL,
        "amber": curses.A_NORMAL,
        "red": curses.A_NORMAL,
        "blue": curses.A_NORMAL,
        "running_tint": curses.A_NORMAL,
        "failed_tint": curses.A_NORMAL,
    }
    assert {name: status.ATTRIBUTES[name] for name in expected} == expected
    assert status.attributes(frozenset()) == curses.A_NORMAL
    assert status.attributes(frozenset({"bold", "badge_running"})) == (
        curses.A_REVERSE | curses.A_BOLD
    )
    assert status.attributes(frozenset({"amber", "bold", "running_tint"})) == curses.A_BOLD
    assert status.attributes(frozenset({"grey", "selection"})) == (curses.A_DIM | curses.A_REVERSE)


@pytest.mark.parametrize(("rows", "cols"), [(24, 79), (23, 80), (10, 40)])
def test_a_terminal_below_eighty_by_twenty_four_draws_one_line_and_blank_rows(rows, cols):
    shown = draw([run()], LIVE, rows=rows, cols=cols)
    assert shown.text(0) == "terminal too small".ljust(cols)
    assert [shown.text(r) for r in range(1, rows)] == [" " * cols] * (rows - 1)


def test_eighty_by_twenty_four_renders_the_views_clipped_to_the_screen():
    long_ref = "z" * 90
    runs = [run(ref=long_ref, pr=None, kind="test")]
    for tab in ("run", "history", "help"):
        shown = draw(runs, LIVE, tab=tab)
        assert "terminal too small" not in shown.text(0)
    assert draw(runs, LIVE).text(1) == (long_ref + "  a1b2c3d").ljust(80)[:80]
