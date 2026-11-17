"""`wisebench.view`: the watch view's rules, as plain functions of the run model."""

from datetime import timedelta

import pytest
from status_helpers import (
    DISABLED,
    IDLE,
    LIVE,
    NOW,
    OLDER,
    SHA,
    STAMP,
    failing_at,
    live,
    run,
    section,
    stage,
    suite,
    t,
)

from wisebench import view

HAND1 = {
    "kind": "test",
    "id": 3,
    "added": "2026-11-10T03:15:00+00:00",
    "ref": "feature-a",
    "sha": "1" * 40,
    "set": None,
    "skip_build": False,
    "args": [],
}
HAND2 = {**HAND1, "id": 4, "added": "2026-11-10T03:40:00+00:00", "ref": "feature-b"}
MERGE = {"kind": "run", "pr": 14, "title": "Add slot check", "sha": "9f8e7d6" + "0" * 33}
WAITING = {"kind": "run", "pr": 15, "title": "Fix probe", "sha": None}
CLEAN = {"sha": "1234567" + "0" * 33, "subject": "Fix the thing", "clean": True}


def name(stage_or_none):
    """The stage's name, or None."""
    return None if stage_or_none is None else stage_or_none.name


@pytest.mark.parametrize(
    ("the_run", "newest", "bench", "expected"),
    [
        (live("build"), True, LIVE, "build"),
        (run(), True, IDLE, None),
        (failing_at(STAMP, "target_suite"), True, DISABLED, "target_suite"),
        (live("bundle"), True, DISABLED, "bundle"),
        (
            run(states=["passed", "error"] + ["not-started"] * 5, result="error"),
            True,
            DISABLED,
            "precondition",
        ),
        (failing_at(OLDER, "bundle"), False, LIVE, "bundle"),
        (run(OLDER), False, LIVE, None),
    ],
    ids=[
        "newest live: its open stage",
        "newest idle: the summary",
        "disabled: the failed stage",
        "disabled: the stage left open",
        "disabled: the errored stage",
        "past run: its failed stage",
        "past run that passed: the summary",
    ],
)
def test_the_automatic_stage_follows_the_run_and_the_bench(the_run, newest, bench, expected):
    assert name(view.auto_stage(the_run, newest, bench)) == expected


@pytest.mark.parametrize(
    ("outcome", "folds", "opened"),
    [
        ("open", {}, True),
        ("failed", {}, True),
        ("passed", {}, False),
        ("not-started", {}, False),
        ("passed", {("s", "test", "x", 0): True}, True),
        ("failed", {("s", "test", "x", 0): False}, False),
    ],
)
def test_running_and_failed_sections_start_open_and_a_fold_overrides_either_way(
    outcome, folds, opened
):
    assert view.is_open(section("s", "test", "x", outcome), folds) is opened


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        (["passed", "failed", "open"], "case_01"),
        (["passed", "open", "not-started"], "case_01"),
        (["passed", "passed"], None),
    ],
    ids=["the first failed", "else the running one", "else none"],
)
def test_the_log_cursor_lands_on_the_first_failed_section_else_the_running_one(outcomes, expected):
    assert name(view.landing(suite("target_suite", outcomes))) == expected


def test_a_section_ends_where_the_next_started_section_starts_else_with_its_stage():
    sections = [
        section("s", "test", "a", "passed", t(3, 1)),
        section("s", "test", "b", "passed", t(3, 4)),
        section("s", "test", "c", "not-started"),
    ]
    ended = stage("s", ended=t(3, 9), sections=sections)
    assert view.section_ends(ended) == [t(3, 4), t(3, 9), t(3, 9)]


def test_the_counts_are_started_queued_passed_and_failed_tests_and_setups_are_not_counted():
    the_suite = suite("target_suite", ["passed", "failed", "open", "not-started"])
    with_setup = stage(
        "target_suite",
        sections=(section("target_suite", "setup", "fixture", "passed"), *the_suite.sections),
    )
    assert view.counts(with_setup) == (3, 4, 1, 1)


@pytest.mark.parametrize(
    ("the_stage", "form"),
    [
        (None, "summary"),
        (suite("host_suite", ["passed"]), "suite"),
        (stage("bundle", lines=["x"]), "plain"),
        (stage("build", progress=object()), "build"),
    ],
)
def test_the_pane_takes_the_form_of_the_stage_shown(the_stage, form):
    assert view.pane_form(the_stage) == form


@pytest.mark.parametrize(
    ("state", "running", "expected"),
    [
        ("passed", False, ("✓", "passed")),
        ("failed", False, ("✗", "failed")),
        ("error", False, ("✗", "failed")),
        ("open", True, ("●", "running")),
        ("open", False, ("?", "dim")),
        ("skipped", False, ("–", "dim")),
        ("not-started", False, ("·", "dim")),
    ],
)
def test_each_state_has_its_glyph_and_meaning(state, running, expected):
    assert view.tone(state, running) == expected


def test_took_is_start_to_end_else_start_to_now_while_running_else_a_dash():
    start = t(3)
    assert view.took(start, start + timedelta(seconds=75), False, NOW) == "1m15s"
    assert view.took(start, None, True, start + timedelta(minutes=61, seconds=2)) == "61m02s"
    assert view.took(start, None, False, NOW) == "—"


def test_a_runs_took_is_whole_minutes_to_its_end_else_to_now_while_running():
    assert view.run_took(run(), False, NOW) == "62m"
    assert view.run_took(live("build"), True, t(3, 38, 59)) == "38m"
    assert view.run_took(live("build"), False, NOW) == "—"


