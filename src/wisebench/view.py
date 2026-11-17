"""The watch view's rules: what the operator sees of the runs, the bench and the queue.

Nothing here draws. `status` lays these words out with Textual; the rules live here so they can be
read, and tested, without a terminal.
"""

import re
from dataclasses import dataclass
from datetime import datetime

from wisebench.bench_state import DASH
from wisebench.runlog import KAS_STAMP

RUNNING = "▶"
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


def is_open(section, folds):
    """Whether a section's body shows: the operator's choice, else open while running or failed.

    Args:
        section (Section): The section.
        folds (dict): Section id to True (open) or False (folded), as the operator chose.

    Returns:
        bool: True if the body shows.
    """
    return folds.get(section.id, section.outcome in ("open", "failed"))


def landing(stage):
    """The section the log cursor starts on: the first failed one, else the running one.

    Args:
        stage (Stage): The stage shown.

    Returns:
        Section | None: The section, or None: the top of a suite, the tail of any other stage.
    """
    return next((s for s in stage.sections if s.outcome == "failed"), None) or next(
        (s for s in stage.sections if s.outcome == "open"), None
    )


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


def pane_form(stage):
    """Which form the pane takes for the stage shown.

    Args:
        stage (Stage | None): The stage shown; None is the summary.

    Returns:
        str: "summary", "suite" (it has sections), "build" (it has bitbake progress) or "plain".
    """
    if stage is None:
        return "summary"
    if stage.sections:
        return "suite"
    return "plain" if stage.progress is None else "build"


def by_start(task):
    """Sort key for running build tasks: oldest first, a task without a start last.

    Args:
        task (Task): A running task.

    Returns:
        tuple: The key.
    """
    return task.started is None, task.started.timestamp() if task.started else 0


def record_width(runs):
    """The History record column's width: the longest record name and two spaces, at least 18.

    Args:
        runs (list[Run]): Every run.

    Returns:
        int: The width.
    """
    return max(18, max((len(run.record) for run in runs), default=0) + 2)


def history_row(run, running, now):
    """One History row: record, what, sha, result, took, then the failing stage or `by hand`.

    Args:
        run (Run): The run.
        running (bool): The bench is running it.
        now (datetime): The current time.

    Returns:
        tuple: The record, what, sha and took as text; the result and the note as
        (text, meaning).
    """
    failing = next((s for s in run.stages if s.state in ("failed", "error")), None)
    if failing is not None:
        note = (display_name(failing.name), "dim")
    else:
        note = ("by hand", "hand") if run.kind == "test" else ("", "dim")
    return run.record, what(run), sha7(run), result(run, running), run_took(run, running, now), note


@dataclass(frozen=True)
class QueueRow:
    """One row of the Queue tab.

    Args:
        key (str): What keeps the selection on the row as rows come and go.
        marker (str): "▶" for the running run, else its place, the running run counting as 1.
        source (str): "by hand" or "merge queue".
        what (str): "PR #<n> <title>" for a merge entry, the ref for a hand entry; the running
            run as its merge entry reads, else "PR #<n>", else its ref.
        sha7 (str): The commit's first 7 characters; blank until GitHub builds a merge entry.
        added (str): A hand entry's local `HH:MM`; blank for the others.
        stage (str): The running run's open stage; blank for the others.
        id (int | None): A hand entry's id; None for the others.
    """

    key: str
    marker: str
    source: str
    what: str
    sha7: str
    added: str
    stage: str
    id: int | None


def queue_rows(runs, bench, entries):
    """The Queue tab's rows: the running run, the hand entries, then the merge-queue entries.

    The running run is the newest run while the bench runs it. It takes its title from the merge
    entry with its PR, and the merge entry for the commit it checked out is left out.

    Args:
        runs (list[Run]): Every run, newest first.
        bench (Bench): The bench's state.
        entries (list[dict] | None): `Queue.entries()`, or None before the first read.

    Returns:
        list[QueueRow]: The rows in order.
    """
    rows, running_sha, entries = [], None, entries or []
    if bench.live and runs:
        run = runs[0]
        opened = next((s.name for s in run.stages if s.state == "open"), "")
        running_sha = next((s.sha for s in run.stages if s.name == "checkout"), None)
        merge = next((e for e in entries if e["kind"] == "run" and e["pr"] == run.pr), None)
        label = f"PR #{merge['pr']} {merge['title']}" if run.pr and merge else what(run)
        source = "by hand" if run.kind == "test" else "merge queue"
        short = (running_sha or "")[:7]
        rows.append(QueueRow("running", RUNNING, source, label, short, "", opened, None))
    for entry in entries:
        sha = entry["sha"] or ""
        if entry["kind"] == "test":
            added = datetime.fromisoformat(entry["added"]).astimezone().strftime("%H:%M")
            key, source, label, entry_id = (
                f"hand {entry['id']}",
                "by hand",
                entry["ref"],
                entry["id"],
            )
        elif sha and sha == running_sha:
            continue
        else:
            added, entry_id = "", None
            key, source, label = (
                f"merge {entry['pr']}",
                "merge queue",
                f"PR #{entry['pr']} {entry['title']}",
            )
        rows.append(QueueRow(key, str(len(rows) + 1), source, label, sha[:7], added, "", entry_id))
    return rows


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


def preview_lines(ref, outcome, rows):
    """The `:run` preview box: what `ref` resolves to, and where it joins the queue.

    The place counts the running run and the hand entries; the merge-queue entries come after it.

    Args:
        ref (str): The ref typed.
        outcome (dict | str): `Queue.preview`'s result, or the text of its error.
        rows (list[QueueRow]): The Queue tab's rows.

    Returns:
        list[str]: The box's lines; the first is its title.
    """
    if isinstance(outcome, str):
        return [f"{ref} does not resolve"]
    state = "clean tree" if outcome["clean"] else "uncommitted changes in the checkout"
    ahead = [row for row in rows if row.marker == RUNNING or row.id is not None]
    place = f"joins as {len(ahead) + 1}" + (f", after {ahead[-1].what}" if ahead else "")
    return [
        "will queue",
        f'resolves to {outcome["sha"][:7]} "{outcome["subject"]}", {state}',
        place,
    ]
