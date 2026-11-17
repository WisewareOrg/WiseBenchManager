"""`view`: the watch view's domain rules, apart from any widget."""

import pytest
from status_helpers import (
    DIRECTORY,
    DISABLED,
    IDLE,
    LIVE,
    LONG,
    NOW,
    OLDER,
    SHA,
    STAMP,
    TESTS,
    building,
    failing_at,
    live,
    plain,
    run,
    running_test,
    sid,
    stage,
    suite,
    t,
)

from wisebench import view

THREE = [live("build"), failing_at(OLDER, "bundle"), run("20261108T030715Z")]
PLAIN_BUILD = live("build", build=plain("build", 40))
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
HAND2 = {
    **HAND1,
    "id": 4,
    "added": "2026-11-10T03:40:00+00:00",
    "ref": "feature-b",
    "sha": "2" * 40,
}
MERGE = {
    "kind": "run",
    "pr": 14,
    "title": "Add slot check",
    "sha": "9f8e7d6" + "0" * 33,
}
WAITING = {"kind": "run", "pr": 15, "title": "Fix probe", "sha": None}


def resolve(runs, bench=IDLE, record=None, stage=None, prompt=None, **read):
    """Resolve the frame for `runs`; `read` is the Snapshot's `queue` and `preview`."""
    snap = view.Snapshot(runs, bench, NOW, DIRECTORY, **read)
    return view.resolve(snap, record, stage, prompt)


def stage_name(frame):
    """The name of the frame's stage, or None."""
    return None if frame.stage is None else frame.stage.name


def stage_of(the_run, name):
    """The stage of `the_run` called `name`."""
    return next(s for s in the_run.stages if s.name == name)


@pytest.mark.parametrize(
    ("runs", "bench", "record", "expected"),
    [
        ([live("build"), run(OLDER)], LIVE, None, "build"),
        ([run(), failing_at(OLDER, "bundle")], IDLE, None, None),
        ([failing_at(STAMP, "bundle")], IDLE, None, None),
        ([failing_at(STAMP, "target_suite"), run(OLDER)], DISABLED, None, "target_suite"),
        ([live("bundle")], DISABLED, None, "bundle"),
        (
            [run(states=["passed", "error"] + ["not-started"] * 5, result="error")],
            DISABLED,
            None,
            "precondition",
        ),
        ([live("build"), failing_at(OLDER, "bundle")], LIVE, OLDER, "bundle"),
        ([live("build"), run(OLDER)], LIVE, OLDER, None),
    ],
    ids=[
        "newest live: its open stage",
        "newest idle: the summary",
        "newest idle that failed: the summary too",
        "disabled: the failed stage",
        "disabled: the stage left open",
        "disabled: the errored stage",
        "past run: its failed stage",
        "past run that passed: the summary",
    ],
)
def test_the_automatic_stage_is_derived_from_the_run_and_the_bench(runs, bench, record, expected):
    assert stage_name(resolve(runs, bench, record)) == expected


def test_the_automatic_stage_follows_the_running_stage_then_falls_back_to_the_summary():
    assert stage_name(resolve([live("build")], LIVE)) == "build"
    assert stage_name(resolve([live("bundle")], LIVE)) == "bundle"
    assert stage_name(resolve([run()], IDLE)) is None


def test_a_chosen_stage_stays_while_the_run_moves_on():
    for runs in ([live("build")], [live("bundle")]):
        assert stage_name(resolve(runs, LIVE, stage="checkout")) == "checkout"


def test_a_record_gone_from_disk_shows_the_newest_run():
    assert resolve([run()], record=OLDER).run.record == STAMP


def test_live_is_the_live_bench_and_the_newest_run_shown():
    runs = [live("build"), run(OLDER)]
    newest = resolve(runs, LIVE)
    past = resolve(runs, LIVE, OLDER)
    idle = resolve(runs, IDLE)

    assert (newest.run is runs[0], newest.newest, newest.live) == (True, True, True)
    assert (past.run is runs[1], past.newest, past.live) == (True, False, False)
    assert (idle.newest, idle.live) == (True, False)


@pytest.mark.parametrize(
    ("runs", "bench", "kind"),
    [
        ([building(1)], LIVE, "build"),
        ([running_test(2)], LIVE, "suite"),
        ([PLAIN_BUILD], LIVE, "plain"),
        ([run()], IDLE, "summary"),
    ],
)
def test_the_pane_head_is_chosen_by_the_stage_shown(runs, bench, kind):
    assert resolve(runs, bench).pane_head == kind


def test_a_live_build_lists_every_running_task_oldest_first_and_a_past_one_none():
    assert [task.recipe for task in resolve([building(3)], LIVE).tasks] == ["r0", "r1", "r2"]
    assert resolve([building(3)], IDLE, stage="build").tasks == ()


