"""`view.resolve` and `view.handle`: the frame derived from intent, and keys acting on it."""

import pytest
from status_helpers import (
    DIRECTORY,
    DISABLED,
    IDLE,
    LIVE,
    LONG,
    MANY,
    NOW,
    OLDER,
    OLDEST,
    SHA,
    SIZE,
    STAMP,
    TESTS,
    Grid,
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

from wisebench import screen, view

ENTER, ESC, HALF_DOWN, HALF_UP = "\n", "\x1b", "\x04", "\x15"
THREE = [live("build"), failing_at(OLDER, "bundle"), run(OLDEST)]
PLAIN_BUILD = live("build", build=plain("build", 40))
TAB, BTAB = "\t", "\x1b[Z"
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
    "ref": "refs/heads/gh-readonly-queue/main/pr-14-cafef00d",
    "sha": "9f8e7d6" + "0" * 33,
    "pr": 14,
}
NOT_REMOVABLE = "only hand-queued runs can be removed"


def resolve(runs, bench=IDLE, state=None, size=SIZE, **read):
    """Resolve the frame for `runs` in `state` (a fresh State by default).

    `read` is the Snapshot's `queue` and `preview`, passed only when given.
    """
    state = view.State() if state is None else state
    return view.resolve(state, view.Snapshot(runs, bench, NOW, DIRECTORY, **read), *size)


def press(state, runs, keys, bench=IDLE, size=SIZE, **read):
    """Resolve, write the scroll back and handle each key in turn, as the loop does.

    Returns the last key's quit flag.
    """
    quits = False
    for key in keys:
        frame = resolve(runs, bench, state, size, **read)
        state.scroll, state.history_scroll = frame.scroll, frame.history_scroll
        quits = view.handle(state, key, frame)
    return quits


def stage_name(frame):
    """The name of the frame's selected stage, or None."""
    return None if frame.stage is None else frame.stage.name


def head(frame, name):
    """Index of the header row of the section called `name`: the section's first row."""
    return next(i for i, row in enumerate(frame.rows) if row.section and row.section.name == name)


def texts(frame):
    """The text of every row of the frame."""
    return [row.text for row in frame.rows]


def host_rows():
    """The rows of the host suite of the long run."""
    return resolve([LONG], state=view.State(stage="host_suite")).rows


@pytest.mark.parametrize(
    ("runs", "bench", "state", "expected"),
    [
        ([live("build"), run(OLDER)], LIVE, {}, "build"),
        ([run(), failing_at(OLDER, "bundle")], IDLE, {}, None),
        ([failing_at(STAMP, "target_suite"), run(OLDER)], DISABLED, {}, "target_suite"),
        ([live("bundle")], DISABLED, {}, "bundle"),
        (
            [run(states=["passed", "error"] + ["not-started"] * 5, result="error")],
            DISABLED,
            {},
            "precondition",
        ),
        ([live("build"), failing_at(OLDER, "bundle")], LIVE, {"record": OLDER}, "bundle"),
        ([live("build"), run(OLDER)], LIVE, {"record": OLDER}, None),
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
def test_the_automatic_stage_is_derived_from_the_run_and_the_bench(runs, bench, state, expected):
    assert stage_name(resolve(runs, bench, view.State(**state))) == expected


def test_one_state_follows_the_running_stage_then_falls_back_to_the_summary_with_no_key():
    state = view.State()
    assert stage_name(resolve([live("build")], LIVE, state)) == "build"
    assert stage_name(resolve([live("bundle")], LIVE, state)) == "bundle"
    assert stage_name(resolve([run()], IDLE, state)) is None


def test_an_explicit_stage_stays_while_the_run_moves_on():
    state = view.State(stage="checkout")
    for runs in ([live("build")], [live("bundle")]):
        assert stage_name(resolve(runs, LIVE, state)) == "checkout"


def test_live_is_the_live_bench_and_the_newest_run_shown():
    runs = [live("build"), run(OLDER)]
    newest = resolve(runs, LIVE)
    past = resolve(runs, LIVE, view.State(record=OLDER))
    idle = resolve(runs, IDLE)

    assert (newest.run is runs[0], newest.newest, newest.live) == (True, True, True)
    assert (past.run is runs[1], past.newest, past.live) == (True, False, False)
    assert (idle.newest, idle.live) == (True, False)


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([running_test(3, failed=(1,))], lambda frame: head(frame, TESTS[1])),
        ([running_test(2)], lambda frame: head(frame, TESTS[2])),
        ([PLAIN_BUILD], lambda frame: len(frame.rows) - 1),
    ],
    ids=["the first failed section", "the running section", "the last row without sections"],
)
def test_the_default_cursor_is_derived_from_the_stage(runs, expected):
    frame = resolve(runs, LIVE)
    assert frame.cursor == expected(frame)
    assert frame.scroll <= frame.cursor < frame.scroll + frame.room


def test_the_default_cursor_follows_the_running_test_and_an_explicit_one_stays():
    following = view.State()
    for running in (2, 3):
        frame = resolve([running_test(running)], LIVE, following)
        assert frame.cursor == head(frame, TESTS[running])

    first = resolve([running_test(2)], LIVE)
    pinned = view.State(cursor=first.rows[head(first, TESTS[0])].anchor)
    for running in (2, 3):
        frame = resolve([running_test(running)], LIVE, pinned)
        assert frame.cursor == head(frame, TESTS[0])


