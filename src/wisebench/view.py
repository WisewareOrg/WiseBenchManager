"""View rules: what the watch view shows, as plain functions independent of the toolkit.

Which stage shows, which sections start open, where the log cursor starts, how the running run
merges into the queue, what `:run` previews, and the text and colour of each row.
"""

import re
from datetime import datetime

from rich.style import Style
from rich.text import Text

from wisebench.bench_state import DASH
from wisebench.runlog import KAS_STAMP

GROUND, TEXT, DIM, LOG, RULE, BRIGHT = (
    "#0c0c0c",
    "#cccccc",
    "#8b8b8b",
    "#a8a8a8",
    "#3a3a3a",
    "#ffffff",
)
GREEN, AMBER, RED, BLUE = "#98c379", "#e5c07b", "#e06c75", "#61afef"
RUNNING_TINT, FAILED_TINT, SELECTION = "#2a2614", "#3a1d20", "#1f2a36"

_LOG_PREFIX = re.compile(KAS_STAMP + r"[A-Z]+\s+- ")
_CONTROLS = {c: "^" + chr(c ^ 64) for c in [*range(32), 127]} | {
    c: f"<{c:02x}>" for c in range(0x80, 0xA0)
}
_RESULTS = {"passed": GREEN, "failed": RED, "error": RED}


def clean(text):
    """`text` with tabs expanded and every C0, DEL and C1 control shown as visible characters."""
    return text.expandtabs(8).translate(_CONTROLS)


def line(*parts):
    """A line of `(text, colour[, bold])` parts, each cleaned; a colour of None is the text's."""
    out = Text(no_wrap=True, overflow="ellipsis", end="")
    for text, colour, *bold in parts:
        out.append(clean(str(text)), Style(color=colour, bold=bool(bold and bold[0])))
    return out


def clock(moment):
    """`HH:MM:SS` in local time, or a dash."""
    return moment.astimezone().strftime("%H:%M:%S") if moment else DASH


def took(started, ended, running, now):
    """`MmSSs` from start to end, or to now while running; else a dash."""
    end = ended or (now if running else None)
    if started is None or end is None:
        return DASH
    seconds = int((end - started).total_seconds())
    return f"{seconds // 60}m{seconds % 60:02d}s"


def run_took(run, running, now):
    """A run's whole minutes, from its start to its end, or to now while running."""
    end = now if running else run.ended
    if run.started is None or end is None:
        return DASH
    return f"{int((end - run.started).total_seconds()) // 60}m"


def result(run, running):
    """(word, colour) of a run's result: `running` while the bench runs it."""
    if running:
        return "running", AMBER
    word = {"error": "bench error", None: "no result"}.get(run.result, run.result)
    return word, _RESULTS.get(run.result, DIM)


def name_of(stage):
    """A stage name as shown: `host_suite` reads `host suite`."""
    return stage.name.replace("_", " ")


def sha7(run):
    """The first seven characters of the commit the run checked out, or a dash."""
    checkout = run.stages[0]
    return checkout.sha[:7] if checkout.sha else DASH


def what(run):
    """What a run tested: `PR #n`, else its ref."""
    return f"PR #{run.pr}" if run.pr is not None else run.ref or ""


def tone(state, running):
    """(glyph, colour, bold) of a stage or section state; `running`: an open one is live."""
    if state == "passed":
        return "✓", GREEN, False
    if state in ("failed", "error"):
        return "✗", RED, True
    if state == "open":
        return ("●", AMBER, True) if running else ("?", DIM, False)
    return ("–" if state == "skipped" else "·"), DIM, False


def stopped_at(run):
    """The first stage that failed, errored or was left open, or None."""
    return next((s for s in run.stages if s.state in ("failed", "error", "open")), None)


def auto_stage(run, newest, bench):
    """The stage shown until one is chosen; None is the summary.

    The newest run's open stage while it is live, its summary while the bench is idle, and
    otherwise the first stage that failed, errored or was left open.
    """
    if newest and bench.live:
        return next((s for s in run.stages if s.state == "open"), None)
    return None if newest and not bench.disabled else stopped_at(run)


def starts_open(section):
    """Whether a section's body shows before the user folds it: while running or failed."""
    return section.outcome in ("open", "failed")


def body(section):
    """A section's body: its output, then a failed test's crash line and traceback."""
    return (*section.lines, *section.failure)


def first_section(stage):
    """The section the log cursor starts on: the first failed, else the running one, else None."""
    failed = (s for s in stage.sections if s.outcome == "failed")
    return next(failed, None) or next((s for s in stage.sections if s.outcome == "open"), None)


