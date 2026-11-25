"""The watch view's rules: what the operator sees of the runs, the bench and the queue.

Nothing here draws; `status` puts these words in Textual's widgets.
"""

import re
from datetime import datetime

from rich.style import Style
from rich.text import Text

from wisebench import palette, rules
from wisebench.bench_state import DASH
from wisebench.runlog import KAS_STAMP

_LOG_PREFIX = re.compile(KAS_STAMP + r"[A-Z]+\s+- ")
_RESULTS = {
    "passed": ("passed", "passed"),
    "failed": ("failed", "failed"),
    "error": ("bench error", "failed"),
    None: ("no result", "dim"),
}


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


def sha7(run):
    """The first seven characters of the commit the run checked out, or a dash.

    Args:
        run (Run): The run.

    Returns:
        str: The short sha.
    """
    return rules.sha(run)[:7] or DASH


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
    parts = [(rules.what(run), "bright", True), (f"  {sha7(run)}  record {run.record}", "dim")]
    if not newest:
        ended = f"  ended {clock(run.ended)}" if run.ended else ""
        parts += [("  ",), (*result(run, False), True), (ended, "dim")]
    elif not bench.live and not bench.disabled:
        parts.insert(0, ("last run  ", "dim"))
    return palette.line(*parts)


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
    return tuple(palette.styled(cell, meaning, meaning in ("running", "failed")) for cell in cells)


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
    return palette.line(*parts)


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
    return Text("\n").join([*rows, palette.line(("this stage's output", "dim"))])


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
    rows = [palette.line((word, meaning, True), (ended, "dim"))]
    for suite in run.stages:
        tests = rules.counts(suite)
        if tests:
            tally = f"{tests['passed']} passed"
            if tests["failed"]:
                tally += f"  {tests['failed']} failed"
            rows.append(palette.line((f"{display_name(suite.name):<14}", "dim"), (tally,)))
    return Text("\n").join(rows)


def suite_tally(stage, name):
    """A suite's name and how many of its tests started, passed and failed.

    Args:
        stage (Stage): The suite.
        name (tuple): Its name, meaning and bold, for `line`.

    Returns:
        Text: The tally.
    """
    tests, failed = rules.counts(stage), rules.counts(stage)["failed"]
    started = tests.total() - tests["not-started"]
    tally = [(f"  {started} of {tests.total()}  ",), (f"{tests['passed']} passed", "passed")]
    tally.append((f"  {failed} failed", "failed" if failed else "dim"))
    return palette.line(name, *tally)


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
        palette.line(name, (numbers,)),
        palette.line(("█" * filled, meaning), ("█" * (33 - filled), "rule")),
        Text(),
    ]
    if live and stage.state == "open" and progress.tasks:
        first_eight = sorted(progress.tasks, key=lambda t: (not t.started, t.started or now))[:8]
        recipe = max(len(t.recipe) for t in first_eight) + 2
        task = max(len(t.task) for t in first_eight) + 2
        rows.append(palette.line(("running now", "dim")))
        for each in first_eight:
            elapsed = took(each.started, None, True, now) if each.started else DASH
            rows.append(palette.line((f"{each.recipe:<{recipe}}{each.task:<{task}}{elapsed}",)))
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
            palette.line(("bench failure, not the candidate's", "failed", True)),
            palette.line((reason.strip(),)),
            palette.line((f"{where}from the DISABLED file, {since(stamp)}", "dim")),
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


def state_line(seen):
    """The status bar.

    Args:
        seen (Seen): The read: the bench's state, the `DISABLED` text and the newest run.

    Returns:
        Text: The state's badge, the timer, and since when the bench is disabled.
    """
    bench, run = seen.bench, seen.newest
    word, meaning = ("RUNNING", "running") if bench.live else ("IDLE", "rule")
    if bench.disabled and not bench.live:
        word, meaning = "DISABLED", "failed"
    badge = Text(
        f" {word} ",
        Style(color=palette.COLOURS["ground"], bgcolor=palette.COLOURS[meaning], bold=True),
    )
    timer = {"on": "passed", "off": "failed"}.get(bench.timer, "dim")
    text = Text.assemble(badge, palette.line(("  timer ", "dim"), (bench.timer, timer)))
    if bench.disabled and run:
        unposted = any(s.name == "posted" and s.state != "passed" for s in run.stages)
        since_when = since(seen.stopped.partition("\n")[0])
        text += palette.line(
            ("  since ", "dim"), (since_when + (", nothing posted" if unposted else ""),)
        )
    return text


def history_rows(seen):
    """The History table's rows.

    Args:
        seen (Seen): The read: every run, newest first, the bench's state and the time.

    Returns:
        list[tuple]: Per run, its record and its cells: record, what, sha, result, took, and the
        stage that failed or `by hand`.
    """
    rows = []
    for run in seen.runs:
        live = run is seen.running
        failing = next((s.name for s in run.stages if s.state in ("failed", "error")), None)
        hand = ("by hand", "hand") if run.kind == "test" else ("",)
        note = (display_name(failing), "dim") if failing else hand
        what_, length = rules.what(run)[:40], run_took(run, live, seen.now)
        cells = ((run.record,), (what_,), (sha7(run),), result(run, live), (f"{length:>4}",), note)
        rows.append((run.record, tuple(palette.styled(*cell) for cell in cells)))
    return palette.table(rows)


def count(seen):
    """How many records the directory holds.

    Args:
        seen (Seen): The read.

    Returns:
        str: The count and the directory's name.
    """
    return f"{len(seen.runs)} records in {seen.directory}/"


def queue_rows(waiting):
    """The Queue table's rows.

    Args:
        waiting (list[Waiting]): The queue.

    Returns:
        list[tuple]: Per row, its key and its cells: place, source, what, sha, added, and the
        running run's stage.
    """
    rows = []
    for place, each in enumerate(waiting, start=1):
        running = each.key == "running"
        source = "by hand" if each.by_hand else "merge queue"
        cells = (
            palette.styled("▶", "running") if running else palette.styled(str(place)),
            palette.styled(source, "hand" if each.by_hand and not running else None),
            palette.styled(each.label),
            palette.styled(each.sha[:7]),
            palette.styled(each.added.astimezone().strftime("%H:%M") if each.added else ""),
            palette.styled(f"running, {display_name(each.stage)}" if running else "", "running"),
        )
        if running:
            for cell in cells:
                cell.style += Style(bgcolor=palette.COLOURS["running-ground"])
        rows.append((each.key, cells))
    return palette.table(rows)


def preview(ref, outcome, waiting):
    """The box `:run <ref>` shows while it is typed.

    Args:
        ref (str): The ref typed.
        outcome (dict | Exception): `Queue.preview`'s result, or what it raised.
        waiting (list[Waiting]): The queue.

    Returns:
        Text: The commit and its subject, whether the checkout is clean, and its place: after the
        running run and the hand-queued runs; or that the ref does not resolve.
    """
    if isinstance(outcome, Exception):
        return palette.line((f"{ref} does not resolve", "failed"))
    state = "clean tree" if outcome["clean"] else "uncommitted changes in the checkout"
    ahead = [each.label for each in waiting if not each.key.startswith("merge")]
    place = f"joins as {len(ahead) + 1}" + (f", after {ahead[-1]}" if ahead else "")
    resolved = f'resolves to {outcome["sha"][:7]} "{outcome["subject"]}", {state}'
    return Text("\n").join(
        [
            palette.line(("will queue", "hand", True)),
            palette.line((resolved, "dim")),
            palette.line((place, "dim")),
        ]
    )