def test_tail_is_the_last_row_and_carries_across_an_automatic_stage_change():
    state = view.State(cursor=view.TAIL)
    build = resolve([PLAIN_BUILD], LIVE, state)
    assert build.cursor == len(build.rows) - 1
    assert build.scroll == len(build.rows) - build.room

    host = suite("host_suite", ["passed", "open", "not-started"], {1: ("b",)}, state="open")
    suite_frame = resolve([live("host_suite", host_suite=host)], LIVE, state)
    assert stage_name(suite_frame) == "host_suite"
    assert suite_frame.cursor == len(suite_frame.rows) - 1
    assert suite_frame.cursor != head(suite_frame, "case_01")


def test_an_anchor_that_loses_its_row_falls_back_to_its_header_then_to_the_default():
    runs = [running_test(2)]
    opened = resolve(runs, LIVE, view.State(folds={sid(TESTS[1]): True}))
    body = head(opened, TESTS[1]) + 1
    assert opened.rows[body].text == "out 1"

    state = view.State(cursor=opened.rows[body].anchor, folds={sid(TESTS[1]): True})
    assert resolve(runs, LIVE, state).cursor == body
    state.folds[sid(TESTS[1])] = False
    folded = resolve(runs, LIVE, state)
    assert folded.cursor == head(folded, TESTS[1])

    elsewhere = resolve(runs, LIVE, view.State(cursor=("host_suite", None, 0)))
    assert elsewhere.cursor == head(elsewhere, TESTS[2])


def test_running_and_failed_sections_open_by_default_and_a_fold_overrides_either_way():
    runs = [running_test(3, failed=(1,))]
    default = texts(resolve(runs, LIVE))
    assert {"out 1", "out 3"} <= set(default)
    assert not {"out 0", "out 2", "booted slot B"} & set(default)

    folds = {sid(TESTS[0]): True, sid(TESTS[1]): False}
    overridden = texts(resolve(runs, LIVE, view.State(folds=folds)))
    assert "out 0" in overridden
    assert "out 1" not in overridden


@pytest.mark.parametrize(
    ("memo", "at", "expected"),
    [
        (0, 25, lambda room: 25 - room + 1),
        (20, 5, lambda room: 5),
        (5, 10, lambda room: 5),
        (99, 29, lambda room: 30 - room),
    ],
    ids=["cursor below", "cursor above", "cursor in view", "past the end"],
)
def test_scroll_moves_only_as_far_as_keeps_the_cursor_on_screen(memo, at, expected):
    anchor = host_rows()[at].anchor
    frame = resolve([LONG], state=view.State(stage="host_suite", cursor=anchor, scroll=memo))
    assert len(frame.rows) == 30
    assert 10 < frame.room < 30
    assert frame.scroll == expected(frame.room)


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


@pytest.mark.parametrize(
    "runs", [[building(2)], [running_test(2)], [PLAIN_BUILD]], ids=["build", "suite", "plain"]
)
def test_the_room_is_the_pane_less_its_head_and_an_open_prompt_takes_one_row(runs):
    closed = resolve(runs, LIVE)
    opened = resolve(runs, LIVE, view.State(prompt=":"))
    for frame in (closed, opened):
        lay = frame.layout
        assert frame.room == lay.pane_bottom - lay.pane_top - frame.head_height
    assert opened.room == closed.room - 1


def test_a_suite_or_plain_head_is_one_row_and_each_running_task_adds_one():
    assert resolve([running_test(2)], LIVE).head_height == 1
    assert resolve([PLAIN_BUILD], LIVE).head_height == 1
    three, one = resolve([building(3)], LIVE), resolve([building(1)], LIVE)
    assert three.head_height - one.head_height == 2


def test_running_tasks_are_capped_so_that_three_log_rows_remain():
    assert resolve([building(40)], LIVE).room == 3


@pytest.mark.parametrize(
    ("size", "too_small"),
    [((10, 40), True), ((23, 80), True), ((24, 79), True), ((24, 80), False)],
)
def test_a_screen_below_the_drawable_size_is_too_small(size, too_small):
    assert resolve([run()], size=size).layout.too_small is too_small


def test_history_selects_the_shown_run_until_a_record_is_chosen():
    runs = [run(), run(OLDER), run(OLDEST)]
    assert resolve(runs, state=view.State(tab="history")).history == runs
    assert resolve(runs, state=view.State(tab="history", record=OLDER)).history_cursor == 1
    chosen = view.State(tab="history", history_record=OLDEST)
    assert resolve(runs, state=chosen).history_cursor == 2


@pytest.mark.parametrize(
    ("keys", "quits"),
    [("q", True), (":q" + ENTER, True), (":x" + ENTER, False), ("j", False)],
    ids=["q", ":q Enter", ":x Enter", "j"],
)
def test_q_and_colon_q_enter_quit_and_nothing_else_does(keys, quits):
    assert press(view.State(), [run()], keys) is quits


