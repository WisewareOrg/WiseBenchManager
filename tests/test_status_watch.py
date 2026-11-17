"""`status`: the watch view driven through Textual's Pilot, and once through a real terminal."""

import fcntl
import os
import pty
import select
import stat
import struct
import subprocess
import sys
import termios
import time
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from helpers import (
    add_settings,
    has_commit,
    run_unit_settings,
    source_repo,
    stub_merge_queue,
    write_bench_toml,
)
from status_helpers import (
    DISABLED,
    IDLE,
    LIVE,
    NOW,
    OLDER,
    OLDEST,
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
)
from textual.color import Color
from textual.geometry import Region
from textual.widgets import Label

from wisebench import status
from wisebench.queue import Queue

SIZE = (120, 34)
HAND = {
    "kind": "test",
    "id": 3,
    "added": "2026-11-10T03:15:00+00:00",
    "ref": "feature-a",
    "sha": "1" * 40,
    "set": None,
    "skip_build": False,
    "args": [],
}
MERGE = {"kind": "run", "pr": 14, "title": "Add slot check", "sha": "9" * 40}
CLEAN = {"sha": "1234567" + "0" * 33, "subject": "Fix the thing", "clean": True}
PALETTE = {name: Color.parse(value) for name, value in status.PALETTE.items()}


class Fixed(datetime):
    """`datetime` whose `now` is the tests' NOW."""

    @classmethod
    def now(cls, tz=None):
        return NOW


@pytest.fixture
def world(monkeypatch):
    """What the app reads: runs, a bench and an autospec `Queue`, each set by the test."""
    world = SimpleNamespace(runs=[run()], bench=IDLE, queue=create_autospec(Queue, instance=True))
    world.queue.entries.return_value = []
    world.queue.preview.return_value = CLEAN
    world.queue.add.return_value = (5, "5" * 40, 1)
    world.queue.remove.return_value = True
    monkeypatch.setattr(status, "Records", lambda _: SimpleNamespace(runs=lambda: world.runs))
    monkeypatch.setattr(status, "read_bench", lambda *_: world.bench)
    monkeypatch.setattr(status, "Queue", lambda *_: world.queue)
    monkeypatch.setattr(status, "datetime", Fixed)
    return world


@pytest.fixture
async def pilot(world, bench_clone):
    """The app running headless over `world`, its first read in."""
    app = status.WatchApp(bench_clone.checkout, bench_clone.records)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        yield pilot


async def settle(pilot):
    """Let every worker finish and every message land."""
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def show(pilot):
    """Read the records again after the test changed what they hold."""
    pilot.app.screen.read()
    await settle(pilot)


async def press(pilot, *keys):
    """Press keys, then let what they started finish."""
    await pilot.press(*keys)
    await settle(pilot)


def one(pilot, selector):
    """The screen's one widget matching a type or selector."""
    return pilot.app.screen.query_one(selector)


def text(widget):
    """What a label-like widget shows, as plain text."""
    return str(widget.render())


def lines(widget):
    """Each screen line a widget draws, as plain text."""
    return [strip.text.rstrip() for strip in widget.render_lines(Region(0, 0, *widget.size))]


def cells(pilot, table_id):
    """Each row of a table as its cells' plain text."""
    table = one(pilot, table_id)
    return [[cell.plain.strip() for cell in table.get_row_at(i)] for i in range(table.row_count)]


def stage_rows(pilot):
    """Each shown stage row's text and classes."""
    rows = pilot.app.screen.query(".stage").results(Label)
    return [(text(row).rstrip(), set(row.classes)) for row in rows if row.display]


def focused(pilot):
    """The type name of the focused widget."""
    return type(pilot.app.focused).__name__


def message(pilot):
    """The message row's text."""
    return text(one(pilot, "#message"))


# -- Run: the stage list ----------------------------------------------------------------------


async def test_the_stage_list_shows_each_stage_with_its_glyph_start_and_took(
    pilot, world, local_zone
):
    world.runs, world.bench = [live("build")], LIVE
    await show(pilot)
    assert [row for row, _ in stage_rows(pilot)][:4] == [
        "✓ checkout       06:00:00  0m02s",
        "✓ precondition   06:05:00  0m02s",
        "● build          06:10:00  417m30s",
        "· bundle",
    ]


