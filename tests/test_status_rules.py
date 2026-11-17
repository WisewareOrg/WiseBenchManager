"""`wisebench.status`'s rules: what the watch view shows, as plain functions of the run model."""

import pytest
from status_helpers import (
    DISABLED,
    IDLE,
    LIVE,
    NOW,
    SHA,
    TESTS,
    failing_at,
    live,
    run,
    section,
    stage,
    suite,
    t,
    target_suite,
)

from wisebench import status
from wisebench.bench_state import DASH

HAND = {"kind": "test", "id": 3, "added": "2026-11-10T03:15:00+00:00", "ref": "feature-a"}
HAND = {**HAND, "sha": "1" * 40, "set": None, "skip_build": False, "args": []}
MERGE = {"kind": "run", "pr": 14, "title": "Add slot check", "sha": "9f8e7d6" + "0" * 33}
RUNNING_MERGE = {"kind": "run", "pr": 31, "title": "Its title", "sha": SHA}
UNBUILT = {"kind": "run", "pr": 15, "title": "Fix probe", "sha": None}
BY_HAND = run(kind="test", ref="topic", pr=None, states=["passed", "open"] + ["not-started"] * 4)


@pytest.mark.parametrize(
    ("the_run", "newest", "bench", "expected"),
    [
        (live("build"), True, LIVE, "build"),
        (run(), True, IDLE, None),
        (failing_at("r", "bundle"), True, DISABLED, "bundle"),
        (failing_at("r", "bundle"), False, IDLE, "bundle"),
        (live("host_suite"), False, IDLE, "host_suite"),
        (run(), False, LIVE, None),
    ],
    ids=["live: open", "idle: summary", "disabled", "past: failed", "past: open", "passed"],
)
def test_the_automatic_stage(the_run, newest, bench, expected):
    shown = status.auto_stage(the_run, newest, bench)
    assert (shown.name if shown else None) == expected


def test_running_and_failed_sections_unfold_by_default_and_a_choice_overrides_either_way():
    sections = [section("s", "test", n, o) for n, o in (("a", "open"), ("b", "failed"), ("c", "x"))]
    assert [status.unfolded({}, s) for s in sections] == [True, True, False]
    choice = {sections[0].id: False, sections[2].id: True}
    assert [status.unfolded(choice, s) for s in sections] == [False, True, True]


def test_log_rows_are_the_stage_lines_then_each_header_and_its_unfolded_body():
    body = section("s", "test", "a", "passed", lines=["out"], failure=["E boom"])
    shown = stage("s", lines=["2026-11-10 03:00:01 - INFO     - lead"], sections=[body])
    assert status.log_rows(shown, {}) == [(None, 0), (body, 0)]
    assert status.log_rows(shown, {}, expand=True) == [(None, 0), (body, 0), (body, 1), (body, 2)]
    texts = [status.row_text(shown, row) for row in status.log_rows(shown, {body.id: True})]
    assert texts == ["lead", "a", "out", "E boom"]


@pytest.mark.parametrize(
    ("shown", "expected"),
    [
        (target_suite(3, failed=(1,)), TESTS[1]),
        (target_suite(3), TESTS[3]),
        (suite("host_suite", ["passed", "passed"]), 0),
        (stage("build", lines=["a", "b", "c"]), 2),
    ],
    ids=["first failed", "running", "neither", "no sections"],
)
def test_the_log_cursor_opens_on_the_first_failed_section_else_the_running_one(shown, expected):
    rows = status.log_rows(shown, {})
    at = status.opening_row(shown, rows)
    assert (rows[at][0].name if isinstance(expected, str) else at) == expected


def test_a_row_is_found_again_after_a_re_parse_by_section_id_else_its_header():
    old = target_suite(2).sections[3]
    rows = status.log_rows(target_suite(3), {old.id: True})
    found = rows[status.find(rows, (old, 1))]
    assert (found[0].id, found[1]) == (old.id, 1)
    folded = status.log_rows(target_suite(3), {old.id: False})
    found = folded[status.find(folded, (old, 1))]
    assert (found[0].id, found[1]) == (old.id, 0)


def test_search_wraps_looks_in_folded_bodies_and_misses_with_none():
    shown = target_suite(3)
    first = (shown.sections[1], 0)
    found = status.search(shown, {}, first, "out 1", 1)
    assert (found[0].name, found[1]) == (TESTS[1], 1)
    assert status.search(shown, {}, found, "out 0", 1)[0].name == TESTS[0]
    assert status.search(shown, {}, found, "out 1", -1) == found
    assert status.search(shown, {}, found, "nowhere", 1) is None