@pytest.mark.parametrize(
    ("start", "keys", "expected"),
    [
        ({}, "l", {"tab": "run"}),
        ({"tab": "history"}, "h", {"tab": "history"}),
        ({}, "3", {"tab": "history"}),
        ({}, "2", {"tab": "queue"}),
        ({"tab": "queue"}, "1", {"tab": "run"}),
        ({"tab": "queue"}, "3", {"tab": "history"}),
        ({}, ";x", {"prompt": ":x"}),
        ({"tab": "history"}, "?", {"tab": "help", "back": "history"}),
        ({"tab": "history"}, "??", {"tab": "history"}),
        ({}, "?" + ESC, {"tab": "run"}),
        ({}, ":x", {"prompt": ":x"}),
        ({}, ":x" + ESC, {"prompt": None}),
        ({"stage": "host_suite"}, ENTER, {"focus": "log"}),
        ({"stage": "host_suite", "focus": "log"}, ESC, {"focus": "stages"}),
    ],
    ids=[
        "l: no-op",
        "h: no-op",
        "3: history",
        "2: queue",
        "1 from queue: run",
        "3 from queue: history",
        "; opens a prompt as : does",
        "?: help",
        "? again: back",
        "Esc: back",
        ": opens a prompt",
        "Esc closes it",
        "Enter: into the log",
        "Esc: back to stages",
    ],
)
def test_keys_switch_tab_help_prompt_and_focus(start, keys, expected):
    state = view.State(**start)
    press(state, [LONG, run(OLDER)], keys)
    assert {name: getattr(state, name) for name in expected} == expected


OPENED = {"tab": "run", "stage": None, "cursor": None, "focus": "stages"}
EXPLICIT = {"stage": "checkout", "focus": "log", "cursor": ("checkout", None, 0)}


@pytest.mark.parametrize(
    ("start", "keys", "record"),
    [
        ({}, "[", OLDER),
        ({"record": OLDEST}, "]", OLDER),
        ({"record": OLDER, **EXPLICIT}, "1", None),
        ({"tab": "history", "history_record": OLDER, **EXPLICIT}, ENTER, OLDER),
    ],
    ids=["[ older", "] newer", "1 the live run", "History Enter"],
)
def test_bracket_one_and_history_enter_open_a_run_in_its_automatic_state(start, keys, record):
    state = view.State(**start)
    press(state, THREE, keys, LIVE)
    assert {name: getattr(state, name) for name in (*OPENED, "record")} == {
        **OPENED,
        "record": record,
    }


def test_history_enter_on_the_newest_run_is_the_one_key():
    start = {"tab": "history", "history_record": STAMP, "record": OLDER, **EXPLICIT}
    entered, one = view.State(**start), view.State(**start)
    press(entered, THREE, ENTER, LIVE)
    press(one, THREE, "1", LIVE)
    assert (entered.tab, entered.record) == ("run", None)
    assert entered == one


@pytest.mark.parametrize(
    ("keys", "expected"),
    [("j", "bundle"), ("k", "precondition"), ("gg", "checkout"), ("G", "posted")],
)
def test_stage_list_keys_set_the_stage_from_the_one_shown(keys, expected):
    state = view.State()
    press(state, [live("build")], keys, LIVE)
    assert state.stage == expected


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ("j", lambda room: 11),
        ("k", lambda room: 9),
        ("gg", lambda room: 0),
        (HALF_DOWN, lambda room: 10 + room // 2),
        (HALF_UP, lambda room: 10 - room // 2),
    ],
    ids=["j", "k", "gg", "^d", "^u"],
)
def test_log_keys_move_the_cursor_over_the_frames_rows(keys, expected):
    state = view.State(stage="host_suite", focus="log", cursor=host_rows()[10].anchor)
    frame = resolve([LONG], state=state)
    assert frame.room // 2 < 10
    press(state, [LONG], keys)
    assert state.cursor == frame.rows[expected(frame.room)].anchor
    assert state.stage == "host_suite"


def test_capital_g_in_the_log_follows_its_tail():
    state = view.State(stage="host_suite", focus="log")
    press(state, [LONG], "G")
    assert state.cursor == view.TAIL


def test_za_toggles_the_section_under_the_cursor_and_lands_on_its_header():
    case = host_rows()[3].section
    state = view.State(stage="host_suite", focus="log", cursor=host_rows()[3].anchor)
    press(state, [LONG], "za")
    assert state.folds == {case.id: True}

    opened = resolve([LONG], state=state)
    assert opened.rows[4].text == "boot one"
    state.cursor = opened.rows[4].anchor
    press(state, [LONG], "za")
    assert state.folds == {case.id: False}
    assert resolve([LONG], state=state).cursor == 3


@pytest.mark.parametrize(("keys", "opened"), [("zR", True), ("zM", False)])
def test_zr_and_zm_open_or_fold_every_section_of_the_stage(keys, opened):
    state = view.State(stage="host_suite", focus="log")
    press(state, [LONG], keys)
    host = next(s for s in LONG.stages if s.name == "host_suite")
    assert state.folds == {section.id: opened for section in host.sections}


def test_search_unfolds_a_match_and_n_and_capital_n_step_between_matches():
    state = view.State(stage="host_suite", focus="log", cursor=host_rows()[0].anchor)

    def under_cursor():
        """The text of the row under the cursor."""
        frame = resolve([LONG], state=state)
        return frame.rows[frame.cursor].text

    press(state, [LONG], "/boot" + ENTER)
    assert (state.pattern, state.prompt, under_cursor()) == ("boot", None, "boot one")
    assert state.folds[sid("case_03", "host_suite")] is True
    press(state, [LONG], "n")
    assert under_cursor() == "boot two"
    press(state, [LONG], "N")
    assert under_cursor() == "boot one"


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ("j", lambda room: 1),
        ("G", lambda room: 39),
        ("Gk", lambda room: 38),
        ("Ggg", lambda room: 0),
        (HALF_DOWN, lambda room: room // 2),
    ],
    ids=["j", "G", "G k", "G gg", "^d"],
)
def test_history_keys_move_the_selection(keys, expected):
    state = view.State(tab="history")
    frame = resolve(MANY, state=state)
    press(state, MANY, keys)
    assert state.history_record == MANY[expected(frame.history_room)].record