async def test_the_shown_stage_follows_the_run_until_one_is_chosen(pilot, world):
    world.runs, world.bench = [live("build")], LIVE
    await show(pilot)
    assert one(pilot, status.RunView).stage.name == "build"
    world.runs = [live("bundle")]
    await show(pilot)
    assert one(pilot, status.RunView).stage.name == "bundle"
    await press(pilot, "k")
    world.runs = [live("host_suite")]
    await show(pilot)
    assert one(pilot, status.RunView).stage.name == "build"


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        (["j"], "checkout"),
        (["k"], "posted"),
        (["j", "j", "k"], "checkout"),
        (["end"], "posted"),
        (["G", "home"], "checkout"),
        (["k", "j"], "posted"),
    ],
    ids=["j from the summary", "k from the summary", "j j k", "End", "G then Home", "clamped"],
)
async def test_the_stage_list_keys_move_the_stage_shown(pilot, keys, expected):
    await press(pilot, *keys)
    assert one(pilot, status.RunView).stage.name == expected


async def test_only_the_shown_stage_row_is_marked_shown(pilot, world, local_zone):
    world.runs, world.bench = [live("build")], LIVE
    await show(pilot)
    shown = [row for row, classes in stage_rows(pilot) if "shown" in classes]
    assert shown == ["● build          06:10:00  417m30s"]


# -- Run: the header ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("runs", "bench", "keys", "header"),
    [
        ([live("build")], LIVE, [], "PR #31  a1b2c3d  record 20261110T030715Z"),
        ([run()], IDLE, [], "last run  PR #31  a1b2c3d  record 20261110T030715Z"),
        (
            [run(), failing_at(OLDER, "bundle")],
            IDLE,
            ["left_square_bracket"],
            "PR #31  a1b2c3d  record 20261109T030715Z  failed  ended 07:02:00",
        ),
        ([], IDLE, [], "0 records in"),
    ],
    ids=["live", "idle", "past", "no runs"],
)
async def test_the_header_names_the_run_and_its_state(
    pilot, world, local_zone, runs, bench, keys, header
):
    world.runs, world.bench = runs, bench
    await show(pilot)
    await press(pilot, *keys)
    assert text(one(pilot, status.RunHeader)).startswith(header)


async def test_brackets_step_through_older_and_newer_runs_and_1_comes_back(pilot, world):
    world.runs = [run(), run(OLDER), run(OLDEST)]
    await show(pilot)
    await press(pilot, *["left_square_bracket"] * 3)
    assert one(pilot, status.RunView).run.record == OLDEST
    await press(pilot, "right_square_bracket")
    assert one(pilot, status.RunView).run.record == OLDER
    assert str(one(pilot, "#run-tab").label) == f"1 Run  {OLDER}"
    await press(pilot, "1")
    assert one(pilot, status.RunView).run.record == STAMP


# -- Run: the pane's forms ----------------------------------------------------------------------


async def test_the_summary_is_the_result_its_end_and_length_and_a_row_per_suite(
    pilot, world, local_zone
):
    world.runs = [run(host_suite=suite("host_suite", ["passed", "passed"]))]
    await show(pilot)
    assert text(one(pilot, status.Head)).splitlines() == [
        "passed  ended 07:02:00, took 62m",
        "host suite    2 passed",
    ]
    assert not one(pilot, status.LogView).display


async def test_a_live_build_shows_its_progress_bar_and_running_tasks_oldest_first(pilot, world):
    world.runs, world.bench = [building(3)], LIVE
    await show(pilot)
    head = text(one(pilot, status.Head)).splitlines()
    assert head == [
        "build  task 1204 of 3311  36%",
        "▉" * 33,
        "running now",
        "r0            do_compile    416m30s",
        "r1            do_compile    416m29s",
        "r2            do_compile    416m28s",
    ]
    assert lines(one(pilot, status.LogView))[0] == "NOTE: Running task 1204 of 3311"
    assert text(one(pilot, "#output-label")) == "this stage's output"


async def test_a_past_build_shows_no_running_tasks(pilot, world):
    world.runs = [run(), replace(building(3), record=OLDER)]
    await show(pilot)
    await press(pilot, "left_square_bracket")
    assert text(one(pilot, status.Head)).splitlines()[0] == "build  task 1204 of 3311  36%"
    assert "running now" not in text(one(pilot, status.Head))


