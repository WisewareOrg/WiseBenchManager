"""The view's rules as plain functions: which stage and rows show, the queue, the preview, texts."""

import pytest
from status_helpers import (
    DISABLED,
    IDLE,
    LIVE,
    NOW,
    STAMP,
    building,
    failing_at,
    live,
    plain,
    run,
    section,
    stage,
    suite,
    t,
    target_suite,
)

from wisebench import view

HAND = {
    "kind": "test",
    "id": 3,
    "added": "2026-11-10T03:15:00+00:00",
    "ref": "feat",
    "sha": "1" * 40,
}


def merge(pr, sha="9" * 40):
    """A merge-queue entry for PR `pr`."""
    return {"kind": "run", "pr": pr, "title": f"title {pr}", "sha": sha}


def texts(rows):
    """The plain text of each row of a `LogRows`."""
    return [rows[i].plain for i in range(len(rows))]


@pytest.mark.parametrize(
    ("shown", "newest", "bench", "expected"),
    [
        (live("bundle"), True, LIVE, "bundle"),
        (failing_at(STAMP, "build"), True, IDLE, None),
        (failing_at(STAMP, "host_suite"), True, DISABLED, "host_suite"),
        (failing_at(STAMP, "build"), False, IDLE, "build"),
    ],
    ids=["live: the open stage", "idle: the summary", "disabled: the failure", "past: the failure"],
)
def test_the_stage_shown_until_one_is_chosen(shown, newest, bench, expected):
    stage = view.auto_stage(shown, newest, bench)
    assert (stage and stage.name) == expected


def test_running_and_failed_sections_start_open_and_the_rest_folded():
    rows = texts(view.LogRows(target_suite(running=4, failed=(2,)), {}, True, NOW))
    assert "      out 2" in rows and "      out 4" in rows
    assert "      out 1" not in rows and "      booted slot B" not in rows


@pytest.mark.parametrize(
    ("shown", "expected"),
    [
        (target_suite(running=4, failed=(2,)), "▾ ✗ t.py::test_2"),
        (target_suite(running=4), "▾ ● t.py::test_4"),
        (suite("target_suite", ["passed"] * 2), "  ✓ case_00"),
        (plain("build", 3), "build line 2"),
    ],
    ids=[
        "the first failure",
        "else the running test",
        "else the first row",
        "no sections: the last",
    ],
)
def test_where_a_logs_cursor_starts(shown, expected):
    rows = view.LogRows(shown, {}, True, NOW)
    assert rows[rows.index(None)].plain.startswith(expected)


def test_a_key_keeps_its_row_and_tail_the_last_row_as_the_log_grows():
    key = view.LogRows(plain("build", 5), {}, True, NOW).key(2)
    grown = view.LogRows(plain("build", 9), {}, True, NOW)
    assert grown[grown.index(key)].plain == "build line 2"
    assert grown[grown.index(view.TAIL)].plain == "build line 8"


def test_a_row_in_a_folded_body_gives_its_sections_header():
    shown = target_suite(running=2)
    key = (shown.sections[3].id, 1)
    rows = view.LogRows(shown, {}, True, NOW)
    assert rows[rows.index(key)].plain == "      out 2"
    folded = view.LogRows(shown, {key[0]: False}, True, NOW)
    assert folded[folded.index(key)].plain.startswith("▸ ● t.py::test_2")


def test_a_search_finds_a_line_in_a_folded_body_and_wraps_round_both_ways():
    shown = suite("target_suite", ["passed"] * 6, {1: ("needle one",), 4: ("needle two",)})
    rows = view.LogRows(shown, {}, False, NOW)
    first = rows.find("needle", (None, 0), 1)
    assert first == (shown.sections[1].id, 1)
    assert rows.find("needle", first, 1) == (shown.sections[4].id, 1)
    assert rows.find("needle", first, -1) == (shown.sections[4].id, 1)
    assert rows.find("absent", first, 1) is None


def test_a_section_ends_where_the_next_started_section_starts_else_with_its_stage():
    rows = [
        section("s", "test", "a", "passed", t(3, 1)),
        section("s", "test", "b", "not-started"),
        section("s", "test", "c", "passed", t(3, 4)),
    ]
    assert view.section_ends(stage("s", ended=t(3, 9), sections=rows)) == [
        t(3, 4),
        t(3, 4),
        t(3, 9),
    ]


@pytest.mark.parametrize(
    ("ended", "running", "expected"),
    [(t(3, 2, 5), False, "2m05s"), (None, True, "427m30s"), (None, False, "—")],
    ids=["finished: start to end", "running: start to now", "neither: a dash"],
)
def test_took_counts_to_the_end_or_while_running_to_now(ended, running, expected):
    assert view.took(t(3), ended, running, NOW) == expected


def test_a_run_took_whole_minutes_and_runs_to_now_while_the_bench_runs_it():
    assert view.run_took(run(), False, NOW) == "62m"
    assert view.run_took(live("build"), True, NOW) == "427m"