def test_history_scrolls_only_as_far_as_keeps_the_selection_on_screen():
    state = view.State(tab="history")
    press(state, MANY, "G")
    bottom = resolve(MANY, state=state)
    assert (bottom.history_cursor, bottom.history_scroll) == (39, 40 - bottom.history_room)
    press(state, MANY, "gg")
    assert resolve(MANY, state=state).history_scroll == 0


EVERY_KEY = "jkGgg[]\n/za:q\x1bzRnN?"


@pytest.mark.parametrize("tab", ["run", "history"])
@pytest.mark.parametrize("size", [SIZE, (10, 40), (3, 5)])
@pytest.mark.parametrize("runs", [[], [run()]], ids=["no runs", "one run"])
def test_no_key_raises_on_no_runs_or_a_tiny_terminal(runs, size, tab):
    state = view.State(tab=tab)
    screen.draw(Grid(*size), resolve(runs, state=state, size=size))
    for key in EVERY_KEY:
        press(state, runs, key, size=size)
        screen.draw(Grid(*size), resolve(runs, state=state, size=size))


def test_no_runs_says_so_and_a_tiny_terminal_says_too_small():
    empty = Grid(*SIZE)
    screen.draw(empty, resolve([]))
    assert "0 records in" in "".join(empty.text(y) for y in range(SIZE[0]))

    tiny = Grid(10, 40)
    screen.draw(tiny, resolve([run()], size=(10, 40)))
    assert "terminal too small" in tiny.text(0)


@pytest.mark.parametrize(
    ("start", "key"), [(None, "]"), (OLDEST, "[")], ids=["newest ]", "oldest ["]
)
def test_bracket_keys_at_either_end_leave_the_state_as_it_was(start, key):
    state = view.State(record=start, stage="build", focus="log")
    press(state, THREE, key, LIVE)
    assert (state.record, state.tab, state.stage, state.focus) == (start, "run", "build", "log")


def test_a_pending_g_or_z_is_discarded_with_the_next_key():
    state = view.State(stage="host_suite", focus="log", cursor=host_rows()[10].anchor)
    press(state, [LONG], "gj")
    assert (state.cursor, state.pending) == (host_rows()[10].anchor, None)

    assert press(state, [LONG], "zq") is False
    assert (state.folds, state.pending) == ({}, None)
    press(state, [LONG], "j")
    assert state.cursor == host_rows()[11].anchor


@pytest.mark.parametrize("key", list("jk13hl/[]G") + [ENTER, HALF_DOWN])
def test_help_swallows_every_key_except_back_quit_and_colon(key):
    state = view.State(tab="help", back="history")
    assert press(state, [LONG], key) is False
    assert (state.tab, state.prompt) == ("help", None)


def test_in_help_colon_opens_the_command_line_and_q_quits():
    state = view.State(tab="help")
    press(state, [run()], ":")
    assert (state.tab, state.prompt) == ("help", ":")
    assert press(view.State(tab="help"), [run()], "q") is True


@pytest.mark.parametrize(
    ("keys", "prompt"),
    [
        (":ab\x7f", ":a"),
        (":ab\b", ":a"),
        (":\x7f", None),
        ("/\x7f", None),
        (":" + ENTER, None),
        ("/" + ENTER, None),
        (":run" + ENTER, None),
        (":q\x01", ":q"),
        (":q\x1b[A", None),
    ],
    ids=[
        "DEL",
        "BS",
        "last char closes",
        "last char closes /",
        "bare :",
        "bare /",
        ":run",
        "control",
        "Esc then keys",
    ],
)
def test_the_command_line_edits_one_character_and_runs_only_a_real_command(keys, prompt):
    state = view.State(stage="host_suite", focus="log")
    assert press(state, [LONG], keys) is False
    assert state.prompt == prompt
    assert state.pattern is None


def test_an_unmapped_key_code_leaves_the_command_line_text_alone():
    state = view.State(prompt=":q")
    assert view.handle(state, 259, resolve([run()], state=state)) is False
    assert state.prompt == ":q"


@pytest.mark.parametrize(
    ("runs", "state", "opens"),
    [
        ([LONG], {"stage": "host_suite"}, True),
        ([LONG], {"stage": "host_suite", "tab": "history"}, False),
        ([LONG], {}, False),
        ([live("build")], {"stage": "build"}, False),
    ],
    ids=["a stage with rows", "History", "the summary", "a stage with no rows"],
)
def test_slash_opens_only_on_run_with_rows(runs, state, opens):
    chosen = view.State(**state)
    press(chosen, runs, "/")
    assert chosen.prompt == ("/" if opens else None)


@pytest.mark.parametrize("search", ["/BOOT", "/nothing"], ids=["case-sensitive", "unmatched"])
def test_a_search_that_misses_moves_nothing(search):
    anchor = host_rows()[5].anchor
    state = view.State(stage="host_suite", focus="log", cursor=anchor)
    press(state, [LONG], search + ENTER)
    assert (state.cursor, state.folds, state.focus, state.prompt) == (anchor, {}, "log", None)


