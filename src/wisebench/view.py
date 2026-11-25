"""The watch view's rules: what the operator sees of the runs, the bench and the queue.

Nothing here draws; `status` puts these words in Textual's widgets.
"""

import re
from datetime import datetime

from rich.style import Style
from rich.text import Text

from wisebench.bench_state import DASH
from wisebench.runlog import KAS_STAMP, Section

COLOURS = {
    "passed": "#98c379",
    "failed": "#e06c75",
    "running": "#e5c07b",
    "hand": "#61afef",
    "dim": "#8b8b8b",
    "log": "#a8a8a8",
    "bright": "#ffffff",
    "rule": "#3a3a3a",
    "ground": "#0c0c0c",
    "ink": "#cccccc",
    "selection": "#1f2a36",
    "running-ground": "#2a2614",
    "failed-ground": "#3a1d20",
}
_LOG_PREFIX = re.compile(KAS_STAMP + r"[A-Z]+\s+- ")
_RUN_PROMPT = re.compile(r":run\s+(.*)")
_CONTROLS = {
    **{code: "^" + chr(code ^ 64) for code in (*range(32), 127)},
    **{code: f"<{code:x}>" for code in (*range(0x80, 0xA0), 0x2028, 0x2029)},
}
_RESULTS = {
    "passed": ("passed", "passed"),
    "failed": ("failed", "failed"),
    "error": ("bench error", "failed"),
    None: ("no result", "dim"),
}


def clean(text):
    """`text` safe to put on a terminal: tabs expanded, controls and line separators shown as text.

    See ADR 0006 rev 1.

    Args:
        text (str): Text read from a log, a record name, git or GitHub.

    Returns:
        str: The text with no character a terminal acts on.
    """
    return text.expandtabs(8).translate(_CONTROLS)


def shown_line(line):
    """A stage's own output line as shown: kas's `<stamp> - LEVEL - ` prefix dropped.

    Args:
        line (str): The line as the log holds it.

    Returns:
        str: The line without the prefix.
    """
    return _LOG_PREFIX.sub("", line, count=1)


def display_name(name):
    """A stage name as shown: `host_suite` reads `host suite`.

    Args:
        name (str): The stage name.

    Returns:
        str: The name with spaces for underscores.
    """
    return name.replace("_", " ")


def clock(moment):
    """`HH:MM:SS` in local time.

    Args:
        moment (datetime): An aware time.

    Returns:
        str: The local wall-clock time.
    """
    return moment.astimezone().strftime("%H:%M:%S")


def took(started, ended, running, now):
    """How long something took as `MmSSs`: start to end, start to now while running, else a dash.

    Args:
        started (datetime): The start.
        ended (datetime | None): The end, or None.
        running (bool): It is still running.
        now (datetime): The current time.

    Returns:
        str: The duration, minutes unbounded, or a dash.
    """
    if ended is None and not running:
        return DASH
    seconds = int(((ended or now) - started).total_seconds())
    return f"{seconds // 60}m{seconds % 60:02d}s"


def run_took(run, running, now):
    """A run's length in whole minutes: since its start while running, else start to end.

    Args:
        run (Run): The run.
        running (bool): The bench is running it.
        now (datetime): The current time.

    Returns:
        str: `<n>m`, or a dash without a start, or without an end when not running.
    """
    end = now if running else run.ended
    if run.started is None or end is None:
        return DASH
    return f"{int((end - run.started).total_seconds()) // 60}m"


def what(run):
    """What a run tested: `PR #n`, else its ref.

    Args:
        run (Run): The run.

    Returns:
        str: The label.
    """
    return f"PR #{run.pr}" if run.pr is not None else run.ref or ""


def sha7(run):
    """The first seven characters of the commit the run checked out, or a dash.

    Args:
        run (Run): The run.

    Returns:
        str: The short sha.
    """
    checkout = next((s for s in run.stages if s.name == "checkout"), None)
    return checkout.sha[:7] if checkout and checkout.sha else DASH