@pytest.mark.parametrize(
    ("shown", "name", "running", "expected"),
    [
        (run(host_suite=suite("host_suite", ["passed", "failed"]), result="failed"), None, False,
         ["failed  ended", "took 62m", "host suite    1 passed  1 failed"]),
        (live("target_suite", target_suite=target_suite(2, failed=(1,))), "target_suite", True,
         ["target suite  3 of 6  1 passed  1 failed", "Enter into the log"]),
        (building(2), "build", True, ["build  task 1204 of 3311  36%", "█" * 12, "running now",
                                       "r0  do_compile  ", "this stage's output"]),
        (run(build=plain("build", 1, "passed")), "build", False, ["this stage's output"]),
    ],
    ids=["summary", "suite tally", "build progress and tasks", "plain"],
)  # fmt: skip
def test_the_pane_head_of_each_form(shown, name, running, expected):
    stage = next((s for s in shown.stages if s.name == name), None)
    head = "\n".join(text.plain for text in view.head(shown, stage, running, NOW))
    assert all(part in head for part in expected), head


def test_the_queue_is_the_running_run_then_hand_entries_then_the_merge_queue():
    running = live("build")
    entries = [HAND, merge(31, sha=running.stages[0].sha), merge(34)]
    rows = view.queue_rows([running], LIVE, entries)
    assert [row[0] for row in rows] == ["running", "hand 3", "merge 34"]
    cells = ["▶", "merge queue", "PR #31 title 31", "a1b2c3d", "", "running, build"]
    assert [cell for cell, _ in rows[0][3]] == cells
    assert [row[3][0][0] for row in rows[1:]] == ["2", "3"]
    assert view.queue_rows([running], IDLE, entries)[0][0] == "hand 3"


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ({"sha": "1234567" + "0" * 33, "subject": "Fix", "clean": True},
         'resolves to 1234567 "Fix", clean tree'),
        ({"sha": "1" * 40, "subject": "s", "clean": False}, "uncommitted changes in the checkout"),
        (ValueError("no"), "feat2 does not resolve"),
    ],
    ids=["clean", "dirty", "unresolvable"],
)  # fmt: skip
def test_the_run_preview_says_what_the_ref_resolves_to_and_where_it_joins(outcome, expected):
    rows = view.queue_rows([live("build")], LIVE, [HAND, merge(34)])
    shown = view.preview("feat2", outcome, rows).plain
    assert expected in shown
    assert shown.endswith("joins as 3, after feat") != isinstance(outcome, Exception)


@pytest.mark.parametrize(
    ("typed", "ref", "split"),
    [
        (":run main", "main", (None, "main")),
        (":run   -C ~/src/x  topic ", "-C ~/src/x  topic", ("~/src/x", "topic")),
        (":run ", None, None),
        (":runx", None, None),
    ],
)
def test_the_ref_and_directory_typed_after_run(typed, ref, split):
    assert view.typed_ref(typed) == ref
    assert ref is None or view.split_run(ref) == split


def test_a_history_row_names_the_failing_stage_and_a_run_by_hand():
    cells = [text for text, _ in view.history_cells(failing_at(STAMP, "host_suite"), False, NOW)]
    assert cells == [STAMP, "PR #31", "a1b2c3d", "failed", " 62m", "host suite", ""]
    hand = view.history_cells(run(kind="test", ref="feature/x", pr=None), False, NOW)
    assert [text for text, _ in hand][1::5] == ["feature/x", "by hand"]
    assert view.history_cells(live("build"), True, NOW)[3] == ("running", view.AMBER)


@pytest.mark.parametrize(
    ("raw", "shown"),
    [("a\x1b[2Jb", "a^[[2Jb"), ("bell\x07", "bell^G"), ("c1\x9b31m", "c1<9b>31m"),
     ("\tx", " " * 8 + "x")],
)  # fmt: skip
def test_no_text_reaches_the_screen_with_a_control_character_in_it(raw, shown):
    assert view.line((raw, None)).plain == shown


def test_the_failure_box_names_the_last_failed_setup_and_the_time_the_bench_stopped():
    sections = [
        section("target_suite", "setup", "outer", "failed", t(3, 1)),
        section("target_suite", "setup", "install_candidate", "failed", t(3, 2)),
        section("target_suite", "test", "t.py::a", "failed", t(3, 3)),
    ]
    stopped = run(target_suite=stage("target_suite", "error", t(3), sections=sections))
    reason, at = view.disabled("2026-11-10T03:14:02+00:00\nssh: no answer\n")
    box = view.failure_box(reason, at, stopped).plain.splitlines()
    assert box[:2] == ["bench failure, not the candidate's", "ssh: no answer"]
    assert (
        box[2] == f"in setup install_candidate; from the DISABLED file, {view.clock(t(3, 14, 2))}"
    )


def test_a_disabled_file_that_does_not_parse_shows_no_reason_and_no_time():
    reason, at = view.disabled("hand-touched\nreason\n")
    assert (reason, at) == ("", None)
    assert view.failure_box(reason, at, run()).plain.split("\n")[1:] == ["", ""]