def section_ends(stage):
    """When each section ended: the next started section's start, else the stage's end."""
    ends, following = [], stage.ended
    for section in reversed(stage.sections):
        ends.append(following)
        following = section.started or following
    return ends[::-1]


def own_line(text):
    """A stage's own output line as shown, without kas's log prefix."""
    return _LOG_PREFIX.sub("", text)


def counts(stage):
    """(started, queued, passed, failed) over a stage's test sections."""
    tests = [s.outcome for s in stage.sections if s.kind == "test"]
    started = sum(1 for o in tests if o != "not-started")
    return started, len(tests), tests.count("passed"), tests.count("failed")


def stage_row(stage, running, now):
    """A stage list row: glyph, name, start and took, all in the stage's colour."""
    glyph, colour, bold = tone(stage.state, running)
    text = f"{glyph} {name_of(stage)}".ljust(17)
    if stage.started is not None and stage.state not in ("not-started", "skipped"):
        text += clock(stage.started).ljust(10) + took(stage.started, stage.ended, running, now)
    return line((text, colour, bold))


def run_header(run, newest, bench):
    """The Run header: title, sha and record; `last run` first while idle, the result when past."""
    parts = [(what(run), BRIGHT, True), (f"  {sha7(run)}  record {run.record}", DIM)]
    if not newest:
        word, colour = result(run, False)
        parts += [("  ", None), (word, colour, True), (f"  ended {clock(run.ended)}", DIM)]
    elif not bench.live and not bench.disabled:
        parts.insert(0, ("last run  ", DIM))
    return line(*parts)


def summary(run, live, now):
    """The pane with no stage shown: the result, its end and length, and each suite's tally."""
    word, colour = result(run, live)
    ended = "" if live else f"  ended {clock(run.ended)},"
    lines = [line((word, colour, True), (f"{ended} took {run_took(run, live, now)}", DIM))]
    for stage in run.stages[4:6]:
        _, queued, passed, failed = counts(stage)
        if queued:
            tally = f"{passed} passed" + (f"  {failed} failed" if failed else "")
            lines.append(line((f"{name_of(stage):<14}", DIM), (tally, None)))
    return Text("\n", no_wrap=True, overflow="ellipsis").join(lines)


def stage_head(stage, running):
    """A stage pane's first line: a suite's tally, a build's progress, or nothing."""
    _, colour, _ = tone(stage.state, running)
    if stage.sections:
        done, queued, passed, failed = counts(stage)
        return line(
            (name_of(stage), colour, True),
            (f"  {done} of {queued}  ", None),
            (f"{passed} passed", GREEN),
            (f"  {failed} failed", RED if failed else DIM),
        )
    progress = stage.progress
    text = f"  {progress.phase}"
    if progress.total:
        text += f" {progress.done} of {progress.total}  {progress.done * 100 // progress.total}%"
    return line(("build", colour, True), (text, None))


def by_start(task):
    """Sort key for running build tasks: by start time, a task without one last."""
    return (task.started is None, task.started or datetime.min.astimezone())


def section_head(section, opened, end, running, now):
    """A section header: fold glyph, glyph and name in the outcome's colour, then took."""
    fold = ("▾ " if opened else "▸ ") if body(section) else "  "
    glyph, colour, bold = tone(section.outcome, running)
    parts = [(fold, DIM), (f"{glyph} ", colour, bold)]
    if section.kind == "setup":
        parts += [("setup", BLUE, bold), (" ", colour, bold)]
    parts.append((section.name, colour, bold))
    if section.started is not None:
        live = running and section.outcome == "open"
        parts.append((f"  {took(section.started, end, live, now)}", DIM))
    return line(*parts)


def failure_box(reason, at, run):
    """The bench failure box: the `DISABLED` reason, where the run stopped, and when."""
    stage = run and stopped_at(run)
    where = ""
    if stage is not None:
        last = [s for s in stage.sections if s.started is not None][-1:]
        where = f"in {name_of(stage)}" + "".join(
            f" {s.kind} {s.name}" if s.kind == "setup" else f" {s.name}" for s in last
        )
        where += "; "
    return Text("\n", no_wrap=True, overflow="ellipsis").join(
        [
            line(("bench failure, not the candidate's", RED, True)),
            line((reason, None)),
            line((f"{where}from the DISABLED file, {clock(at)}", DIM)),
        ]
    )
