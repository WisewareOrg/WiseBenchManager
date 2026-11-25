"""`wisebench.view`: what the operator sees of the runs, the bench and the queue."""

import subprocess
import unicodedata

import pytest
from rich.console import Console
from status_helpers import (
    NOW,
    OLDER,
    SHA,
    STAMP,
    hand,
    merge,
    output,
    parsed,
    rep,
    run_log,
    setup_log,
    stage_of,
    started_log,
)

from wisebench import bench_state, view

LIVE = bench_state.Bench(live=True, disabled=False, timer="on")
IDLE = bench_state.Bench(live=False, disabled=False, timer="on")
DISABLED = bench_state.Bench(live=False, disabled=True, timer="off")
STOPPED = "2026-11-10T03:15:00+00:00\nthe board is gone\n"
PASSED = parsed(run_log())
BUILDING = parsed(run_log("build"))
BY_HAND = parsed(run_log(kind="test", ref="feature-a", pr=None))


def failed_at(name, state="failed", record=OLDER, **bodies):
    """A finished run whose stage `name` ended in `state`."""
    return parsed(run_log(name, state, **bodies), record=record)


def building(*lines):
    """A run left in its build, with the build's output."""
    return parsed(run_log("build", build=output(*lines)))


def words(rows):
    """Table rows as (key, the text of each cell)."""
    return [(key, [cell.plain.strip() for cell in cells]) for key, cells in rows]


def meaning(text, needle):
    """The meaning whose colour `needle` has in a `Text`; None for the text colour."""
    style = text.get_style_at_offset(Console(), text.plain.index(needle))
    found = style.color and style.color.name
    return next((name for name, colour in view.COLOURS.items() if colour == found), found)


# -- outside text ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("\x1b]0;t\x07", "^[]0;t^G"),
        ("a\tb", "a       b"),
        ("café 日本 ✓ 🎉 [bold]x[/]\xa0", "café 日本 ✓ 🎉 [bold]x[/]\xa0"),
    ],
    ids=["C0", "a tab", "legal text"],
)
def test_a_control_is_shown_as_text_and_the_rest_is_unchanged(raw, shown):
    assert view.clean(raw) == shown, "controls become text, legal text is kept"
    assert view.styled(raw).plain == shown, "every text drawn is cleaned"


def test_no_character_that_a_terminal_acts_on_survives():
    every = "".join(map(chr, range(0x3000)))

    left = {c for c in view.clean(every) if unicodedata.category(c) in ("Cc", "Zl", "Zp")}

    assert left == set(), "controls and line separators are all shown as text"


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("2026-11-10 03:10:00 - INFO     - NOTE: first", "NOTE: first"),
        ("at 2026-11-10 03:10:01 - INFO     - x", "at 2026-11-10 03:10:01 - INFO     - x"),
        (
            "2026-11-10 03:10:00 - INFO - 2026-11-10 03:10:01 - INFO - x",
            "2026-11-10 03:10:01 - INFO - x",
        ),
    ],
    ids=["a prefix", "a prefix inside the line", "two prefixes"],
)
def test_kass_prefix_is_dropped_once_at_the_start_of_a_line(raw, shown):
    assert view.shown_line(raw) == shown, "only the leading prefix goes"


# -- states and words typed ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "running", "shown"),
    [
        ("passed", True, ("✓", "passed")),
        ("failed", False, ("✗", "failed")),
        ("error", False, ("✗", "failed")),
        ("open", True, ("●", "running")),
        ("open", False, ("?", "dim")),
        ("skipped", True, ("–", "dim")),
        ("not-started", True, ("·", "dim")),
    ],
)
def test_a_state_has_a_glyph_and_a_meaning(state, running, shown):
    assert view.tone(state, running) == shown, "only a run being run has a running stage"


@pytest.mark.parametrize(
    ("run", "newest", "bench", "shown"),
    [
        (BUILDING, True, LIVE, "build"),
        (parsed(run_log("build", "failed")), True, LIVE, None),
        (failed_at("bundle"), True, IDLE, None),
        (failed_at("bundle"), True, DISABLED, "bundle"),
        (failed_at("precondition", "error"), True, DISABLED, "precondition"),
        (BUILDING, True, DISABLED, "build"),
        (failed_at("bundle"), False, LIVE, "bundle"),
        (BUILDING, False, IDLE, "build"),
        (PASSED, False, LIVE, None),
    ],
    ids=[
        "live: the open stage",
        "live between stages: the summary",
        "idle: the summary",
        "disabled: the failed stage",
        "disabled: the stage in error",
        "disabled: the stage left open",
        "past: its failed stage",
        "past: the stage left open",
        "past and passed: the summary",
    ],
)
def test_the_stage_shown_follows_from_the_run_and_the_bench(run, newest, bench, shown):
    found = view.auto_stage(run, newest, bench)

    assert (found and found.name) == shown, "the stage the bench's state calls for"