def test_n_and_capital_n_do_nothing_without_a_pattern_or_on_history():
    anchor = host_rows()[5].anchor
    state = view.State(stage="host_suite", focus="stages", cursor=anchor)
    press(state, [LONG], "nN")
    assert (state.cursor, state.focus) == (anchor, "stages")

    history = view.State(tab="history", pattern="boot", stage="host_suite")
    press(history, [LONG], "nN")
    assert (history.cursor, history.history_record) == (None, None)


def test_search_wraps_past_the_last_match_to_the_first_and_back():
    state = view.State(stage="host_suite", focus="log", cursor=host_rows()[0].anchor)

    def under_cursor():
        """The text of the row under the cursor."""
        frame = resolve([LONG], state=state)
        return frame.rows[frame.cursor].text

    press(state, [LONG], "/boot" + ENTER)
    seen = [under_cursor()]
    for key in "nnN":
        press(state, [LONG], key)
        seen.append(under_cursor())
    assert seen == ["boot one", "boot two", "boot one", "boot two"]


@pytest.mark.parametrize(
    ("runs", "state"),
    [([run()], {}), ([live("build")], {"stage": "build"})],
    ids=["the summary", "a stage with no rows"],
)
def test_enter_with_no_log_rows_keeps_focus_on_the_stages(runs, state):
    chosen = view.State(**state)
    press(chosen, runs, ENTER, LIVE)
    assert chosen.focus == "stages"


def test_log_focus_with_no_rows_moves_the_stage_list_instead():
    state = view.State(stage="build", focus="log")
    press(state, [live("build")], "j", LIVE)
    assert (state.stage, state.cursor) == ("bundle", None)


@pytest.mark.parametrize(
    ("stage_name", "key", "expected"),
    [("checkout", "k", "checkout"), ("posted", "j", "posted"), (None, "k", "posted")],
    ids=["clamps at the top", "clamps at the bottom", "k from the summary"],
)
def test_the_stage_list_clamps_and_k_from_the_summary_lands_on_the_last_stage(
    stage_name, key, expected
):
    state = view.State(stage=stage_name)
    press(state, [run()], key)
    assert state.stage == expected


LEAD = run(host_suite=suite("host_suite", ["passed"] * 2, {0: ("body",)}, lead=("preamble",)))


@pytest.mark.parametrize(
    ("keys", "focus"),
    [("za", "log"), ("za", "stages"), ("zR", "stages"), ("zM", "stages")],
    ids=["za on a lead line", "za", "zR", "zM"],
)
def test_fold_keys_do_nothing_on_a_lead_line_or_with_focus_on_the_stages(keys, focus):
    rows = resolve([LEAD], state=view.State(stage="host_suite")).rows
    assert rows[0].section is None
    state = view.State(stage="host_suite", focus=focus, cursor=rows[0].anchor)
    press(state, [LEAD], keys)
    assert state.folds == {}


@pytest.mark.parametrize(
    ("at", "keys", "row"),
    [(0, "k", 0), (29, "j", 29), (28, HALF_DOWN, 29), (1, HALF_UP, 0)],
    ids=["k at the top", "j at the bottom", "^d past the end", "^u past the start"],
)
def test_log_movement_clamps_at_the_first_and_last_row(at, keys, row):
    state = view.State(stage="host_suite", focus="log", cursor=host_rows()[at].anchor)
    press(state, [LONG], keys)
    assert state.cursor == host_rows()[row].anchor


@pytest.mark.parametrize("keys", [ENTER, "j", "k", "G", "gg", HALF_DOWN, HALF_UP])
def test_history_keys_on_no_records_do_nothing(keys):
    state = view.State(tab="history")
    press(state, [], keys)
    assert (state.tab, state.history_record) == ("history", None)


@pytest.mark.parametrize("key", ["h", "l"])
@pytest.mark.parametrize("tab", ["run", "history", "queue"])
def test_h_and_l_no_longer_switch_views(tab, key):
    state = view.State(tab=tab, history_record=OLDER)
    press(state, THREE, key, LIVE, queue=[HAND1])
    assert (state.tab, state.history_record) == (tab, OLDER)


def test_the_panes_start_on_row_3_and_end_two_rows_above_the_status_bar():
    lay = resolve([run()]).layout
    assert (lay.pane_top, lay.bar, lay.pane_bottom) == (3, SIZE[0] - 1, SIZE[0] - 3)
    prompted = resolve([run()], state=view.State(prompt=":")).layout
    assert (prompted.pane_top, prompted.bar, prompted.pane_bottom) == (3, SIZE[0] - 2, SIZE[0] - 4)


KAS_LINES = [
    "2026-11-10 03:10:00 - INFO     - NOTE: first",
    "2026-11-10 03:10:01 - WARNING  - NOTE: second",
    "2026-11-10 03:10:02 - ERROR    - NOTE: third",
    "NOTE: bare",
]


def test_a_plain_stages_log_rows_drop_the_kas_prefix_and_search_matches_what_is_shown():
    shown = stage("posted", "open", t(3, 10), lines=KAS_LINES)
    runs = [live("build", posted=shown)]
    state = view.State(stage="posted", focus="log", cursor=("posted", None, 2))
    frame = resolve(runs, LIVE, state)
    assert [row.text for row in frame.rows] == [
        "NOTE: first",
        "NOTE: second",
        "NOTE: third",
        "NOTE: bare",
    ]
    assert shown.lines == tuple(KAS_LINES)

    press(state, runs, "/2026" + ENTER, LIVE)
    assert state.cursor == ("posted", None, 2)
    press(state, runs, "/first" + ENTER, LIVE)
    assert state.cursor == ("posted", None, 0)