async def test_a_suite_shows_its_counts_its_sections_and_the_hint(pilot, world):
    world.runs, world.bench = [running_test(2, failed=(1,))], LIVE
    await show(pilot)
    assert text(one(pilot, status.Head)) == "target suite  3 of 6  1 passed  1 failed"
    assert lines(one(pilot, status.LogView))[:7] == [
        "▸ ✓ setup install_candidate  1m00s",
        "▸ ✓ t.py::test_0  1m00s",
        "▾ ✗ t.py::test_1  1m00s",
        "      out 1",
        "▾ ● t.py::test_2  399m30s",
        "      out 2",
        "  · t.py::test_3",
    ]
    assert one(pilot, "#hint").display


async def test_a_failure_box_shows_the_disabled_reason_over_the_stages_output(
    pilot, world, bench_clone, local_zone
):
    errored = stage("target_suite", "error", lines=["ota: reboot", "ota: ssh: no answer"])
    world.runs = [run(result="error", target_suite=errored)]
    world.bench = DISABLED
    (bench_clone.records / "DISABLED").write_text("2026-11-10T03:14:02+00:00\nssh: no answer\n")
    await show(pilot)
    box = one(pilot, status.FailureBox)
    assert box.display
    assert text(box).splitlines() == [
        "bench failure, not the candidate's",
        "ssh: no answer",
        "from the DISABLED file, 06:14:02",
    ]
    assert lines(one(pilot, status.LogView))[:2] == ["ota: reboot", "ota: ssh: no answer"]
    await press(pilot, "colon", "e")
    assert one(pilot, "#command").value == ":e"


# -- Run: the log -------------------------------------------------------------------------------


async def test_enter_moves_into_a_log_with_rows_and_esc_back_to_the_stages(pilot, world):
    world.runs, world.bench = [live("build", build=plain("build", 5))], LIVE
    await show(pilot)
    await press(pilot, "enter")
    assert focused(pilot) == "LogView"
    await press(pilot, "escape")
    assert focused(pilot) == "StageList"


async def test_enter_stays_on_the_stages_when_the_log_has_no_rows(pilot):
    await press(pilot, "j", "enter")
    assert focused(pilot) == "StageList"


async def test_a_plain_logs_cursor_starts_on_its_last_row_and_keys_move_it(pilot, world):
    world.runs, world.bench = [live("build", build=plain("build", 100))], LIVE
    await show(pilot)
    log = one(pilot, status.LogView)
    half = log.size.height // 2
    assert log.cursor == 99
    await press(pilot, "enter", "k", "k", "ctrl+u")
    assert log.cursor == 97 - half
    await press(pilot, "home", "j")
    assert log.cursor == 1
    await press(pilot, "ctrl+d")
    assert log.cursor == 1 + half


async def test_g_follows_the_live_log_as_it_grows_and_a_move_up_stops_following(pilot, world):
    world.runs, world.bench = [live("build", build=plain("build", 100))], LIVE
    await show(pilot)
    log = one(pilot, status.LogView)
    await press(pilot, "enter", "home", "G")
    world.runs = [live("build", build=plain("build", 150))]
    await show(pilot)
    assert log.cursor == 149
    assert lines(log)[-1] == "build line 149"
    await press(pilot, "k")
    world.runs = [live("build", build=plain("build", 200))]
    await show(pilot)
    assert log.cursor == 148


async def test_a_suites_cursor_starts_on_its_first_failed_section(pilot, world):
    world.runs, world.bench = [running_test(3, failed=(1,))], LIVE
    await show(pilot)
    log = one(pilot, status.LogView)
    assert log.rows.anchor(log.cursor) == (sid(TESTS[1]), 0)


@pytest.mark.parametrize("key", ["enter", "space"])
async def test_enter_or_space_on_a_section_folds_and_unfolds_it(pilot, world, key):
    world.runs, world.bench = [running_test(3, failed=(1,))], LIVE
    await show(pilot)
    log = one(pilot, status.LogView)
    await press(pilot, "enter")
    assert lines(log)[2:4] == ["▾ ✗ t.py::test_1  1m00s", "      out 1"]
    await press(pilot, key)
    assert lines(log)[2:4] == ["▸ ✗ t.py::test_1  1m00s", "▸ ✓ t.py::test_2  1m00s"]
    await press(pilot, key)
    assert lines(log)[3] == "      out 1"