def result(run, running):
    """A run's result as a word and its meaning.

    Args:
        run (Run): The run.
        running (bool): The bench is running it.

    Returns:
        tuple[str, str]: The word and its meaning: "running", "passed", "failed" or "dim".
    """
    return ("running", "running") if running else _RESULTS[run.result]


def tone(state, running):
    """The glyph and meaning of a stage or section state.

    Args:
        state (str): A `Stage.state` or `Section.outcome`.
        running (bool): The run is live, so an open stage or section is running.

    Returns:
        tuple[str, str]: The glyph and the meaning: "passed", "failed", "running" or "dim".
    """
    if state == "passed":
        return "✓", "passed"
    if state in ("failed", "error"):
        return "✗", "failed"
    if state == "open":
        return ("●", "running") if running else ("?", "dim")
    return ("–" if state == "skipped" else "·"), "dim"


def auto_stage(run, newest, bench):
    """The stage shown when the operator has not chosen one; None is the run's summary.

    The open stage of the newest run while the bench runs it; the summary of the newest run while
    the bench is idle; else the first stage that failed, errored or was left open.

    Args:
        run (Run): The run shown.
        newest (bool): It is the newest run.
        bench (Bench): The bench's state.

    Returns:
        Stage | None: The stage, or None for the summary.
    """
    if newest and bench.live:
        return next((s for s in run.stages if s.state == "open"), None)
    if newest and not bench.disabled:
        return None
    return next((s for s in run.stages if s.state in ("failed", "error", "open")), None)


def counts(stage):
    """A suite's test counts.

    Args:
        stage (Stage): The suite stage.

    Returns:
        tuple[int, int, int, int]: Started or reported, queued, passed, failed.
    """
    tests = [s for s in stage.sections if s.kind == "test"]
    return (
        sum(s.outcome != "not-started" for s in tests),
        len(tests),
        sum(s.outcome == "passed" for s in tests),
        sum(s.outcome == "failed" for s in tests),
    )


def typed_run(text):
    """The directory and the ref typed after `:run `.

    Args:
        text (str): The command line's text.

    Returns:
        tuple[str | None, str] | None: The directory (no spaces in it) after `-C` and the ref, with
        None as the directory when the ref is not `-C <dir> <ref>`; None when the text is not
        `:run <ref>`.
    """
    found = _RUN_PROMPT.fullmatch(text)
    ref = found.group(1).strip() if found else ""
    if not ref:
        return None
    with_directory = re.fullmatch(r"-C\s+(\S+)\s+(\S.*)", ref)
    return with_directory.groups() if with_directory else (None, ref)


def styled(text, meaning=None, bold=False):
    """Text safe to draw, in the colour of its meaning.

    Args:
        text (str): The text, from anywhere.
        meaning (str | None): A key of `COLOURS`, or None for the text colour.
        bold (bool): Draw it bold.

    Returns:
        Text: The cleaned text.
    """
    return Text(clean(text), Style(color=COLOURS.get(meaning), bold=bold))


def line(*parts):
    """One line of several colours.

    Args:
        *parts (tuple): Arguments of `styled`.

    Returns:
        Text: The parts joined.
    """
    return Text.assemble(*(styled(*part) for part in parts), no_wrap=True, overflow="ellipsis")


def section_ends(stage):
    """When each section ended: the next started section's start, else the stage's end.

    Args:
        stage (Stage): The stage.

    Returns:
        list[datetime | None]: One end per section, in section order.
    """
    ends, following = [], stage.ended
    for section in reversed(stage.sections):
        ends.append(following)
        following = section.started or following
    return ends[::-1]


def outline(stage):
    """A suite's sections as its tree shows them, with when each ended.

    A suite's own output, when it has any, comes first, as a section that failed when the stage
    errored.

    Args:
        stage (Stage): The suite.

    Returns:
        list[tuple[Section, datetime | None]]: Each section and its end.
    """
    rows = list(zip(stage.sections, section_ends(stage), strict=True))
    if stage.lines:
        outcome = "failed" if stage.state == "error" else "passed"
        output = Section(
            (stage.name, "output", "", 0), "output", "output", outcome, None, stage.lines, ()
        )
        rows.insert(0, (output, None))
    return rows