@pytest.mark.parametrize(
    ("the_stage", "expected"),
    [
        (
            stage_of(running_test(3, failed=(1,)), "target_suite"),
            ("target_suite", sid(TESTS[1]), 0),
        ),
        (stage_of(running_test(2), "target_suite"), ("target_suite", sid(TESTS[2]), 0)),
        (stage_of(PLAIN_BUILD, "build"), ("build", None, 39)),
        (stage_of(run(), "build"), None),
    ],
    ids=[
        "the first failed section",
        "the running section",
        "the last line without sections",
        "nothing without rows",
    ],
)
def test_the_log_cursor_lands_by_default_on(the_stage, expected):
    assert view.default_anchor(the_stage) == expected


def test_the_default_landing_follows_the_running_test():
    for running in (2, 3):
        landing = view.default_anchor(stage_of(running_test(running), "target_suite"))
        assert landing == ("target_suite", sid(TESTS[running]), 0)


def test_running_and_failed_sections_open_by_default_and_a_fold_overrides_either_way():
    sections = stage_of(running_test(3, failed=(1,)), "target_suite").sections
    opened = {section.name for section in sections if view.is_open({}, section)}
    assert opened == {TESTS[1], TESTS[3]}

    folds = {sid(TESTS[0]): True, sid(TESTS[1]): False}
    overridden = {section.name for section in sections if view.is_open(folds, section)}
    assert overridden == {TESTS[0], TESTS[3]}


def test_the_rows_are_the_stages_lines_then_each_header_and_its_body_folded_or_not():
    host = suite("host_suite", ["passed", "failed"], {0: ("a",)}, {1: ("E boom",)}, lead=("pre",))
    assert [(row.kind, row.text) for row in view.rows(host)] == [
        ("line", "pre"),
        ("head", "case_00"),
        ("body", "a"),
        ("head", "case_01"),
        ("body", "E boom"),
    ]


KAS_LINES = [
    "2026-11-10 03:10:00 - INFO     - NOTE: first",
    "2026-11-10 03:10:01 - WARNING  - NOTE: second",
    "2026-11-10 03:10:02 - ERROR    - NOTE: third",
    "NOTE: bare",
]


def test_a_plain_stages_rows_drop_the_kas_prefix_and_search_matches_what_is_shown():
    shown = stage("posted", "open", t(3, 10), lines=KAS_LINES)
    assert [row.text for row in view.rows(shown)] == [
        "NOTE: first",
        "NOTE: second",
        "NOTE: third",
        "NOTE: bare",
    ]
    assert shown.lines == tuple(KAS_LINES)
    assert view.search(shown, ("posted", None, 2), "2026", 1) is None
    assert view.search(shown, ("posted", None, 2), "first", 1).anchor == ("posted", None, 0)


def test_the_kas_prefix_is_dropped_only_at_the_start_of_a_plain_stages_own_line():
    inner = "2026-11-10 03:10:00 - INFO     - NOTE: in a section body"
    midline = "NOTE: retry at 2026-11-10 03:10:01 - INFO     - NOTE: again"
    shown = suite("host_suite", ["failed"], bodies={0: [inner]}, lead=[midline])
    texts = [row.text for row in view.rows(shown)]
    assert midline in texts
    assert inner in texts


HOST = stage_of(LONG, "host_suite")


def found(at, pattern, direction):
    """The text of the row `view.search` finds in the long run's host suite from row `at`."""
    row = view.search(HOST, view.rows(HOST)[at].anchor, pattern, direction)
    return None if row is None else row.text


def test_search_covers_folded_bodies_and_steps_both_ways_and_wraps():
    assert not any(view.is_open({}, section) for section in HOST.sections)
    assert found(0, "boot", 1) == "boot one"
    assert found(5, "boot", 1) == "boot two"
    assert found(5, "boot", -1) == "boot one"
    assert found(len(view.rows(HOST)) - 1, "boot", 1) == "boot one"


@pytest.mark.parametrize("pattern", ["BOOT", "nothing"], ids=["case-sensitive", "unmatched"])
def test_a_search_that_misses_finds_nothing(pattern):
    assert found(5, pattern, 1) is None


# -- the Queue tab ---------------------------------------------------------------------------


def queue_rows(runs, bench=IDLE, queue=None):
    """The Queue rows for `runs` and the read `queue`."""
    return resolve(runs, bench, queue=queue).queue_rows


def test_the_queue_has_a_running_row_then_hand_entries_then_the_merge_entry():
    rows = queue_rows(THREE, LIVE, [HAND1, HAND2, MERGE])
    assert [(r.marker, r.source, r.what) for r in rows] == [
        ("▶", "merge queue", "PR #31"),
        ("2", "by hand", "feature-a"),
        ("3", "by hand", "feature-b"),
        ("4", "merge queue", "PR #14 Add slot check"),
    ]


