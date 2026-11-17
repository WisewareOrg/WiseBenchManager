"""View rules: what the watch view shows, as plain functions of the run model and the queue."""

import re
from dataclasses import dataclass
from datetime import datetime

from wisebench.bench_state import DASH
from wisebench.runlog import KAS_STAMP

RUNNING = "▶"
_LOG_PREFIX = re.compile(KAS_STAMP + r"[A-Z]+\s+- ")
_RUN_PROMPT = re.compile(r":run\s+(.*)")


def auto_stage(run, newest, bench):
    """The stage shown until the user picks one; None is the run's summary.

    The open stage of the newest run while it is live; the summary of the newest run while the
    bench is idle; otherwise the first stage that failed, errored or was left open.
    """
    if newest and bench.live:
        return next((s for s in run.stages if s.state == "open"), None)
    if newest and not bench.disabled:
        return None
    return next((s for s in run.stages if s.state in ("failed", "error", "open")), None)


def unfolded(folds, section):
    """Whether a section's body shows: as `folds` says, else while it runs or failed."""
    return folds.get(section.id, section.outcome in ("open", "failed"))


def log_rows(stage, folds, expand=False):
    """A stage's log rows: its own lines, then each section's header and unfolded body.

    A row is `(None, i)` for the stage's line i, or `(section, line)`: line 0 the header, then
    the body's output lines and failure lines. `expand` lists folded bodies too.
    """
    rows = [(None, i) for i in range(len(stage.lines))]
    for section in stage.sections:
        rows.append((section, 0))
        if expand or unfolded(folds, section):
            body = len(section.lines) + len(section.failure)
            rows.extend((section, line) for line in range(1, body + 1))
    return rows


def row_text(stage, row):
    """A log row's text: a stage line without kas's log prefix, a header's name, a body line."""
    section, line = row
    if section is None:
        return _LOG_PREFIX.sub("", stage.lines[line])
    return section.name if line == 0 else (*section.lines, *section.failure)[line - 1]


def opening_row(stage, rows):
    """Where the log cursor lands on a stage.

    On the first failed section's header, else the running one's; a suite with neither on its first
    row, a stage without sections on its last.
    """
    if not stage.sections:
        return max(len(rows) - 1, 0)
    wanted = next((s for s in stage.sections if s.outcome == "failed"), None) or next(
        (s for s in stage.sections if s.outcome == "open"), None
    )
    return rows.index((wanted, 0)) if wanted else 0


def find(rows, row):
    """Where a row of an earlier listing of the stage sits in `rows`.

    The row itself, else its section's header, sections matched by id; None when neither is listed.
    """
    section, line = row
    if section is None:
        return line if line < len(rows) else None
    found = {i: k for k, (s, i) in enumerate(rows) if s is not None and s.id == section.id}
    return found.get(line, found.get(0))


def section_ends(stage):
    """Section id to its end: the next started section's start, else the stage's end."""
    ends, following = {}, stage.ended
    for section in reversed(stage.sections):
        ends[section.id] = following
        if section.started is not None:
            following = section.started
    return ends


def search(stage, folds, at, pattern, direction):
    """The next (1) or previous (-1) row after row `at` holding `pattern`; None on a miss.

    The search wraps, and looks in folded bodies too.
    """
    rows = log_rows(stage, folds, expand=True)
    start = rows.index(at)
    for step in range(1, len(rows) + 1):
        row = rows[(start + direction * step) % len(rows)]
        if pattern in row_text(stage, row):
            return row
    return None


def clock(moment):
    """`HH:MM:SS` in local time."""
    return moment.astimezone().strftime("%H:%M:%S")


def took(started, ended, running, now):
    """`MmSSs` from start to end, or to now while running; a dash when neither."""
    if ended is None and not running:
        return DASH
    seconds = int(((ended or now) - started).total_seconds())
    return f"{seconds // 60}m{seconds % 60:02d}s"


def run_took(run, running, now):
    """A run's whole minutes from its start to its end, or to now while running; else a dash."""
    end = now if running else run.ended
    if run.started is None or end is None:
        return DASH
    return f"{int((end - run.started).total_seconds()) // 60}m"