def test_the_kas_prefix_is_dropped_only_at_the_start_of_a_plain_stages_own_line():
    inner = "2026-11-10 03:10:00 - INFO     - NOTE: in a section body"
    midline = "NOTE: retry at 2026-11-10 03:10:01 - INFO     - NOTE: again"
    shown = suite("host_suite", ["failed"], bodies={0: [inner]}, lead=[midline])
    frame = resolve([live("build", host_suite=shown)], LIVE, view.State(stage="host_suite"))
    assert midline in texts(frame)
    assert inner in texts(frame)


# -- the Queue view ----------------------------------------------------------------------


def queue_state(**chosen):
    """A State on the Queue tab."""
    return view.State(tab="queue", **chosen)


@pytest.mark.parametrize(
    ("start", "key", "expected"),
    [
        ("run", TAB, "queue"),
        ("queue", TAB, "history"),
        ("history", TAB, "run"),
        ("run", BTAB, "history"),
        ("history", BTAB, "queue"),
        ("queue", BTAB, "run"),
    ],
)
def test_tab_and_shift_tab_cycle_run_queue_history_and_wrap(start, key, expected):
    state = view.State(tab=start)
    press(state, THREE, [key], LIVE, queue=[HAND1])
    assert state.tab == expected


def test_tab_and_shift_tab_are_swallowed_by_help_and_the_command_line():
    state = view.State(tab="help", back="run")
    press(state, THREE, [TAB, BTAB], LIVE)
    assert state.tab == "help"
    prompted = view.State(prompt=":ab")
    press(prompted, THREE, [TAB, BTAB], LIVE)
    assert (prompted.tab, prompted.prompt) == ("run", ":ab")


@pytest.mark.parametrize("keys", [["2"], [TAB], ["3", BTAB]], ids=["2", "Tab", "3 then Shift-Tab"])
def test_entering_the_queue_selects_its_first_row_however_it_is_entered(keys):
    state = view.State()
    press(state, THREE, keys, LIVE, queue=[HAND1, HAND2, MERGE])
    frame = resolve(THREE, LIVE, state, queue=[HAND1, HAND2, MERGE])
    assert (frame.tab, frame.queue_cursor) == ("queue", 0)


def test_leaving_and_re_entering_the_queue_selects_the_first_row_again():
    state = view.State()
    read = {"queue": [HAND1, HAND2, MERGE]}
    press(state, [run()], "2jj", **read)
    assert resolve([run()], state=state, **read).queue_cursor == 2
    press(state, [run()], "1", **read)
    press(state, [run()], "2", **read)
    assert resolve([run()], state=state, **read).queue_cursor == 0


@pytest.mark.parametrize(
    ("keys", "at"),
    [("j", 1), ("jj", 2), ("jjj", 2), ("jjk", 1), ("k", 0), ("G", 2), ("Ggg", 0), ("Gk", 1)],
)
def test_j_k_gg_and_capital_g_move_the_queue_cursor_and_clamp(keys, at):
    state = view.State()
    read = {"queue": [HAND1, HAND2, MERGE]}
    press(state, [run()], "2" + keys, **read)
    assert resolve([run()], state=state, **read).queue_cursor == at


def test_the_queue_has_a_running_row_then_hand_entries_then_the_merge_head():
    frame = resolve(THREE, LIVE, queue_state(), queue=[HAND1, HAND2, MERGE])
    assert [(r.marker, r.source, r.what) for r in frame.queue_rows] == [
        ("▶", "merge queue", "PR #31"),
        ("2", "by hand", "feature-a"),
        ("3", "by hand", "feature-b"),
        ("4", "merge queue", "PR #14"),
    ]


def test_with_no_running_run_the_markers_start_at_1():
    frame = resolve(THREE, IDLE, queue_state(), queue=[HAND1, HAND2, MERGE])
    assert [r.marker for r in frame.queue_rows] == ["1", "2", "3"]


def test_the_merge_head_is_hidden_when_its_sha_equals_the_running_sha():
    same = {**MERGE, "sha": SHA}
    frame = resolve([live("build")], LIVE, queue_state(), queue=[HAND1, same])
    assert [r.marker for r in frame.queue_rows] == ["▶", "2"]
    idle = resolve([live("build")], IDLE, queue_state(), queue=[HAND1, same])
    assert [r.marker for r in idle.queue_rows] == ["1", "2"]


def test_a_queue_not_read_yet_has_no_rows_and_no_cursor():
    frame = resolve([run()], IDLE, queue_state())
    assert (frame.queue_rows, frame.queue_cursor) == ([], None)
    empty = resolve([run()], IDLE, queue_state(), queue=[])
    assert (empty.queue_rows, empty.queue_cursor) == ([], None)


def test_a_hand_row_carries_its_ref_sha7_local_added_time_kind_and_id(local_zone):
    (row,) = resolve([run()], IDLE, queue_state(), queue=[HAND1]).queue_rows
    assert (row.source, row.what, row.sha7, row.added) == (
        "by hand",
        "feature-a",
        "1111111",
        "06:15",
    )
    assert (row.stage, row.kind, row.id) == ("", "test", 3)