async def test_a_fold_key_on_a_body_line_folds_its_section_onto_its_header(pilot, world):
    world.runs, world.bench = [running_test(3, failed=(1,))], LIVE
    await show(pilot)
    log = one(pilot, status.LogView)
    await press(pilot, "enter", "j", "space")
    assert log.rows.anchor(log.cursor) == (sid(TESTS[1]), 0)
    assert lines(log)[2] == "▸ ✗ t.py::test_1  1m00s"


async def test_search_finds_text_in_folded_sections_unfolds_them_and_n_and_n_step(pilot, world):
    bodies = {0: ("boot one",), 3: ("boot two",)}
    world.runs = [run(host_suite=suite("host_suite", ["passed"] * 5, bodies))]
    await show(pilot)
    await press(pilot, *["j"] * 5)
    log = one(pilot, status.LogView)
    await press(pilot, "slash", *"boot", "enter")
    assert focused(pilot) == "LogView"
    assert log.rows.anchor(log.cursor) == (("host_suite", "test", "case_00", 0), 1)
    await press(pilot, "n")
    assert log.rows.anchor(log.cursor) == (("host_suite", "test", "case_03", 0), 1)
    await press(pilot, "n")
    assert log.rows.anchor(log.cursor) == (("host_suite", "test", "case_00", 0), 1)
    await press(pilot, "N")
    assert log.rows.anchor(log.cursor) == (("host_suite", "test", "case_03", 0), 1)
    assert "      boot two" in lines(log)


async def test_a_search_that_misses_moves_nothing(pilot, world):
    world.runs, world.bench = [live("build", build=plain("build", 10))], LIVE
    await show(pilot)
    await press(pilot, "slash", *"nowhere", "enter")
    assert one(pilot, status.LogView).cursor == 9
    assert focused(pilot) == "StageList"


async def test_the_prefix_kas_writes_is_dropped_from_a_stages_own_lines(pilot, world):
    build = stage("build", "open", lines=["2026-11-10 03:00:00 - INFO     - NOTE: x"])
    world.runs, world.bench = [live("build", build=build)], LIVE
    await show(pilot)
    assert lines(one(pilot, status.LogView))[0] == "NOTE: x"


async def test_log_text_reaches_the_screen_with_its_controls_shown_not_acted_on(pilot, world):
    build = stage("build", "open", lines=["\x1b]0;owned\x07 \x9b2J"])
    world.runs, world.bench = [live("build", build=build)], LIVE
    await show(pilot)
    assert lines(one(pilot, status.LogView))[0] == "^[]0;owned^G <9b>2J"


# -- views, keys and quitting ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("keys", "view"),
    [
        (["tab"], "queue"),
        (["tab", "tab", "tab"], "run"),
        (["shift+tab"], "history"),
        (["2"], "queue"),
        (["3"], "history"),
        (["3", "1"], "run"),
        (["h"], "run"),
        (["l"], "run"),
    ],
)
async def test_tab_shift_tab_and_the_numbers_switch_views_and_wrap(pilot, keys, view):
    await press(pilot, *keys)
    assert pilot.app.screen.current == view


async def test_question_mark_toggles_the_keys_panel_which_lists_the_commands(pilot):
    await press(pilot, "question_mark")
    panel = pilot.app.screen.query_one("HelpPanel")
    assert ":run [-C <dir>] <ref>" in str(panel.query_one("Markdown").source)
    await press(pilot, "question_mark")
    assert not pilot.app.screen.query("HelpPanel")


@pytest.mark.parametrize("keys", [["q"], ["colon", "q", "enter"], ["semicolon", "q", "enter"]])
async def test_q_and_colon_q_quit_with_0(pilot, keys):
    await pilot.press(*keys)
    await pilot.pause()
    assert pilot.app.return_code == 0


async def test_the_command_line_takes_typed_keys_and_esc_closes_it(pilot):
    await press(pilot, "colon", *"q2")
    assert one(pilot, "#command").value == ":q2"
    assert pilot.app.screen.current == "run"
    await press(pilot, "escape")
    assert not one(pilot, "#command-row").display


# -- History --------------------------------------------------------------------------------------