def test_a_section_ends_where_the_next_started_section_starts_else_with_the_stage():
    begun = [section("s", "test", n, "passed", t(3, m)) for n, m in (("a", 1), ("b", 2))]
    shown = stage("s", ended=t(4), sections=[*begun, section("s", "test", "c", "not-started")])
    assert list(status.section_ends(shown).values()) == [t(4), t(4), t(3, 2)]


@pytest.mark.parametrize(
    ("ended", "running", "expected"),
    [(t(3, 2, 5), False, "2m05s"), (None, True, "427m30s"), (None, False, DASH)],
)
def test_a_stage_took_start_to_end_or_to_now_while_running(ended, running, expected):
    assert status.took(t(3), ended, running, NOW) == expected


@pytest.mark.parametrize(
    ("the_run", "running", "expected"),
    [(run(), False, "62m"), (live("build"), True, "427m"), (live("build"), False, DASH)],
)
def test_a_run_took_whole_minutes_to_its_end_or_to_now_while_running(the_run, running, expected):
    assert status.run_took(the_run, running, NOW) == expected


def test_the_queue_is_the_running_run_then_hand_entries_then_the_merge_queue(local_zone):
    rows = status.queue_rows([live("build")], LIVE, [HAND, RUNNING_MERGE, MERGE, UNBUILT])
    assert [(r.marker, r.source, r.what, r.sha7, r.added, r.stage) for r in rows] == [
        ("▶", "merge queue", "PR #31 Its title", SHA[:7], "", "build"),
        ("2", "by hand", "feature-a", "1111111", "06:15", ""),
        ("3", "merge queue", "PR #14 Add slot check", "9f8e7d6", "", ""),
        ("4", "merge queue", "PR #15 Fix probe", "", "", ""),
    ]
    assert [(r.key, r.id) for r in rows] == [
        ((), None),
        (("hand", 3), 3),
        (("merge", 14), None),
        (("merge", 15), None),
    ]


@pytest.mark.parametrize(
    ("runs", "bench", "first"),
    [
        ([live("build")], LIVE, ("▶", "merge queue", "PR #31")),
        ([BY_HAND], LIVE, ("▶", "by hand", "topic")),
        ([live("build")], IDLE, ("1", "by hand", "feature-a")),
    ],
    ids=["no merge entry", "by hand", "not live"],
)
def test_the_running_row_is_the_live_newest_run_titled_by_its_pr_or_its_ref(runs, bench, first):
    row = status.queue_rows(runs, bench, [HAND])[0]
    assert (row.marker, row.source, row.what) == first


@pytest.mark.parametrize(
    ("entries", "place"),
    [([], "joins as 2, after PR #31"), ([HAND, MERGE], "joins as 3, after feature-a")],
)
def test_the_preview_joins_after_the_running_run_and_the_hand_entries(entries, place):
    rows = status.queue_rows([live("build")], LIVE, entries)
    result = {"sha": "1234567" + "0" * 33, "subject": "s", "clean": False}
    resolved = 'resolves to 1234567 "s", uncommitted changes in the checkout'
    assert status.preview_lines("topic", result, rows) == ["will queue", resolved, place]
    assert status.preview_lines("topic", "boom", rows) == ["topic does not resolve"]


@pytest.mark.parametrize(
    ("text", "target"),
    [
        (":run topic ", (None, "topic")),
        (":run -C ~/src HEAD", ("~/src", "HEAD")),
        (":run -C ~/src", (None, "-C ~/src")),
        (":run ", None),
        (":runx", None),
    ],
)
def test_the_directory_and_ref_a_run_command_names(text, target):
    assert status.run_target(text) == target


def test_a_bench_failure_is_in_the_last_failed_or_open_setup_of_the_first_failed_stage():
    setups = [
        section("target_suite", "setup", n, o) for n, o in (("outer", "failed"), ("inner", "open"))
    ]
    failing = stage(
        "target_suite", "error", sections=[*setups, section("target_suite", "test", "t", "failed")]
    )
    assert status.failed_setup(run(target_suite=failing)) == "inner"
    assert status.failed_setup(failing_at("r", "bundle")) is None


def test_a_run_reads_as_its_pr_else_its_ref_with_its_sha7():
    assert (status.what(run()), status.sha7(run())) == ("PR #31", SHA[:7])
    assert status.what(run(pr=None, ref="topic")) == "topic"
    assert status.sha7(run(sha=None)) == DASH
