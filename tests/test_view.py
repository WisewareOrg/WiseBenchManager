"""The view's rules as plain functions: which stage and section show, the queue and its preview."""

import pytest
from status_helpers import (
    DISABLED,
    IDLE,
    LIVE,
    NOW,
    STAMP,
    failing_at,
    live,
    run,
    section,
    stage,
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


@pytest.mark.parametrize(
    ("shown", "newest", "bench", "expected"),
    [
        (live("bundle"), True, LIVE, "bundle"),
        (run(), True, IDLE, None),
        (failing_at(STAMP, "host_suite"), True, DISABLED, "host_suite"),
        (failing_at(STAMP, "build"), False, IDLE, "build"),
    ],
    ids=["live: the open stage", "idle: the summary", "disabled: the failure", "past: the failure"],
)
def test_the_stage_shown_until_one_is_chosen(shown, newest, bench, expected):
    stage = view.auto_stage(shown, newest, bench)
    assert (stage and stage.name) == expected


def test_running_and_failed_sections_start_open_and_the_cursor_starts_on_the_first_failure():
    suite = target_suite(running=4, failed=(2,))
    opened = [s.name for s in suite.sections if view.starts_open(s)]
    assert opened == ["t.py::test_2", "t.py::test_4"]
    assert view.first_section(suite).name == "t.py::test_2"
    assert view.first_section(target_suite(running=4)).name == "t.py::test_4"


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


def test_the_queue_is_the_running_run_then_hand_entries_then_the_merge_queue():
    running = live("build")
    entries = [HAND, merge(31, sha=running.stages[0].sha), merge(34)]
    rows = view.queue_rows([running], LIVE, entries)
    assert [row[0] for row in rows] == ["running", "hand 3", "merge 34"]
    assert rows[0][3][:3] == ("▶", "merge queue", "PR #31 title 31")
    assert rows[0][3][5] == "running, build"
    assert [row[3][0] for row in rows[1:]] == ["2", "3"]
    assert [row[0] for row in view.queue_rows([running], IDLE, entries)][0] == "hand 3"


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (
            {"sha": "1234567" + "0" * 33, "subject": "Fix", "clean": True},
            'resolves to 1234567 "Fix", clean tree',
        ),
        ({"sha": "1" * 40, "subject": "s", "clean": False}, "uncommitted changes in the checkout"),
        (ValueError("no"), "feat2 does not resolve"),
    ],
    ids=["clean", "dirty", "unresolvable"],
)
def test_the_run_preview_says_what_the_ref_resolves_to_and_where_it_joins(outcome, expected):
    rows = view.queue_rows([live("build")], LIVE, [HAND, merge(34)])
    shown = view.preview("feat2", outcome, rows).plain
    assert expected in shown
    if not isinstance(outcome, Exception):
        assert shown.endswith("joins as 3, after feat")


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
    if ref:
        assert view.split_run(ref) == split


def test_a_history_row_names_the_failing_stage_and_a_run_by_hand():
    cells = [cell.plain for cell in view.history_cells(failing_at(STAMP, "host_suite"), False, NOW)]
    assert cells == [STAMP, "PR #31", "a1b2c3d", "failed", " 62m", "host suite"]
    hand = run(kind="test", ref="feature/x", pr=None)
    assert [c.plain for c in view.history_cells(hand, False, NOW)][1::4] == [
        "feature/x",
        "  by hand",
    ]
    assert view.history_cells(live("build"), True, NOW)[3].plain == "running"


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("a\x1b[2Jb", "a^[[2Jb"),
        ("bell\x07", "bell^G"),
        ("c1\x9b31m", "c1<9b>31m"),
        ("\tx", " " * 8 + "x"),
    ],
)
def test_no_text_reaches_the_screen_with_a_control_character_in_it(raw, shown):
    assert view.line((raw, None)).plain == shown


def test_the_failure_box_says_where_the_run_stopped_and_when_the_bench_was_disabled():
    stopped = live("target_suite", target_suite=target_suite(running=0))
    box = view.failure_box("ssh: no answer", t(3, 14, 2), stopped).plain.splitlines()
    assert box[0] == "bench failure, not the candidate's"
    assert box[1] == "ssh: no answer"
    assert box[2].startswith("in target suite t.py::test_0; from the DISABLED file, ")
    assert box[2].endswith(view.clock(t(3, 14, 2)))