async def test_history_lists_every_run_and_counts_the_records(pilot, world):
    world.runs = [run(), failing_at(OLDER, "host_suite"), run(OLDEST, kind="test", pr=None)]
    await show(pilot)
    await press(pilot, "3")
    assert cells(pilot, "#history-table") == [
        [STAMP, "PR #31", "a1b2c3d", "passed", "62m", ""],
        [OLDER, "PR #31", "a1b2c3d", "failed", "62m", "host suite"],
        [OLDEST, "main", "a1b2c3d", "passed", "62m", "by hand"],
    ]
    assert text(one(pilot, "#history-footer")).startswith("3 records in ")


async def test_history_selects_the_run_shown_and_enter_opens_the_selection(pilot, world):
    world.runs = [run(), run(OLDER), run(OLDEST)]
    await show(pilot)
    await press(pilot, "left_square_bracket", "3")
    assert one(pilot, "#history-table").cursor_row == 1
    await press(pilot, "j", "enter")
    assert pilot.app.screen.current == "run"
    assert one(pilot, status.RunView).run.record == OLDEST


async def test_a_live_refresh_redraws_only_the_running_row(pilot, world):
    world.runs, world.bench = [live("build"), *[run(f"2026{i:04d}") for i in range(30)]], LIVE
    await show(pilot)
    await press(pilot, "3")
    assert cells(pilot, "#history-table")[0][3:5] == ["running", "427m"]
    table = one(pilot, "#history-table")
    updated = []
    real = table.update_cell_at
    table.update_cell_at = lambda at, value, **kw: updated.append(at) or real(at, value, **kw)
    world.runs = [replace(live("build"), pr=32), *world.runs[1:]]
    await show(pilot)
    assert updated
    assert {row for row, _ in updated} == {0}


# -- Queue ----------------------------------------------------------------------------------------


async def test_entering_the_queue_reads_it_and_lists_running_hand_then_merge_rows(pilot, world):
    world.runs, world.bench = [live("build")], LIVE
    world.queue.entries.return_value = [HAND, MERGE]
    await show(pilot)
    await press(pilot, "2")
    world.queue.entries.assert_called_once_with()
    assert [row[:3] + row[5:] for row in cells(pilot, "#queue-table")] == [
        ["▶", "merge queue", "PR #31", "running, build"],
        ["2", "by hand", "feature-a", ""],
        ["3", "merge queue", "PR #14 Add slot check", ""],
    ]


async def test_a_failed_queue_read_says_why_and_keeps_the_last_rows(pilot, world):
    world.queue.entries.return_value = [HAND]
    await press(pilot, "2")
    world.queue.entries.side_effect = OSError("no network")
    pilot.app.screen.read_queue()
    await settle(pilot)
    assert message(pilot) == "no network"
    assert len(cells(pilot, "#queue-table")) == 1


async def test_the_queue_is_read_again_on_its_interval_only_while_it_shows(pilot, world):
    await press(pilot, "3")
    pilot.app.screen.tick_queue()
    await settle(pilot)
    world.queue.entries.assert_not_called()
    await press(pilot, "2")
    pilot.app.screen.tick_queue()
    await settle(pilot)
    assert world.queue.entries.call_count == 2


async def test_the_queue_selection_stays_on_its_row_when_rows_before_it_go(pilot, world):
    other = {**HAND, "id": 4, "ref": "feature-b"}
    world.queue.entries.return_value = [HAND, other, MERGE]
    await press(pilot, "2", "j", "j")
    world.queue.entries.return_value = [other, MERGE]
    pilot.app.screen.read_queue()
    await settle(pilot)
    assert one(pilot, status.QueueView).selected().key == "merge 14"


async def test_run_previews_the_typed_ref_and_enter_queues_it(pilot, world):
    world.queue.entries.return_value = [HAND]
    await press(pilot, "colon", *"run feature-c")
    assert pilot.app.screen.current == "queue"
    world.queue.preview.assert_called_with("feature-c", None)
    assert text(one(pilot, "#preview")).splitlines() == [
        "will queue",
        'resolves to 1234567 "Fix the thing", clean tree',
        "joins as 2, after feature-a",
    ]
    await press(pilot, "enter")
    world.queue.add.assert_called_once_with("feature-c", None, False, [], None)
    assert message(pilot) == "queued 5 5555555"
    assert world.queue.entries.call_count == 2