def test_the_merge_head_row_has_its_pr_sha7_no_added_time_and_no_id():
    (row,) = resolve([run()], IDLE, queue_state(), queue=[MERGE]).queue_rows
    assert (row.source, row.what, row.sha7, row.added) == ("merge queue", "PR #14", "9f8e7d6", "")
    assert (row.stage, row.kind, row.id) == ("", "run", None)


def test_the_running_row_comes_from_the_newest_run_with_its_open_stage():
    (row,) = resolve([live("build")], LIVE, queue_state(), queue=[]).queue_rows
    assert (row.marker, row.source, row.what, row.sha7) == ("▶", "merge queue", "PR #31", SHA[:7])
    assert (row.added, row.stage, row.kind, row.id) == ("", "build", "run", None)


def test_a_running_hand_run_reads_by_hand_with_its_ref_and_a_blank_sha_until_known():
    hand_run = live("checkout", kind="test", pr=None, ref="feature-a", sha=None)
    (row,) = resolve([hand_run], LIVE, queue_state(), queue=[]).queue_rows
    assert (row.source, row.what, row.sha7, row.kind) == ("by hand", "feature-a", "", "test")
    assert (row.stage, row.id) == ("checkout", None)


def test_no_running_row_when_the_bench_is_not_running_the_newest_run():
    assert resolve([run()], IDLE, queue_state(), queue=[]).queue_rows == []


@pytest.mark.parametrize(
    ("keys", "removed"),
    [("2dd", 3), ("2jdd", 4)],
    ids=["first hand row", "second hand row"],
)
def test_dd_on_a_hand_row_asks_for_its_removal(keys, removed):
    state = view.State()
    press(state, [run()], keys, queue=[HAND1, HAND2, MERGE])
    assert (state.remove_id, state.message) == (removed, None)


def test_dd_with_a_live_run_removes_the_hand_row_under_the_cursor_not_the_running_one():
    state = view.State()
    press(state, THREE, "2jdd", LIVE, queue=[HAND1, HAND2, MERGE])
    assert state.remove_id == 3


@pytest.mark.parametrize(
    ("keys", "bench"),
    [("2Gdd", IDLE), ("2dd", LIVE)],
    ids=["merge-queue row", "running row"],
)
def test_dd_on_a_merge_queue_or_running_row_removes_nothing_and_says_why(keys, bench):
    state = view.State()
    press(state, THREE, keys, bench, queue=[HAND1, HAND2, MERGE])
    assert (state.remove_id, state.message) == (None, NOT_REMOVABLE)


@pytest.mark.parametrize("read", [{}, {"queue": []}], ids=["unread", "empty"])
def test_dd_on_an_empty_queue_does_nothing(read):
    state = view.State()
    press(state, [run()], "2dd", **read)
    assert (state.remove_id, state.message, state.tab) == (None, None, "queue")


@pytest.mark.parametrize("tab", ["run", "history"])
def test_dd_off_the_queue_tab_does_nothing(tab):
    state = view.State(tab=tab)
    press(state, THREE, "dd", LIVE, queue=[HAND1, HAND2])
    assert (state.remove_id, state.message, state.tab) == (None, None, tab)


def test_d_then_another_key_is_discarded_and_removes_nothing():
    state = view.State()
    press(state, [run()], "2djd", queue=[HAND1, HAND2])
    assert (state.remove_id, state.pending) == (None, "d")
    press(state, [run()], "x")
    assert (state.remove_id, state.pending) == (None, None)


def test_after_dd_the_cursor_is_the_next_row_once_the_read_lands():
    state = view.State()
    before = {"queue": [HAND1, HAND2, MERGE]}
    press(state, [run()], "2dd", **before)
    after = resolve([run()], state=state, queue=[HAND2, MERGE])
    assert after.queue_cursor == 0 and after.queue_rows[0].id == 4


def test_after_dd_of_the_last_hand_row_the_cursor_is_the_merge_head():
    state = view.State()
    press(state, [run()], "2jdd", queue=[HAND1, HAND2, MERGE])
    after = resolve([run()], state=state, queue=[HAND1, MERGE])
    assert (after.queue_cursor, after.queue_rows[1].source) == (1, "merge queue")


def test_after_dd_of_the_last_row_the_cursor_clamps_to_the_new_last_row():
    state = view.State()
    press(state, [run()], "2jdd", queue=[HAND1, HAND2])
    assert resolve([run()], state=state, queue=[HAND1]).queue_cursor == 0


def test_the_cursor_keeps_its_hand_row_when_rows_before_it_go_away():
    state = view.State()
    press(state, [run()], "2j", queue=[HAND1, HAND2, MERGE])
    after = resolve([run()], state=state, queue=[HAND2, MERGE])
    assert (after.queue_cursor, after.queue_rows[after.queue_cursor].id) == (0, 4)


def test_the_cursor_keeps_the_merge_head_when_a_hand_row_is_added_before_it():
    state = view.State()
    third = {**HAND1, "id": 5, "ref": "feature-c"}
    press(state, [run()], "2G", queue=[HAND1, HAND2, MERGE])
    after = resolve([run()], state=state, queue=[HAND1, HAND2, third, MERGE])
    assert (after.queue_cursor, after.queue_rows[after.queue_cursor].source) == (3, "merge queue")


def test_the_cursor_keeps_the_running_row_when_hand_rows_arrive():
    state = view.State()
    press(state, THREE, "2", LIVE, queue=[])
    after = resolve(THREE, LIVE, state, queue=[HAND1])
    assert (after.queue_cursor, after.queue_rows[0].marker) == (0, "▶")


