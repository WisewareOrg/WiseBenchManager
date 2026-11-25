"""The watch view's rules: what the operator sees of the runs, the bench and the queue.

Nothing here draws; `status` puts these words in Textual's widgets.
"""

import re
from datetime import datetime

from rich.text import Text

from wisebench.bench_state import DASH
from wisebench.runlog import KAS_STAMP

COLOURS = {
    "passed": "#98c379",
    "failed": "#e06c75",
    "running": "#e5c07b",
    "hand": "#61afef",
    "dim": "#8b8b8b",
}
_LOG_PREFIX = re.compile(KAS_STAMP + r"[A-Z]+\s+- ")
_RUN_PROMPT = re.compile(r":run\s+(.*)")
_CONTROLS = {
    **{code: "^" + chr(code ^ 64) for code in (*range(32), 127)},
    **{code: f"<{code:x}>" for code in range(0x80, 0xA0)},
}
_RESULTS = {
    "passed": ("passed", "passed"),
    "failed": ("failed", "failed"),
    "error": ("bench error", "failed"),
    None: ("no result", "dim"),
}


def clean(text):
    """`text` safe to put on a terminal: tabs expanded, C0 and C1 controls shown as text.

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


def typed_ref(text):
    """The ref typed after `:run `, stripped.

    Args:
        text (str): The command line's text.

    Returns:
        str | None: The ref; None when the text is not `:run <ref>`.
    """
    found = _RUN_PROMPT.fullmatch(text)
    return (found.group(1).strip() or None) if found else None


def split_run(text):
    """Split what `:run ` was typed with into the directory after `-C` and the ref.

    Args:
        text (str): The text after `:run `, as `typed_ref` returns it.

    Returns:
        tuple[str | None, str]: The directory (no spaces in it) and the ref after `-C <dir> `; or
        None and `text` as it is when it does not begin with `-C <dir> <ref>`.
    """
    found = re.fullmatch(r"-C\s+(\S+)\s+(\S.*)", text)
    return (found.group(1), found.group(2)) if found else (None, text)


def styled(text, meaning=None):
    """Text safe to draw, in the colour of its meaning.

    Args:
        text (str): The text, from anywhere.
        meaning (str | None): A key of `COLOURS`, or None for the text colour.

    Returns:
        Text: The cleaned text.
    """
    return Text(clean(text), COLOURS.get(meaning, ""))


def node(name, state, started, ended, running, now):
    """A stage's or a test's line in the tree: glyph, name, start and how long it took.

    Args:
        name (str): Its name.
        state (str): A `Stage.state` or `Section.outcome`.
        started (datetime | None): Its start.
        ended (datetime | None): Its end, or None.
        running (bool): The bench is running its run.
        now (datetime): The current time.

    Returns:
        Text: The line in its state's colour.
    """
    glyph, meaning = tone(state, running)
    label = f"{glyph} {name}"
    if started and state != "skipped":
        label += f"  {clock(started)}  {took(started, ended, running and state == 'open', now)}"
    return styled(label, meaning)


def log_lines(stage, section):
    """The log of what the tree has highlighted.

    Args:
        stage (Stage | None): The stage, or None for nothing to show.
        section (Section | None): The test or setup in it, or None for the whole stage.

    Returns:
        list[str]: The section's output and failure; else the stage's own output, then each
        section's name and output.
    """
    if stage is None:
        return []
    if section:
        return [*section.lines, *section.failure]
    lines = [shown_line(line) for line in stage.lines]
    for each in stage.sections:
        if each.started or each.failure:
            lines += [f"--- {each.name}", *each.lines, *each.failure]
    return lines


def head(run, stage, live, now):
    """The lines over the log: a suite's tally, a build's progress, else the run's summary.

    Args:
        run (Run): The run shown.
        stage (Stage | None): The stage shown, or None for the summary.
        live (bool): The bench is running this run.
        now (datetime): The current time.

    Returns:
        Text: The lines.
    """
    rows = []
    if stage is None:
        word, meaning = result(run, live)
        rows.append(styled(f"{word}, took {run_took(run, live, now)}", meaning))
    for suite in [stage] if stage else run.stages:
        started, queued, passed, failed = counts(suite)
        if queued:
            tally = f"{display_name(suite.name)}  {started} of {queued}  {passed} passed"
            rows.append(styled(tally + f"  {failed} failed" * bool(failed), failed and "failed"))
    if stage and stage.progress:
        progress = stage.progress
        rows.append(styled(f"{progress.phase} {progress.done} of {progress.total}"))
        if live and stage.state == "open":
            oldest = sorted(progress.tasks, key=lambda t: (t.started is None, t.started or now))
            for t in oldest[:8]:
                elapsed = took(t.started, None, True, now) if t.started else DASH
                rows.append(styled(f"  {t.recipe}  {t.task}  {elapsed}", "running"))
    return Text("\n").join(rows)


def failure(stopped):
    """The box a disabled bench shows.

    Args:
        stopped (str): The `DISABLED` file's text: its time, then its reason.

    Returns:
        Text: Whose failure it is, then the file's lines.
    """
    lines = ("bench failure, not the candidate's", *stopped.splitlines())
    return Text("\n").join(styled(line, "failed") for line in lines)


def history_rows(runs, bench, now):
    """The History table's rows.

    Args:
        runs (list[Run]): Every run, newest first.
        bench (Bench): The bench's state.
        now (datetime): The current time.

    Returns:
        list[tuple]: Per run, its record and its cells: record, what, sha, result, took, the stage
        that failed, and `by hand`.
    """
    rows = []
    for i, run in enumerate(runs):
        live = bench.live and i == 0
        failing = next((s.name for s in run.stages if s.state in ("failed", "error")), "")
        hand = styled("by hand" if run.kind == "test" else "", "hand")
        text = (run.record, what(run), sha7(run))
        ended = (run_took(run, live, now), display_name(failing))
        cells = (*map(styled, text), styled(*result(run, live)), *map(styled, ended), hand)
        rows.append((run.record, cells))
    return rows


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

    def add(key, place, source, label, sha, added="", stage=""):
        """Add a row.

        Args:
            key (str): Its key.
            place (str): Its place.
            source (str): Where it came from.
            label (str): What it runs.
            sha (str | None): The commit.
            added (str): When it was queued by hand.
            stage (str): The stage the running run is in.
        """
        hand = styled(source, "hand" if source == "by hand" else None)
        text = (label, (sha or "")[:7], added)
        rows.append((key, (styled(place), hand, *map(styled, text), styled(stage, "running"))))

    if bench.live and runs:
        run = runs[0]
        opened = next((display_name(s.name) for s in run.stages if s.state == "open"), "")
        running_sha = next((s.sha for s in run.stages if s.name == "checkout"), None)
        merge = next((e for e in entries if e["kind"] == "run" and e["pr"] == run.pr), None)
        label = what(run) + (f" {merge['title']}" if run.pr and merge else "")
        add("running", "▶", "by hand" if run.kind == "test" else "merge queue", label, running_sha,
            stage=opened)  # fmt: skip
    for entry in entries:
        place, sha = str(len(rows) + 1), entry["sha"]
        if entry["kind"] == "test":
            added = datetime.fromisoformat(entry["added"]).astimezone().strftime("%H:%M")
            add(f"hand {entry['id']}", place, "by hand", entry["ref"], sha, added)
        elif not sha or sha != running_sha:
            label = f"PR #{entry['pr']} {entry['title']}"
            add(f"merge {entry['pr']}", place, "merge queue", label, sha)
    return rows


def preview(ref, outcome):
    """What `:run <ref>` would queue.

    Args:
        ref (str): The ref typed.
        outcome (dict | Exception): `Queue.preview`'s result, or what it raised.

    Returns:
        Text: The commit, its subject and whether the checkout is clean; or that it does not
        resolve.
    """
    if isinstance(outcome, Exception):
        return styled(f"{ref} does not resolve", "failed")
    state = "clean tree" if outcome["clean"] else "uncommitted changes in the checkout"
    return styled(f'will queue {outcome["sha"][:7]} "{outcome["subject"]}", {state}', "hand")