async def test_the_preview_follows_the_ref_typed_and_goes_with_the_command_line(pilot, world):
    world.queue.preview.side_effect = lambda ref, source: {**CLEAN, "subject": ref}
    await press(pilot, "colon", *"run feature-c", "backspace")
    assert '"feature-"' in text(one(pilot, "#preview"))
    await press(pilot, "escape")
    assert not one(pilot, "#preview").display


async def test_run_of_an_unresolvable_ref_says_so(pilot, world):
    world.queue.add.side_effect = subprocess.CalledProcessError(128, "git")
    await press(pilot, "colon", *"run nope", "enter")
    assert message(pilot) == "nope does not resolve to a commit"


@pytest.mark.parametrize(
    ("down", "removed", "said"),
    [(1, 3, "removed queued run 3"), (2, None, "only hand-queued runs can be removed")],
    ids=["hand row", "merge row"],
)
async def test_remove_removes_only_the_selected_hand_row(pilot, world, down, removed, said):
    world.runs, world.bench = [live("build")], LIVE
    world.queue.entries.return_value = [HAND, MERGE]
    await show(pilot)
    await press(pilot, "2", *["j"] * down, "colon", *"remove", "enter")
    if removed:
        world.queue.remove.assert_called_once_with(removed)
    else:
        world.queue.remove.assert_not_called()
    assert message(pilot) == said


# -- the bench commands ---------------------------------------------------------------------------


@pytest.fixture
def unit():
    """A `[run] unit` in the bench's settings file."""
    add_settings(run_unit_settings())


async def test_disable_stops_the_timer_and_says_so(unit, pilot, systemctl):
    await press(pilot, "colon", *"disable", "enter")
    assert ["systemctl", "--user", "stop", "bench-run.timer"] in systemctl.calls
    assert message(pilot) == "bench disabled"


async def test_enable_asks_first_and_y_enables(unit, pilot, systemctl, bench_clone):
    (bench_clone.records / "DISABLED").write_text("x\n")
    await press(pilot, "colon", *"enable", "enter")
    assert "Enable the bench?" in message(pilot)
    assert (bench_clone.records / "DISABLED").exists()
    await press(pilot, "y")
    assert not (bench_clone.records / "DISABLED").exists()
    assert ["systemctl", "--user", "start", "bench-run.timer"] in systemctl.calls
    assert message(pilot) == "bench enabled"


@pytest.mark.parametrize("key", ["n", "escape", "2", "q"])
async def test_any_other_key_after_enable_cancels_it_and_does_nothing_else(
    unit, pilot, bench_clone, key
):
    (bench_clone.records / "DISABLED").write_text("x\n")
    await press(pilot, "colon", *"enable", "enter", key)
    assert (bench_clone.records / "DISABLED").exists()
    assert pilot.app.screen.current == "run"
    assert pilot.app.return_code is None
    assert not one(pilot, "#message").display


async def test_a_bench_command_without_a_run_unit_says_so(pilot):
    await press(pilot, "colon", *"disable", "enter")
    assert message(pilot) == "no [run] unit in the bench's settings file"


# -- colour --------------------------------------------------------------------------------------


def colour(widget, needle):
    """(foreground, background) of the first cell of `needle` as the widget draws it."""
    for strip in widget.render_lines(Region(0, 0, *widget.size)):
        if needle in strip.text:
            cut, column = strip.text.index(needle), 0
            for segment in strip:
                if column + len(segment.text) > cut:
                    style = segment.style
                    return Color.from_rich_color(style.color), Color.from_rich_color(style.bgcolor)
                column += len(segment.text)
    raise AssertionError(f"{needle!r} is not drawn")