def test_with_no_running_run_the_markers_start_at_1():
    assert [r.marker for r in queue_rows(THREE, IDLE, [HAND1, HAND2, MERGE])] == ["1", "2", "3"]


def test_a_merge_entry_is_hidden_when_its_sha_equals_the_running_sha():
    same = {**MERGE, "sha": SHA}
    assert [r.marker for r in queue_rows([live("build")], LIVE, [HAND1, same])] == ["▶", "2"]
    assert [r.marker for r in queue_rows([live("build")], IDLE, [HAND1, same])] == ["1", "2"]


def test_a_merge_entry_with_no_sha_draws_an_empty_sha_and_is_not_the_running_one():
    hand_run = live("checkout", kind="test", pr=None, ref="feature-a", sha=None)
    rows = queue_rows([hand_run], LIVE, [WAITING])
    assert [(r.what, r.sha7) for r in rows] == [("feature-a", ""), ("PR #15 Fix probe", "")]


def test_the_running_row_takes_its_title_from_the_merge_entry_with_its_pr():
    same = {**MERGE, "pr": 31, "sha": SHA, "title": "Add slot check"}
    (row, *_) = queue_rows([live("build")], LIVE, [same])
    assert row.what == "PR #31 Add slot check"


def test_the_running_row_keeps_its_title_when_the_merge_group_is_rebuilt_mid_run():
    rebuilt = {**MERGE, "pr": 31, "sha": "e" * 40, "title": "Add slot check"}
    rows = queue_rows([live("build")], LIVE, [rebuilt])
    assert [r.what for r in rows] == ["PR #31 Add slot check", "PR #31 Add slot check"]


def test_the_running_row_stays_pr_only_when_no_merge_entry_has_its_pr():
    (row, *_) = queue_rows([live("build")], LIVE, [MERGE])
    assert row.what == "PR #31"


def test_a_queue_not_read_yet_or_empty_has_no_rows_and_no_selection():
    for read in (None, []):
        rows = queue_rows([run()], IDLE, read)
        assert (rows, view.queue_index(None, rows)) == ([], None)


def test_a_hand_row_carries_its_ref_sha7_local_added_time_kind_and_id(local_zone):
    (row,) = queue_rows([run()], IDLE, [HAND1])
    assert (row.source, row.what, row.sha7, row.added) == (
        "by hand",
        "feature-a",
        "1111111",
        "06:15",
    )
    assert (row.stage, row.kind, row.id) == ("", "test", 3)


def test_a_merge_entry_row_has_its_pr_sha7_no_added_time_and_no_id():
    (row,) = queue_rows([run()], IDLE, [MERGE])
    assert (row.source, row.what, row.sha7, row.added) == (
        "merge queue",
        "PR #14 Add slot check",
        "9f8e7d6",
        "",
    )
    assert (row.stage, row.kind, row.id) == ("", "run", None)


def test_the_running_row_comes_from_the_newest_run_with_its_open_stage():
    (row,) = queue_rows([live("build")], LIVE, [])
    assert (row.marker, row.source, row.what, row.sha7) == ("▶", "merge queue", "PR #31", SHA[:7])
    assert (row.added, row.stage, row.kind, row.id) == ("", "build", "run", None)


def test_a_running_hand_run_reads_by_hand_with_its_ref_and_a_blank_sha_until_known():
    hand_run = live("checkout", kind="test", pr=None, ref="feature-a", sha=None)
    (row,) = queue_rows([hand_run], LIVE, [])
    assert (row.source, row.what, row.sha7, row.kind) == ("by hand", "feature-a", "", "test")
    assert (row.stage, row.id) == ("checkout", None)


def test_no_running_row_when_the_bench_is_not_running_the_newest_run():
    assert queue_rows([run()], IDLE, []) == []


def test_the_running_row_shows_before_the_queue_has_been_read():
    assert [(r.marker, r.stage) for r in queue_rows([live("build")], LIVE)] == [("▶", "build")]


def kept(before, index, after, runs=None, bench=IDLE):
    """Where the Queue selection lands after the read `after`, from row `index` of `before`.

    Returns its index and the row there.
    """
    runs = runs or [run()]
    old, new = queue_rows(runs, bench, before), queue_rows(runs, bench, after)
    at = view.queue_index((view.identity(old[index]), index), new)
    return at, new[at]


def test_the_selection_is_the_first_row_until_one_is_chosen():
    assert view.queue_index(None, queue_rows([run()], IDLE, [HAND1, HAND2])) == 0


def test_the_selection_keeps_its_merge_queue_row_by_pr_when_rows_before_it_go_away():
    assert kept([HAND1, MERGE, WAITING], 2, [MERGE, WAITING])[1].what == "PR #15 Fix probe"