@pytest.mark.parametrize(
    ("the_run", "running", "result", "note"),
    [
        (run(), False, ("passed", "passed"), ("", "dim")),
        (failing_at(STAMP, "host_suite"), False, ("failed", "failed"), ("host suite", "dim")),
        (
            run(kind="test", pr=None, ref="feature/x"),
            False,
            ("passed", "passed"),
            ("by hand", "hand"),
        ),
        (live("build"), True, ("running", "running"), ("", "dim")),
        (
            run(result="error", states=["passed", "error"] + ["not-started"] * 5),
            False,
            ("bench error", "failed"),
            ("precondition", "dim"),
        ),
    ],
    ids=["passed", "failed", "by hand", "running", "bench error"],
)
def test_a_history_row_is_record_what_sha_result_took_and_the_failing_stage_or_by_hand(
    the_run, running, result, note
):
    record, what, sha, shown_result, took, shown_note = view.history_row(the_run, running, NOW)
    assert (record, sha, shown_result, shown_note) == (STAMP, SHA[:7], result, note)
    assert what == ("feature/x" if the_run.kind == "test" else "PR #31")


def rows(runs=(), bench=IDLE, entries=None):
    """(marker, source, what, sha7, stage) of each Queue row."""
    return [
        (r.marker, r.source, r.what, r.sha7, r.stage)
        for r in view.queue_rows(list(runs), bench, entries)
    ]


def test_the_queue_is_the_running_run_then_hand_entries_then_the_merge_queue():
    assert rows([live("build")], LIVE, [HAND1, MERGE, WAITING]) == [
        ("▶", "merge queue", "PR #31", SHA[:7], "build"),
        ("2", "by hand", "feature-a", "1111111", ""),
        ("3", "merge queue", "PR #14 Add slot check", "9f8e7d6", ""),
        ("4", "merge queue", "PR #15 Fix probe", "", ""),
    ]


def test_the_running_run_takes_its_title_from_its_merge_entry_and_hides_the_entry_of_its_sha():
    running = live("build", checkout=stage("checkout", sha=MERGE["sha"]))
    entry = {**MERGE, "pr": 31}
    assert rows([running], LIVE, [entry]) == [
        ("▶", "merge queue", "PR #31 Add slot check", "9f8e7d6", "build")
    ]


def test_without_a_running_run_the_places_start_at_1():
    assert [r[0] for r in rows([live("build")], IDLE, [HAND1, MERGE])] == ["1", "2"]


def test_a_running_hand_run_reads_by_hand_with_its_ref():
    states = ["open"] + ["not-started"] * 5
    hand = run(kind="test", pr=None, ref="feature/x", result=None, states=states)
    assert rows([hand], LIVE) == [("▶", "by hand", "feature/x", SHA[:7], "checkout")]


def test_a_hand_rows_added_time_is_local(local_zone):
    (row,) = view.queue_rows([], IDLE, [HAND1])
    assert (row.added, row.id, row.key) == ("06:15", 3, "hand 3")


def test_the_preview_says_what_will_queue_where_it_joins_and_after_what():
    queue = view.queue_rows([], IDLE, [HAND1, HAND2, MERGE])
    assert view.preview_lines("feature-c", CLEAN, queue) == [
        "will queue",
        'resolves to 1234567 "Fix the thing", clean tree',
        "joins as 3, after feature-b",
    ]


@pytest.mark.parametrize(
    ("runs", "bench", "entries", "place"),
    [
        ([], IDLE, [MERGE], "joins as 1"),
        ([live("build")], LIVE, [], "joins as 2, after PR #31"),
        ([live("build")], LIVE, [HAND1], "joins as 3, after feature-a"),
    ],
    ids=["first", "after the running run", "after the last hand entry"],
)
def test_the_place_counts_the_running_run_and_the_hand_entries_only(runs, bench, entries, place):
    queue = view.queue_rows(runs, bench, entries)
    assert view.preview_lines("x", CLEAN, queue)[2] == place


def test_a_dirty_checkout_and_an_unresolvable_ref_are_said_so():
    dirty = view.preview_lines("x", {**CLEAN, "clean": False}, [])
    assert dirty[1].endswith("uncommitted changes in the checkout")
    assert view.preview_lines("nope", "fatal: bad revision", []) == ["nope does not resolve"]


@pytest.mark.parametrize(
    ("text", "ref"),
    [
        (":run feature-c", "feature-c"),
        (":run   feature-c  ", "feature-c"),
        (":run ", None),
        (":ru", None),
        (":runx", None),
    ],
)
def test_the_typed_ref_is_what_follows_run_stripped(text, ref):
    assert view.typed_ref(text) == ref


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("HEAD", (None, "HEAD")),
        ("-C /x HEAD", ("/x", "HEAD")),
        ("-C /x my branch", ("/x", "my branch")),
        ("-C /x", (None, "-C /x")),
    ],
    ids=["no-directory", "directory-and-ref", "multi-word-ref", "no-ref"],
)
def test_split_run_takes_the_directory_after_dash_c_and_leaves_any_other_text_whole(text, expected):
    assert view.split_run(text) == expected


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("a\tb", "a       b"),
        ("\x1b]0;title\x07", "^[]0;title^G"),
        ("\x1b[31mred", "^[[31mred"),
        ("\x9b31m", "<9b>31m"),
        ("del\x7f", "del^?"),
    ],
    ids=["tab", "OSC and BEL", "CSI", "C1 CSI", "DEL"],
)
def test_text_from_outside_never_carries_a_control_code(raw, shown):
    assert view.clean(raw) == shown


def test_the_kas_prefix_is_dropped_only_at_the_start_of_a_line():
    prefixed = "2026-11-10 03:00:00 - INFO     - NOTE: x"
    assert view.shown_line(prefixed) == "NOTE: x"
    assert view.shown_line("echo " + prefixed) == "echo " + prefixed