@pytest.mark.parametrize(
    ("runs", "bench", "keys", "where", "needle", "ground", "expected"),
    [
        ([failing_at(STAMP, "bundle")], DISABLED, [], ".shown", "✗ bundle", False, "red"),
        ([live("build")], LIVE, [], ".shown", "● build", False, "amber"),
        ([live("build")], LIVE, [], ".passed", "✓ checkout", False, "green"),
        ([live("build")], LIVE, [], ".shown", "● build", True, "running-tint"),
        ([failing_at(STAMP, "bundle")], DISABLED, [], ".shown", "✗ bundle", True, "failed-tint"),
        ([run()], IDLE, ["j"], ".shown", "✓ checkout", True, "selection"),
        ([live("build")], LIVE, [], "#badge", "RUNNING", True, "amber"),
        ([run(kind="test", pr=None)], IDLE, ["3"], "#history-table", "by hand", False, "blue"),
        ([failing_at(STAMP, "bundle")], IDLE, ["3"], "#history-table", "failed", False, "red"),
        ([run()], IDLE, ["3"], "#history-table", STAMP, True, "selection"),
    ],
    ids=[
        "failed red",
        "running amber",
        "passed green",
        "running stage on the running tint",
        "failed stage on the failed tint",
        "chosen stage on the selection",
        "running badge amber",
        "by hand blue",
        "failed run red",
        "History selection",
    ],
)
async def test_each_meaning_shows_in_its_colour(
    pilot, world, runs, bench, keys, where, needle, ground, expected
):
    world.runs, world.bench = runs, bench
    await show(pilot)
    await press(pilot, *keys)
    fg, bg = colour(one(pilot, where), needle)
    assert (bg if ground else fg).rgb == PALETTE[expected].rgb


# -- cost per key and per refresh -----------------------------------------------------------------


@pytest.mark.parametrize("length", [100, 20_000])
async def test_a_key_and_a_refresh_draw_only_the_log_rows_on_screen(pilot, world, length):
    world.runs, world.bench = [live("build", build=plain("build", length))], LIVE
    await show(pilot)
    log = one(pilot, status.LogView)
    await press(pilot, "enter", "home")
    drawn, real = [], status.LogView.render_line
    status.LogView.render_line = lambda self, y: drawn.append(y) or real(self, y)
    try:
        await press(pilot, "j")
        per_key = len(drawn)
        drawn.clear()
        world.runs = [live("build", build=plain("build", length + 10))]
        await show(pilot)
        per_refresh = len(drawn)
    finally:
        status.LogView.render_line = real
    assert 0 < per_key <= log.size.height
    assert per_refresh <= log.size.height


@pytest.mark.parametrize("count", [50, 2_000])
async def test_a_history_key_draws_only_the_rows_on_screen(pilot, world, count):
    world.runs = [run(f"2026{i:05d}") for i in range(count)]
    await show(pilot)
    await press(pilot, "3")
    table = one(pilot, "#history-table")
    drawn, real = [], status.Table.render_line
    status.Table.render_line = lambda self, y: drawn.append(y) or real(self, y)
    try:
        await press(pilot, "j")
    finally:
        status.Table.render_line = real
    assert 0 < len(drawn) <= table.size.height


# -- the terminal -------------------------------------------------------------------------------


def test_not_on_a_terminal_it_says_so_and_exits_2(capsys, bench_clone):
    assert status.watch(bench_clone.checkout, bench_clone.records) == 2
    assert capsys.readouterr().err == "BENCH ERROR: bench status --watch needs a terminal\n"