def what(run):
    """What a run tested: `PR #n`, else its ref."""
    return f"PR #{run.pr}" if run.pr is not None else run.ref


def sha7(run):
    """The first seven characters of the run's checked-out commit, or a dash."""
    checkout = next((s for s in run.stages if s.name == "checkout"), None)
    return checkout.sha[:7] if checkout and checkout.sha else DASH


def counts(stage):
    """A suite's tests, setups left out: (started or reported, queued, passed, failed)."""
    tests = [s for s in stage.sections if s.kind == "test"]
    return (
        sum(1 for s in tests if s.outcome != "not-started"),
        len(tests),
        sum(1 for s in tests if s.outcome == "passed"),
        sum(1 for s in tests if s.outcome == "failed"),
    )


@dataclass(frozen=True)
class QueueRow:
    """One row of the Queue tab.

    Args:
        marker (str): `RUNNING` for the running run, else its place, the running run counting 1.
        source (str): "by hand" or "merge queue".
        what (str): "PR #<n> <title>", "PR #<n>" or the ref.
        sha7 (str): The commit's first 7 characters; blank until GitHub has built it.
        added (str): A hand entry's local `HH:MM`; blank for the others.
        stage (str): The running run's open stage; blank for the others.
        key (tuple): What keeps the selection on this row as rows come and go.
        id (int | None): A hand entry's id, which `dd` removes; None for the others.
    """

    marker: str
    source: str
    what: str
    sha7: str
    added: str
    stage: str
    key: tuple
    id: int | None = None


def queue_rows(runs, bench, entries):
    """The Queue tab's rows: the running run, then the hand entries, then the merge queue.

    The running run takes its title from the merge entry with its PR, and the merge entry whose
    sha it checked out is left out.
    """
    rows, running_sha = [], None
    if bench.live and runs:
        run = runs[0]
        opened = next((s.name for s in run.stages if s.state == "open"), "")
        running_sha = next((s.sha for s in run.stages if s.name == "checkout"), None)
        title = run.ref or ""
        if run.pr:
            entry = next((e for e in entries if e["kind"] == "run" and e["pr"] == run.pr), None)
            title = f"PR #{run.pr} {entry['title']}" if entry else f"PR #{run.pr}"
        source = "by hand" if run.kind == "test" else "merge queue"
        rows.append(QueueRow(RUNNING, source, title, (running_sha or "")[:7], "", opened, ()))
    for entry in entries:
        if entry["kind"] == "run" and entry["sha"] is not None and entry["sha"] == running_sha:
            continue
        place, sha = str(len(rows) + 1), (entry["sha"] or "")[:7]
        if entry["kind"] == "test":
            added = datetime.fromisoformat(entry["added"]).astimezone().strftime("%H:%M")
            key = ("hand", entry["id"])
            rows.append(QueueRow(place, "by hand", entry["ref"], sha, added, "", key, entry["id"]))
        else:
            title = f"PR #{entry['pr']} {entry['title']}"
            rows.append(QueueRow(place, "merge queue", title, sha, "", "", ("merge", entry["pr"])))
    return rows


def typed_ref(text):
    """The ref typed after `:run `, stripped; None when the text holds no such ref."""
    found = _RUN_PROMPT.fullmatch(text)
    return (found.group(1).strip() or None) if found else None


def split_run(text):
    """`(directory, ref)` from `-C <dir> <ref>`, else `(None, text)`."""
    found = re.fullmatch(r"-C\s+(\S+)\s+(\S.*)", text)
    return (found.group(1), found.group(2)) if found else (None, text)


def preview_lines(ref, result, rows):
    """The `:run` preview's lines: what `ref` resolves to and where it joins the queue.

    It joins after the running run and the hand entries. `result` is `Queue.preview`'s, or the text
    of its failure.
    """
    if isinstance(result, str):
        return [f"{ref} does not resolve"]
    state = "clean tree" if result["clean"] else "uncommitted changes in the checkout"
    ahead = [row for row in rows if row.marker == RUNNING or row.source == "by hand"]
    place = f"joins as {len(ahead) + 1}" + (f", after {ahead[-1].what}" if ahead else "")
    return [
        "will queue",
        f'resolves to {result["sha"][:7]} "{result["subject"]}", {state}',
        place,
    ]