@pytest.mark.parametrize(
    ("typed", "run"),
    [
        (":run main", (None, "main")),
        (":run \t  main  ", (None, "main")),
        (":run -C /x main", ("/x", "main")),
        (":run -C  /x  my branch", ("/x", "my branch")),
        (":run -C /x", (None, "-C /x")),
        (":run", None),
        (":run   ", None),
        (":runx", None),
        ("x:run main", None),
    ],
)
def test_run_is_typed_with_a_ref_and_perhaps_a_directory(typed, run):
    assert view.typed_run(typed) == run, "the directory and the ref, or nothing to run"


# -- Run -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("run", "name", "running", "shown"),
    [
        (PASSED, "host_suite", True, ["✓ host suite", "06:20:00", "0m02s"]),
        (BUILDING, "build", True, ["● build", "06:10:00", "417m30s"]),
        (BUILDING, "build", False, ["? build", "06:10:00", "—"]),
        (BUILDING, "bundle", True, ["· bundle", "", ""]),
        (failed_at("bundle", "skipped"), "bundle", False, ["– bundle", "", ""]),
    ],
    ids=["passed", "running", "left open", "not started", "skipped"],
)
def test_a_stage_row_is_its_glyph_and_name_start_and_length(local_zone, run, name, running, shown):
    cells = view.stage_cells(stage_of(run, name), running, NOW)

    assert [cell.plain for cell in cells] == shown, "only a stage that ran has times"
    assert meaning(cells[0], cells[0].plain) == view.tone(stage_of(run, name).state, running)[1], (
        "the row has the colour of its state"
    )


def test_a_builds_head_is_its_progress_and_while_it_runs_its_oldest_tasks():
    start = "2026-11-10 03:10:{:02d} - INFO     - NOTE: recipe r{}-1.0-r0: task do_compile: Started"
    tasks = ["NOTE: recipe bare-1.0-r0: task do_fetch: Started"]
    tasks += [start.format(9 - i, 9 - i) for i in range(10)]
    run = building(*tasks, "NOTE: Running task 1204 of 3311")

    live = view.head(run, stage_of(run, "build"), True, NOW).plain
    past = view.head(run, stage_of(run, "build"), False, NOW).plain

    assert live.startswith("build  task 1204 of 3311  36%\n"), "phase, done of total and share"
    listed = [row.split()[0] for row in live.splitlines() if "do_" in row]
    assert listed == [f"r{i}" for i in range(8)], "the oldest eight, an unknown start last"
    assert "do_compile" not in past, "a build nobody is running has no task running"


def test_a_build_with_no_count_yet_is_at_zero():
    run = building("Loading cache...done.")

    head = view.head(run, stage_of(run, "build"), True, NOW)

    assert "parsing recipes 0 of 0  0%" in head.plain, "nothing counted is nothing done"


@pytest.mark.parametrize(
    ("stopped", "run", "shown"),
    [
        (STOPPED, PASSED, ["the board is gone", "from the DISABLED file, 06:15:00"]),
        ("garbage\nwhy\n", PASSED, ["why", "from the DISABLED file, —"]),
        ("", PASSED, ["", "from the DISABLED file, —"]),
        (
            STOPPED,
            parsed(
                run_log(
                    "target_suite",
                    "error",
                    target_suite=setup_log("board", 25)
                    + started_log("a", 26)
                    + setup_log("wifi", 27)
                    + started_log("b", 28)
                    + setup_log("install", 29)
                    + started_log("c", 30),
                ),
                [rep("a", "setup", "failed"), rep("c", "setup", "passed")],
            ),
            ["the board is gone", "in setup wifi; from the DISABLED file, 06:15:00"],
        ),
    ],
    ids=["no setup", "an unreadable time", "an unreadable file", "the last setup not passed"],
)
def test_the_failure_box_says_whose_failure_it_is_why_and_where(local_zone, stopped, run, shown):
    box = view.failure(stopped, run).plain.split("\n")

    assert box == ["bench failure, not the candidate's", *shown], "whose, why, and where"


# -- History ---------------------------------------------------------------------------------------