def landing(sections):
    """Where a suite's cursor lands: its first failed section, else its open one, else its first.

    Args:
        sections (list[Section]): The suite's sections as `outline` gives them.

    Returns:
        int: The place.
    """
    rank = {"failed": 0, "open": 1}
    return min(range(len(sections)), key=lambda i: rank.get(sections[i].outcome, 2))


def run_title(run, newest, bench):
    """The line over the Run tab.

    Args:
        run (Run): The run shown.
        newest (bool): It is the newest run.
        bench (Bench): The bench's state.

    Returns:
        Text: What it tested, its sha and record; `last run` first while the bench is idle, and
        its result and end when it is a past run.
    """
    parts = [(what(run), "bright", True), (f"  {sha7(run)}  record {run.record}", "dim")]
    if not newest:
        ended = f"  ended {clock(run.ended)}" if run.ended else ""
        parts += [("  ",), (*result(run, False), True), (ended, "dim")]
    elif not bench.live and not bench.disabled:
        parts.insert(0, ("last run  ", "dim"))
    return line(*parts)


def stage_cells(stage, running, now):
    """A stage's cells in the stage list.

    Args:
        stage (Stage): The stage.
        running (bool): The bench is running its run.
        now (datetime): The current time.

    Returns:
        tuple: Glyph and name, start and took, in the stage's colour.
    """
    glyph, meaning = tone(stage.state, running)
    times = ("", "")
    if stage.started and stage.state != "skipped":
        open_ = running and stage.state == "open"
        times = clock(stage.started), took(stage.started, stage.ended, open_, now)
    cells = (f"{glyph} {display_name(stage.name)}", *times)
    return tuple(styled(cell, meaning, meaning in ("running", "failed")) for cell in cells)


def section_label(section, end, running, now):
    """A test's or setup's line in the suite's log.

    Args:
        section (Section): The section.
        end (datetime | None): When it ended.
        running (bool): The bench is running its run.
        now (datetime): The current time.

    Returns:
        Text: Glyph, `setup` for a setup, its name, and how long it took.
    """
    glyph, meaning = tone(section.outcome, running)
    parts = [(f"{glyph} ", meaning, True)]
    if section.kind == "setup":
        parts.append(("setup ", "hand", True))
    parts.append((section.name, meaning, meaning in ("running", "failed")))
    if section.started:
        length = took(section.started, end, running and section.outcome == "open", now)
        parts.append((f"  {length}", "dim"))
    return line(*parts)


def head(run, stage, live, now):
    """The lines over a stage's log.

    Args:
        run (Run): The run shown.
        stage (Stage | None): The stage shown, or None for the run's summary.
        live (bool): The bench is running this run.
        now (datetime): The current time.

    Returns:
        Text: The summary, or a suite's tally, or a build's progress, bar and running tasks.
    """
    if stage is None:
        return summary(run, live, now)
    _, meaning = tone(stage.state, live)
    name = (display_name(stage.name), meaning, True)
    if stage.sections:
        return suite_tally(stage, name)
    rows = []
    if stage.progress:
        rows = build_progress(stage, name, meaning, live, now)
    return Text("\n").join([*rows, line(("this stage's output", "dim"))])


def summary(run, live, now):
    """A run's result and what each suite passed.

    Args:
        run (Run): The run.
        live (bool): The bench is running it.
        now (datetime): The current time.

    Returns:
        Text: The result line, then a line per suite that has tests.
    """
    word, meaning = result(run, live)
    ended = f"  ended {clock(run.ended)}, took {run_took(run, live, now)}" if run.ended else ""
    rows = [line((word, meaning, True), (ended, "dim"))]
    for suite in run.stages:
        _, queued, passed, failed = counts(suite)
        if queued:
            tally = f"{passed} passed"
            if failed:
                tally += f"  {failed} failed"
            rows.append(line((f"{display_name(suite.name):<14}", "dim"), (tally,)))
    return Text("\n").join(rows)