def test_on_a_real_terminal_log_text_drives_nothing_the_mouse_is_off_and_ctrl_c_exits_0(
    bench_clone,
):
    """A record whose log holds an OSC title, BEL and a C1 CSI, watched in a pty."""
    record = bench_clone.records / STAMP
    record.mkdir()
    (record / "bench.log").write_text(
        "bench: 2026-11-10T03:00:00+00:00 run started kind=test ref=main\n"
        "bench: 2026-11-10T03:00:01+00:00 stage checkout started\n"
        "\x1b]0;owned\x07 \x9b2J\n"
        "bench: 2026-11-10T03:00:02+00:00 stage checkout failed\n"
        "bench: 2026-11-10T03:00:03+00:00 run failed\n"
    )
    code = (
        "import sys; from wisebench import status; "
        f"sys.exit(status.watch({str(bench_clone.checkout)!r}, {str(bench_clone.records)!r}))"
    )
    pid, fd = pty.fork()
    if pid == 0:
        try:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 34, 120, 0, 0))
            os.environ["TERM"] = "xterm-256color"
            os.execv(sys.executable, [sys.executable, "-c", code])
        finally:
            os._exit(127)
    output = b""

    def read_until(needle, seconds):
        """Collect output until `needle` appears, the child closes, or `seconds` pass."""
        nonlocal output
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and needle not in output:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                output += chunk

    try:
        read_until(b"1 Run", 20)
        os.write(fd, b"j")
        read_until(b"owned", 10)
        os.write(fd, b"\x03")
        read_until(b"\0never", 5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            done, wait_status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            time.sleep(0.05)
        else:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            pytest.fail("the view did not exit on Ctrl-C")
    finally:
        os.close(fd)

    assert os.waitstatus_to_exitcode(wait_status) == 0
    assert b"^[]0;owned^G" in output
    assert b"\x1b]0;owned" not in output
    assert b"owned\x07" not in output
    assert "\x9b".encode() not in output
    for mode in (b"1000", b"1002", b"1003", b"1006", b"1015"):
        assert b"\x1b[?" + mode + b"h" not in output


# -- a real Queue ---------------------------------------------------------------------------------


async def test_run_with_a_home_relative_directory_previews_and_queues_its_commit(
    monkeypatch, bench_clone, tmp_path
):
    """`~/src` is typed, HOME is `tmp_path`: the preview resolves in `src` and Enter queues it."""
    stub_merge_queue(monkeypatch, bench_clone.checkout)
    sha = source_repo(tmp_path / "src")
    app = status.WatchApp(bench_clone.checkout, bench_clone.records)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await press(pilot, "colon", *"run -C ~/src HEAD")
        assert f'resolves to {sha[:7]} "unpushed work"' in text(one(pilot, "#preview"))
        assert not has_commit(bench_clone.checkout, sha)
        await press(pilot, "enter")
        assert message(pilot) == f"queued 1 {sha[:7]}"
    assert has_commit(bench_clone.checkout, sha)
    held = Queue(bench_clone.checkout, bench_clone.records).entries()
    assert [(e["ref"], e["sha"]) for e in held] == [("HEAD", sha)]


def snapshot(root):
    """Every entry under root: kind, mtime_ns, and file bytes."""
    seen = {}
    for path in sorted([root, *root.rglob("*")]):
        info = path.lstat()
        body = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
        seen[str(path)] = (stat.S_IFMT(info.st_mode), info.st_mtime_ns, body)
    return seen


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores read-only modes")
async def test_the_watch_writes_nothing_takes_no_lock_and_changes_things_only_through_the_queue(
    tmp_path, monkeypatch, systemctl, bench_clone
):
    """The real records, bench state and app over a read-only tree, keys driving the Queue tab,
    `:run` and `:remove`: bytes and mtimes unchanged, no flock or lockf, no process started, the
    only `systemctl` call the timer query, and only `Queue`'s four view methods called."""
    from wisebench import queue

    record = bench_clone.records / STAMP
    record.mkdir()
    (record / "bench.log").write_text(
        "bench: 2026-11-10T03:00:00+00:00 run started kind=test ref=main\n"
    )
    write_bench_toml(bench_clone.checkout)
    add_settings(run_unit_settings())
    (bench_clone.records / "DISABLED").write_text("reason\n")
    (bench_clone.records / "lock").touch()
    forbidden, started, calls = [], [], []

    def refuse(*args, **kwargs):
        forbidden.append(args)
        raise AssertionError("a lock was taken")

    def record_popen(argv, *args, **kwargs):
        started.append(list(argv))
        raise FileNotFoundError(argv[0])

    for name, result in (
        ("entries", [HAND]),
        ("preview", CLEAN),
        ("add", (5, "5" * 40, 2)),
        ("remove", True),
    ):
        monkeypatch.setattr(
            queue.Queue, name, lambda self, *a, _n=name, _r=result: calls.append(_n) or _r
        )
    monkeypatch.setattr(fcntl, "flock", refuse)
    monkeypatch.setattr(fcntl, "lockf", refuse)
    monkeypatch.setattr(subprocess, "Popen", record_popen)
    before = snapshot(tmp_path)
    paths = [tmp_path, *tmp_path.rglob("*")]
    for path in paths:
        path.chmod(0o555 if path.is_dir() else 0o444)
    try:
        app = status.WatchApp(bench_clone.checkout, bench_clone.records)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await press(pilot, "2", "colon", *"run feature-c", "enter")
            await press(pilot, "j", "colon", *"remove", "enter")
            await press(pilot, "q")
    finally:
        for path in paths:
            path.chmod(0o755 if path.is_dir() else 0o644)

    assert forbidden == []
    assert snapshot(tmp_path) == before
    assert started == []
    assert systemctl.calls
    assert all(call[2] == "is-active" for call in systemctl.calls)
    assert {"entries", "add", "remove"} <= set(calls) <= {"entries", "preview", "add", "remove"}