def test_the_selection_follows_a_merge_queue_pr_that_moves_to_another_position():
    at, row = kept([MERGE, WAITING], 0, [WAITING, MERGE])
    assert (at, row.what) == (1, "PR #14 Add slot check")


def test_the_selection_keeps_its_hand_row_when_rows_before_it_go_away():
    at, row = kept([HAND1, HAND2, MERGE], 1, [HAND2, MERGE])
    assert (at, row.id) == (0, 4)


def test_the_selection_keeps_a_merge_entry_when_a_hand_row_is_added_before_it():
    third = {**HAND1, "id": 5, "ref": "feature-c"}
    at, row = kept([HAND1, HAND2, MERGE], 2, [HAND1, HAND2, third, MERGE])
    assert (at, row.source) == (3, "merge queue")


def test_the_selection_keeps_the_running_row_when_hand_rows_arrive():
    at, row = kept([], 0, [HAND1], THREE, LIVE)
    assert (at, row.marker) == (0, "▶")


def test_a_selected_row_that_goes_leaves_its_index_held_to_the_last_row():
    assert kept([HAND1, HAND2, MERGE], 0, [HAND2, MERGE])[1].id == 4
    assert kept([HAND1, HAND2, MERGE], 1, [HAND1, MERGE])[1].source == "merge queue"
    assert kept([HAND1, HAND2], 1, [HAND1])[0] == 0


# -- the `:run` preview -----------------------------------------------------------------------

CLEAN = {"sha": "1234567" + "0" * 33, "subject": "Fix the thing", "clean": True}


def previewed(typed, result, ref="feature-c", queue=(), bench=IDLE):
    """The preview lines while `typed` is in the command line and `ref`'s result is in."""
    snap = {"queue": list(queue), "preview": (ref, result)}
    return resolve(THREE, bench, prompt=typed, **snap).preview_lines


def test_a_resolved_ref_previews_what_will_queue_where_it_joins_and_after_what():
    assert previewed(":run feature-c", CLEAN, queue=[HAND1, HAND2, MERGE]) == [
        "will queue",
        'resolves to 1234567 "Fix the thing", clean tree',
        "joins as 3, after feature-b",
    ]


def test_a_dirty_checkout_says_so_in_place_of_clean_tree():
    lines = previewed(":run feature-c", {**CLEAN, "clean": False}, queue=[HAND1])
    assert lines[1] == 'resolves to 1234567 "Fix the thing", uncommitted changes in the checkout'


def test_the_first_place_has_no_after():
    assert previewed(":run feature-c", CLEAN, queue=[MERGE])[2] == "joins as 1"


def test_a_running_run_counts_as_place_1_and_is_what_the_first_hand_entry_joins_after():
    assert previewed(":run feature-c", CLEAN, bench=LIVE)[2] == "joins as 2, after PR #31"
    both = previewed(":run feature-c", CLEAN, queue=[HAND1], bench=LIVE)
    assert both[2] == "joins as 3, after feature-a"


def test_an_unresolvable_ref_previews_only_that_it_does_not_resolve():
    assert previewed(":run nope", "fatal: bad revision", ref="nope") == ["nope does not resolve"]


def test_a_preview_of_another_ref_than_the_one_typed_is_not_shown():
    assert previewed(":run feature-d", CLEAN) is None


def test_the_ref_typed_is_stripped_before_it_is_compared_with_the_preview():
    assert previewed(":run   feature-c  ", CLEAN) is not None


def test_no_preview_without_the_run_prompt_or_a_result():
    assert previewed(":ru", CLEAN) is None
    assert previewed(":run ", CLEAN) is None
    assert previewed(None, CLEAN) is None
    assert resolve([run()], prompt=":run feature-c", queue=[]).preview_lines is None


@pytest.mark.parametrize(
    ("typed", "ref"),
    [
        (":run feature-c", "feature-c"),
        (":run \t  feature-c  ", "feature-c"),
        (":run", None),
        (":run    ", None),
        (":runfeature-c", None),
        (":rn a", None),
        (None, None),
    ],
)
def test_typed_ref_is_the_stripped_ref_after_run_and_space(typed, ref):
    assert view.typed_ref(typed) == ref


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("HEAD", (None, "HEAD")),
        ("-C /x HEAD", ("/x", "HEAD")),
        ("-C /x my branch", ("/x", "my branch")),
        ("-C /x", (None, "-C /x")),
    ],
    ids=[
        "no-directory",
        "directory-and-ref",
        "multi-word-ref",
        "no-ref",
    ],
)
def test_split_run_takes_the_directory_after_dash_c_and_leaves_any_other_text_whole(text, expected):
    assert view.split_run(text) == expected