def suite_tally(stage, name):
    """A suite's name and how many of its tests started, passed and failed.

    Args:
        stage (Stage): The suite.
        name (tuple): Its name, meaning and bold, for `line`.

    Returns:
        Text: The tally.
    """
    started, queued, passed, failed = counts(stage)
    tally = [(f"  {started} of {queued}  ",), (f"{passed} passed", "passed")]
    tally.append((f"  {failed} failed", "failed" if failed else "dim"))
    return line(name, *tally)


def build_progress(stage, name, meaning, live, now):
    """A build's progress line and bar, and the tasks running while it is live.

    Args:
        stage (Stage): The build, with `progress`.
        name (tuple): Its name, meaning and bold, for `line`.
        meaning (str): The stage's colour.
        live (bool): The bench is running it.
        now (datetime): The current time.

    Returns:
        list[Text]: The lines.
    """
    progress = stage.progress
    share = progress.done / progress.total if progress.total else 0
    numbers = f"  {progress.phase} {progress.done} of {progress.total}  {share:.0%}"
    filled = round(33 * share)
    rows = [
        line(name, (numbers,)),
        line(("█" * filled, meaning), ("█" * (33 - filled), "rule")),
        Text(),
    ]
    if live and stage.state == "open" and progress.tasks:
        first_eight = sorted(progress.tasks, key=lambda t: (not t.started, t.started or now))[:8]
        recipe = max(len(t.recipe) for t in first_eight) + 2
        task = max(len(t.task) for t in first_eight) + 2
        rows.append(line(("running now", "dim")))
        for each in first_eight:
            elapsed = took(each.started, None, True, now) if each.started else DASH
            rows.append(line((f"{each.recipe:<{recipe}}{each.task:<{task}}{elapsed}",)))
        rows.append(Text())
    return rows


def failure(stopped, run):
    """The box a disabled bench shows.

    Args:
        stopped (str): The `DISABLED` file's text: its UTC time, then its reason.
        run (Run): The newest run.

    Returns:
        Text: Whose failure it is, the reason, and the setup it stopped in and when.
    """
    stamp, _, reason = stopped.partition("\n")
    setups = [
        each.name
        for stage in run.stages
        for each in stage.sections
        if each.kind == "setup" and each.outcome in ("failed", "open")
    ]
    where = f"in setup {setups[-1]}; " if setups else ""
    return Text("\n").join(
        [
            line(("bench failure, not the candidate's", "failed", True)),
            line((reason.strip(),)),
            line((f"{where}from the DISABLED file, {since(stamp)}", "dim")),
        ]
    )


def since(stamp):
    """The `DISABLED` file's time as shown.

    Args:
        stamp (str): Its first line.

    Returns:
        str: The local time, or a dash when it does not parse.
    """
    try:
        return clock(datetime.fromisoformat(stamp.strip()))
    except ValueError:
        return DASH


def state_line(bench, stopped, run):
    """The status bar.

    Args:
        bench (Bench): The bench's state.
        stopped (str): The `DISABLED` file's text.
        run (Run | None): The newest run.

    Returns:
        Text: The state's badge, the timer, and since when the bench is disabled.
    """
    word, meaning = ("RUNNING", "running") if bench.live else ("IDLE", "rule")
    if bench.disabled and not bench.live:
        word, meaning = "DISABLED", "failed"
    badge = Text(f" {word} ", Style(color=COLOURS["ground"], bgcolor=COLOURS[meaning], bold=True))
    timer = {"on": "passed", "off": "failed"}.get(bench.timer, "dim")
    text = Text.assemble(badge, line(("  timer ", "dim"), (bench.timer, timer)))
    if bench.disabled and run:
        unposted = any(s.name == "posted" and s.state != "passed" for s in run.stages)
        since_when = since(stopped.partition("\n")[0])
        text += line(("  since ", "dim"), (since_when + (", nothing posted" if unposted else ""),))
    return text


def table(rows):
    """Table rows with every cell padded to its column, so a row's ground has no gaps.

    Args:
        rows (list[tuple]): Per row, its key and its cells.

    Returns:
        list[tuple]: The rows.
    """
    for column in zip(*(cells for _, cells in rows), strict=True):
        width = max(cell.cell_len for cell in column) + 2
        for cell in column:
            cell.pad_right(width - cell.cell_len)
    return rows