@pytest.mark.parametrize("keys", [":run feature-c", ";run feature-c"])
def test_run_with_a_ref_and_enter_sets_run_ref_and_closes_the_prompt(keys):
    state = view.State()
    press(state, [run()], keys + ENTER)
    assert (state.run_ref, state.prompt) == ("feature-c", None)


def test_run_strips_whitespace_around_the_ref():
    state = view.State()
    press(state, [run()], ":run \t  feature-c  " + ENTER)
    assert state.run_ref == "feature-c"


@pytest.mark.parametrize("typed", [":run", ":run ", ":run    ", ":runfeature-c", ":x", ":rn a"])
def test_run_without_a_ref_or_another_word_queues_nothing_and_stays_silent(typed):
    state = view.State()
    press(state, [run()], typed + ENTER)
    assert (state.run_ref, state.prompt, state.message, state.remove_id) == (None, None, None, None)


def test_the_tab_switches_to_the_queue_when_the_prompt_becomes_run_space():
    state = view.State()
    press(state, [run()], ":run")
    assert state.tab == "run"
    press(state, [run()], " ")
    assert (state.tab, state.prompt) == ("queue", ":run ")


def test_run_space_from_history_switches_to_the_queue_too():
    state = view.State(tab="history")
    press(state, [run()], ":run ")
    assert state.tab == "queue"


def test_esc_leaves_the_queue_showing_with_the_prompt_closed_and_nothing_queued():
    state = view.State()
    press(state, [run()], ":run feature" + ESC)
    assert (state.tab, state.prompt, state.run_ref) == ("queue", None, None)


def test_a_message_survives_movement_and_is_cleared_by_opening_the_prompt():
    state = view.State(message="queued 5 abc1234")
    press(state, [run()], "2j", queue=[HAND1, HAND2])
    assert state.message == "queued 5 abc1234"
    press(state, [run()], ":")
    assert (state.message, state.prompt) == (None, ":")


def test_a_message_is_cleared_by_esc():
    state = view.State(message="queued 5 abc1234")
    press(state, [run()], ESC)
    assert state.message is None


def test_the_frame_shows_the_message_and_it_takes_the_prompt_row_so_the_bar_shifts_up():
    plain_frame = resolve([run()])
    shown = resolve([run()], state=view.State(message="no queued run 3"))
    assert shown.message == "no queued run 3"
    assert plain_frame.message is None
    assert (shown.layout.bar, shown.layout.pane_bottom) == (
        plain_frame.layout.bar - 1,
        plain_frame.layout.pane_bottom - 1,
    )


def _previewed(typed, result, ref=None, queue=(), bench=IDLE):
    state = view.State(tab="queue", prompt=typed)
    snap = {"queue": list(queue), "preview": (ref if ref is not None else "feature-c", result)}
    return resolve(THREE, bench, state, **snap).preview_lines


CLEAN = {"sha": "1234567" + "0" * 33, "subject": "Fix the thing", "clean": True}


def test_a_resolved_ref_previews_what_will_queue_where_it_joins_and_after_what():
    lines = _previewed(":run feature-c", CLEAN, queue=[HAND1, HAND2, MERGE])
    assert lines == [
        "will queue",
        'resolves to 1234567 "Fix the thing", clean tree',
        "joins as 3, after feature-b",
    ]


def test_a_dirty_checkout_says_so_in_place_of_clean_tree():
    lines = _previewed(":run feature-c", {**CLEAN, "clean": False}, queue=[HAND1])
    assert lines[1] == 'resolves to 1234567 "Fix the thing", uncommitted changes in the checkout'


def test_the_first_place_has_no_after():
    assert _previewed(":run feature-c", CLEAN, queue=[MERGE])[2] == "joins as 1"


def test_a_running_run_counts_as_place_1_and_is_what_the_first_hand_entry_joins_after():
    assert _previewed(":run feature-c", CLEAN, bench=LIVE)[2] == "joins as 2, after PR #31"
    both = _previewed(":run feature-c", CLEAN, queue=[HAND1], bench=LIVE)
    assert both[2] == "joins as 3, after feature-a"


def test_an_unresolvable_ref_previews_only_that_it_does_not_resolve():
    lines = _previewed(":run nope", "fatal: bad revision", ref="nope")
    assert lines == ["nope does not resolve"]


def test_a_preview_of_another_ref_than_the_one_typed_is_not_shown():
    assert _previewed(":run feature-d", CLEAN, ref="feature-c") is None


def test_the_ref_typed_is_stripped_before_it_is_compared_with_the_preview():
    assert _previewed(":run   feature-c  ", CLEAN) is not None


def test_no_preview_without_the_run_prompt_or_a_result():
    assert _previewed(":ru", CLEAN) is None
    assert _previewed(":run ", CLEAN, ref="feature-c") is None
    closed = resolve([run()], state=queue_state(), queue=[], preview=("feature-c", CLEAN))
    assert closed.preview_lines is None
    typed = view.State(tab="queue", prompt=":run feature-c")
    assert resolve([run()], state=typed, queue=[]).preview_lines is None


def test_the_running_row_shows_before_the_queue_has_been_read():
    frame = resolve([live("build")], LIVE, queue_state())
    assert [(r.marker, r.stage) for r in frame.queue_rows] == [("▶", "build")]