def test_history_is_a_row_per_run_the_newest_running_while_the_bench_is_live():
    runs = [BUILDING, failed_at("host_suite"), parsed(run_log("build"), record="unfinished")]
    runs.append(parsed(run_log(kind="test", ref="feature-a", pr=None), record="by hand"))
    runs.append(failed_at("build", "error", "in error"))

    live = words(view.history_rows(runs, LIVE, NOW))
    idle = words(view.history_rows(runs, IDLE, None))

    assert live == [
        (STAMP, [STAMP, "PR #31", SHA[:7], "running", "427m", ""]),
        (OLDER, [OLDER, "PR #31", SHA[:7], "failed", "59m", "host suite"]),
        ("unfinished", ["unfinished", "PR #31", SHA[:7], "no result", "—", ""]),
        ("by hand", ["by hand", "feature-a", SHA[:7], "passed", "59m", "by hand"]),
        ("in error", ["in error", "PR #31", SHA[:7], "bench error", "59m", "build"]),
    ], "record, what, sha, result, took, and the failing stage or `by hand`"
    assert idle[0][1][3:5] == ["no result", "—"], "a run the bench left is not running"


# -- Queue -----------------------------------------------------------------------------------------


def test_the_queue_is_the_running_run_then_by_hand_then_the_merge_queue(local_zone):
    entries = [hand(3), hand(4, "feature-b"), merge(14), merge(15, "Fix", None)]

    rows = words(view.queue_rows([BUILDING], LIVE, entries))
    idle = words(view.queue_rows([PASSED], IDLE, entries))

    assert rows == [
        ("running", ["▶", "merge queue", "PR #31", SHA[:7], "", "running, build"]),
        ("hand 3", ["2", "by hand", "feature-a", "1111111", "06:15", ""]),
        ("hand 4", ["3", "by hand", "feature-b", "1111111", "06:15", ""]),
        ("merge 14", ["4", "merge queue", "PR #14 Add slot check", "9f8e7d6", "", ""]),
        ("merge 15", ["5", "merge queue", "PR #15 Fix", "", "", ""]),
    ], "place, source, what, sha, added, and the running run's stage"
    assert [cells[0] for _, cells in idle] == ["1", "2", "3", "4"], (
        "nothing running, no place taken"
    )


@pytest.mark.parametrize(
    ("run", "entry", "label", "waiting"),
    [
        (BUILDING, merge(31, "Add it", SHA), "PR #31 Add it", []),
        (BUILDING, merge(31, "Add it", "e" * 40), "PR #31 Add it", ["merge 31"]),
        (BUILDING, merge(31, "Add it", None), "PR #31 Add it", ["merge 31"]),
        (BUILDING, merge(14), "PR #31", ["merge 14"]),
        (parsed(run_log("checkout")), merge(14, sha=None), "PR #31", ["merge 14"]),
    ],
    ids=["its entry", "its PR rebuilt", "its PR unbuilt", "another's", "nothing checked out yet"],
)
def test_the_running_run_takes_its_prs_title_and_leaves_out_its_commits_entry(
    run, entry, label, waiting
):
    (_, running), *rest = words(view.queue_rows([run], LIVE, [entry]))

    assert running[2] == label, "the title of the entry with the run's PR"
    assert [key for key, _ in rest] == waiting, "only the entry of the running commit is left out"


def test_a_running_run_by_hand_says_so():
    (_, running), *_ = words(view.queue_rows([BY_HAND], LIVE, []))

    assert running[1:3] == ["by hand", "feature-a"], "its source and its ref"


@pytest.mark.parametrize(
    ("bench", "entries", "place"),
    [
        (IDLE, [], "joins as 1"),
        (IDLE, [merge()], "joins as 1"),
        (LIVE, [merge()], "joins as 2, after PR #31"),
        (LIVE, [hand(3), hand(4, "feature-b"), merge()], "joins as 4, after feature-b"),
    ],
    ids=["empty", "a merge entry", "a running run", "runs by hand"],
)
def test_the_preview_is_the_commit_and_the_place_after_the_running_and_hand_runs(
    bench, entries, place
):
    rows = view.queue_rows([BUILDING], bench, entries)
    commit = {"sha": "1234567" + "0" * 33, "subject": "Fix it", "clean": True}

    box = view.preview("main", commit, rows).plain.splitlines()

    assert box == ["will queue", 'resolves to 1234567 "Fix it", clean tree', place], "the box"


@pytest.mark.parametrize(
    ("outcome", "shown"),
    [
        ({"sha": SHA, "subject": "Fix", "clean": False}, "uncommitted changes in the checkout"),
        (subprocess.CalledProcessError(128, ["git"]), "nope does not resolve"),
        (OSError("no git"), "nope does not resolve"),
    ],
    ids=["a dirty checkout", "an unknown ref", "any other failure"],
)
def test_the_preview_warns_of_what_would_go_wrong(outcome, shown):
    assert shown in view.preview("nope", outcome, []).plain, "the warning"
