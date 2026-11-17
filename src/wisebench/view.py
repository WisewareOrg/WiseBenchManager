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

GROUND, TEXT, DIM, LOG, RULE = "#0c0c0c", "#cccccc", "#8b8b8b", "#a8a8a8", "#3a3a3a"
BRIGHT, GREEN, AMBER, RED, BLUE = "#ffffff", "#98c379", "#e5c07b", "#e06c75", "#61afef"
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
    """A stage pane's head: a suite's tally, or a build's progress line over its bar."""
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
    text, filled = f"  {progress.phase}", 0
    if progress.total:
        text += f" {progress.done} of {progress.total}  {progress.done * 100 // progress.total}%"
        filled = round(33 * progress.done / progress.total)
    bar = line(("█" * filled, colour), ("█" * (33 - filled), RULE))
    return Text("\n").join([line(("build", colour, True), (text, None)), bar])


def by_start(task):
    """Sort key for running build tasks: by start time, a task without one last."""
    return (task.started is None, task.started.timestamp() if task.started else 0)


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


def queue_rows(runs, bench, entries):
    """The Queue's rows: the running run, the hand-queued runs, then the merge queue's entries.

    The running run takes its title from the merge-queue entry with its PR, and the entry whose
    sha the running run checked out is left out.

    Returns:
        list[tuple]: `(key, hand id or None, what, cells)` per row; the key stays with its row.
    """
    rows, running_sha = [], None
    if bench.live and runs:
        run = runs[0]
        running_sha = run.stages[0].sha
        title = what(run)
        for entry in entries:
            if entry["kind"] == "run" and run.pr is not None and entry["pr"] == run.pr:
                title = f"PR #{entry['pr']} {entry['title']}"
        opened = next((s for s in run.stages if s.state == "open"), None)
        stage = f"running, {name_of(opened)}" if opened else ""
        source = "by hand" if run.kind == "test" else "merge queue"
        rows.append(("running", None, title, ("▶", source, title, sha7(run), "", stage)))
    for entry in entries:
        sha = (entry["sha"] or "")[:7]
        if entry["kind"] == "test":
            added = datetime.fromisoformat(entry["added"]).astimezone().strftime("%H:%M")
            cells = (str(len(rows) + 1), "by hand", entry["ref"], sha, added, "")
            rows.append((f"hand {entry['id']}", entry["id"], entry["ref"], cells))
        elif entry["sha"] is None or entry["sha"] != running_sha:
            title = f"PR #{entry['pr']} {entry['title']}"
            cells = (str(len(rows) + 1), "merge queue", title, sha, "", "")
            rows.append((f"merge {entry['pr']}", None, title, cells))
    return rows


def queue_cells(cells):
    """A Queue row's cells as drawn: the running marker and stage amber, `by hand` blue."""
    colours = (AMBER, BLUE if cells[1] == "by hand" else None, None, None, None, AMBER)
    return [line((cell, colour)) for cell, colour in zip(cells, colours, strict=True)]


def typed_ref(text):
    """The ref typed after `:run `, stripped; None when the command line holds no such ref."""
    found = re.fullmatch(r":run\s+(.*)", text or "")
    return (found.group(1).strip() or None) if found else None


def split_run(text):
    """(directory after `-C`, ref) of `:run`'s argument; (None, text) without `-C <dir> <ref>`."""
    found = re.fullmatch(r"-C\s+(\S+)\s+(\S.*)", text)
    return (found.group(1), found.group(2)) if found else (None, text)


def preview(ref, outcome, rows):
    """The `:run` preview: what the ref resolves to and where it would join the queue.

    It joins after the running run and the hand-queued runs, ahead of the merge queue's entries.
    `outcome` is `Queue.preview`'s result, or the exception it raised.
    """
    if isinstance(outcome, Exception):
        return line((f"{ref} does not resolve", RED))
    state = "clean tree" if outcome["clean"] else "uncommitted changes in the checkout"
    ahead = [row for row in rows if row[0] == "running" or row[1] is not None]
    place = f"joins as {len(ahead) + 1}" + (f", after {ahead[-1][2]}" if ahead else "")
    return Text("\n", no_wrap=True, overflow="ellipsis").join(
        [
            line(("will queue", BLUE, True)),
            line((f'resolves to {outcome["sha"][:7]} "{outcome["subject"]}", {state}', DIM)),
            line((place, DIM)),
        ]
    )


def history_cells(run, running, now):
    """A History row: record, what, sha, result, took, then the failing stage or `by hand`."""
    word, colour = result(run, running)
    failing = next((name_of(s) for s in run.stages if s.state in ("failed", "error")), "")
    notes = [(failing, DIM)] + [("  by hand", BLUE)] * (run.kind == "test")
    return [
        line((run.record, None)),
        line((what(run)[:24], None)),
        line((sha7(run), None)),
        line((word, colour)),
        line((f"{run_took(run, running, now):>4}", None)),
        line(*notes),
    ]


HELP = (
    ("move", ("Tab S-Tab", "next / prev view"), ("1-3", "jump to view"), ("j k", "down / up"),
     ("gg G", "top / bottom"), ("^d ^u", "half page"), ("Enter", "open selection")),
    ("runs and log", ("[ ]", "older / newer run"), ("1", "back to the live run"),
     ("dd", "remove hand-queued run"), ("za", "fold / unfold a stage"),
     ("zR zM", "unfold / fold all"), ("/", "search"), ("n N", "next / prev match"),
     ("G", "follow the live log")),
    ("commands", (":run ref", "queue a ref"), (":disable", "stop the bench"),
     (":enable", "start it again"), (":q", "quit"), ("?", "this screen"), ("q", "quit")),
)  # fmt: skip


def help_columns():
    """The keymap's three columns, each a heading over key and meaning lines."""
    return [
        Text("\n").join(
            [
                line((heading, BLUE)),
                *(line((f"{key:<10}", AMBER), (words, None)) for key, words in keys),
            ]
        )
        for heading, *keys in HELP
    ]