def history_rows(runs, bench, now):
    """The History table's rows.

    Args:
        runs (list[Run]): Every run, newest first.
        bench (Bench): The bench's state.
        now (datetime | None): The current time while a run is live.

    Returns:
        list[tuple]: Per run, its record and its cells: record, what, sha, result, took, and the
        stage that failed or `by hand`.
    """
    rows = []
    for i, run in enumerate(runs):
        live = bench.live and i == 0
        failing = next((s.name for s in run.stages if s.state in ("failed", "error")), None)
        hand = ("by hand", "hand") if run.kind == "test" else ("",)
        note = (display_name(failing), "dim") if failing else hand
        cells = (
            styled(run.record),
            styled(what(run)[:40]),
            styled(sha7(run)),
            styled(*result(run, live)),
            styled(f"{run_took(run, live, now):>4}"),
            styled(*note),
        )
        rows.append((run.record, cells))
    return table(rows)


def queue_rows(runs, bench, entries):
    """The Queue table's rows: the running run, the hand entries, then the merge-queue entries.

    The running run takes its title from the merge entry with its PR, and the merge entry for the
    commit it checked out is left out.

    Args:
        runs (list[Run]): Every run, newest first.
        bench (Bench): The bench's state.
        entries (list[dict]): `Queue.entries()`.

    Returns:
        list[tuple]: Per row, its key (`running`, `hand <id>` or `merge <pr>`) and its cells:
        place, source, what, sha, added, and the running run's stage.
    """
    rows, running_sha = [], None
    if bench.live and runs:
        run = runs[0]
        opened = next((display_name(s.name) for s in run.stages if s.state == "open"), "")
        running_sha = next((s.sha for s in run.stages if s.name == "checkout"), None)
        merge = next((e for e in entries if e["kind"] == "run" and e["pr"] == run.pr), None)
        label = what(run) + (f" {merge['title']}" if run.pr and merge else "")
        source = "by hand" if run.kind == "test" else "merge queue"
        cells = [("▶", "running"), (source,), (label,), ((running_sha or "")[:7],), ("",)]
        cells.append((f"running, {opened}", "running"))
        rows.append(("running", tuple(styled(*cell) for cell in cells)))
        for cell in rows[0][1]:
            cell.style += Style(bgcolor=COLOURS["running-ground"])
    for entry in entries:
        place, sha = (str(len(rows) + 1),), ((entry["sha"] or "")[:7],)
        if entry["kind"] == "test":
            added = datetime.fromisoformat(entry["added"]).astimezone().strftime("%H:%M")
            key, cells = (
                f"hand {entry['id']}",
                (("by hand", "hand"), (entry["ref"],), sha, (added,)),
            )
        elif not entry["sha"] or entry["sha"] != running_sha:
            title = f"PR #{entry['pr']} {entry['title']}"
            key, cells = f"merge {entry['pr']}", (("merge queue",), (title,), sha, ("",))
        else:
            continue
        rows.append((key, tuple(styled(*cell) for cell in (place, *cells, ("",)))))
    return table(rows)


def preview(ref, outcome, rows):
    """The box `:run <ref>` shows while it is typed.

    Args:
        ref (str): The ref typed.
        outcome (dict | Exception): `Queue.preview`'s result, or what it raised.
        rows (list[tuple]): The Queue table's rows.

    Returns:
        Text: The commit and its subject, whether the checkout is clean, and its place: after the
        running run and the hand-queued runs; or that the ref does not resolve.
    """
    if isinstance(outcome, Exception):
        return line((f"{ref} does not resolve", "failed"))
    state = "clean tree" if outcome["clean"] else "uncommitted changes in the checkout"
    ahead = [cells for key, cells in rows if key == "running" or key.startswith("hand")]
    place = f"joins as {len(ahead) + 1}"
    place += f", after {ahead[-1][2].plain.strip()}" if ahead else ""
    resolved = f'resolves to {outcome["sha"][:7]} "{outcome["subject"]}", {state}'
    return Text("\n").join(
        [line(("will queue", "hand", True)), line((resolved, "dim")), line((place, "dim"))]
    )
