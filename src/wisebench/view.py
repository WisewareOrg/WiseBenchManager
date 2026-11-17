"""View: the watch view's domain rules, apart from any widget.

Which run and stage show, which build tasks are listed, the rows of a stage's log, which sections
open by default and where the log cursor lands, `/` search across folded sections, the Queue tab's
rows and the selection that stays on its row, and the `:run` preview.
"""

import re
from dataclasses import dataclass
from datetime import datetime

from wisebench.bench_state import Bench
from wisebench.runlog import KAS_STAMP, Run, Section, Stage, Task

_LOG_PREFIX = re.compile(KAS_STAMP + r"[A-Z]+\s+- ")
_RUN_PROMPT = re.compile(r":run\s+(.*)")
_RUNNING = "▶"


@dataclass(frozen=True)
class Snapshot:
    """The disk and the clock, read once per refresh.

    Args:
        runs (list[Run]): Every run, newest first.
        bench (Bench): The bench's live, disabled and timer state.
        now (datetime): The current time.
        directory (str): The records directory as the History footer names it.
        queue (list[dict] | None): The last good `Queue.entries()` read; None before one.
        preview (tuple[str, dict | str] | None): The last `Queue.preview` result: the ref it was
            asked for, and its result or the error text.
    """

    runs: list[Run]
    bench: Bench
    now: datetime
    directory: str
    queue: list[dict] | None = None
    preview: tuple[str, dict | str] | None = None


@dataclass(frozen=True)
class Row:
    """One row of a stage's log.

    Args:
        anchor (tuple): (stage name, section id or None, line); a section header is line 0.
        kind (str): "line" for the stage's own lines, "head" for a section header, "body" for a
            section's line.
        text (str): The stage's own line without kas's log prefix, a section line raw, or the
            section's name.
        section (Section | None): The section a header or body line belongs to.
        end (datetime | None): When a header's section ended: the next section's start, else the
            stage's end.
    """

    anchor: tuple
    kind: str
    text: str
    section: Section | None = None
    end: datetime | None = None


@dataclass(frozen=True)
class QueueRow:
    """One row of the Queue tab.

    Args:
        marker (str): "▶" for the running run, else its place in the queue, the running run
            counting as 1.
        source (str): "by hand" or "merge queue".
        what (str): A merge-queue entry reads "PR #<n> <title>"; the running run likewise when the
            merge queue holds an entry with its PR, else "PR #<n>", or its ref when it has no
            PR; a hand entry its ref.
        sha7 (str): The commit's first 7 characters; blank until known (a merge-queue entry
            GitHub has not built).
        added (str): A hand entry's local `HH:MM`; blank for the others.
        stage (str): The running run's open stage; blank for the others.
        kind (str): "test" or "run".
        id (int | None): A hand entry's id; None for the running run and the merge-queue entries.
        pr (int | None): A merge-queue entry's PR number; None for the others.
    """

    marker: str
    source: str
    what: str
    sha7: str
    added: str
    stage: str
    kind: str
    id: int | None
    pr: int | None


@dataclass(frozen=True)
class Frame:
    """What one refresh shows, resolved from the user's choices and a `Snapshot`.

    Args:
        run (Run | None): The run shown in Run.
        newest (bool): The run shown is the newest.
        live (bool): The run shown is the newest and the bench is running it.
        stage (Stage | None): The stage shown; None is the run's summary.
        pane_head (str): "build", "suite", "plain" or "summary".
        tasks (tuple[Task, ...]): Every running build task, oldest first.
        queue_rows (list[QueueRow]): The Queue tab's rows: the running run, the hand entries,
            then the merge-queue entries.
        preview_lines (list[str] | None): The preview box's lines while `:run <ref>` is typed and
            its result is in; None otherwise.
    """

    run: Run | None
    newest: bool
    live: bool
    stage: Stage | None
    pane_head: str
    tasks: tuple[Task, ...]
    queue_rows: list[QueueRow]
    preview_lines: list[str] | None


def resolve(snap, record=None, stage=None, prompt=None):
    """The frame to show over `snap`.

    Args:
        snap (Snapshot): The runs, the bench, the time and the Queue tab's reads.
        record (str | None): The run chosen; None, or a record gone from disk, is the newest.
        stage (str | None): The stage chosen; None is the automatic stage.
        prompt (str | None): The command line's text while it is open.

    Returns:
        Frame: What the widgets show.
    """
    runs = snap.runs
    run = next((r for r in runs if r.record == record), runs[0] if runs else None)
    newest = run is not None and run is runs[0]
    live = snap.bench.live and newest
    shown = _stage(stage, run, newest, snap.bench) if run is not None else None
    pane_head, tasks = _head(shown, live)
    queue_rows = _queue_rows(snap)
    return Frame(
        run=run,
        newest=newest,
        live=live,
        stage=shown,
        pane_head=pane_head,
        tasks=tasks,
        queue_rows=queue_rows,
        preview_lines=_preview_lines(prompt, snap.preview, queue_rows),
    )


def _pr_label(entry):
    """A merge-queue entry's `what`: `PR #<n> <title>`."""
    return f"PR #{entry['pr']} {entry['title']}"


def _queue_rows(snap):
    """The Queue tab's rows: the running run, the hand entries, then the merge-queue entries.

    The running run is the newest run while the bench is running it. A merge-queue entry is left
    out while its sha is the sha the running run checked out; the running run takes its title
    from the entry with its PR number.
    """
    rows, running_sha = [], None
    if snap.bench.live and snap.runs:
        run = snap.runs[0]
        opened = next((s for s in run.stages if s.state == "open"), None)
        checkout = next((s for s in run.stages if s.name == "checkout"), None)
        running_sha = checkout.sha if checkout else None
        what = run.ref or ""
        if run.pr:
            what = f"PR #{run.pr}"
            merge_entry = next(
                (e for e in snap.queue or [] if e["kind"] == "run" and e["pr"] == run.pr), None
            )
            if merge_entry:
                what = _pr_label(merge_entry)
        rows.append(
            QueueRow(
                _RUNNING,
                "by hand" if run.kind == "test" else "merge queue",
                what,
                (running_sha or "")[:7],
                "",
                opened.name if opened else "",
                run.kind or "",
                None,
                None,
            )
        )
    for entry in snap.queue or []:
        if entry["kind"] == "run" and entry["sha"] is not None and entry["sha"] == running_sha:
            continue
        hand = entry["kind"] == "test"
        added = (
            datetime.fromisoformat(entry["added"]).astimezone().strftime("%H:%M") if hand else ""
        )
        rows.append(
            QueueRow(
                str(len(rows) + 1),
                "by hand" if hand else "merge queue",
                entry["ref"] if hand else _pr_label(entry),
                (entry["sha"] or "")[:7],
                added,
                "",
                entry["kind"],
                entry["id"] if hand else None,
                None if hand else entry["pr"],
            )
        )
    return rows


def identity(row):
    """What keeps the Queue selection on a row as rows come and go.

    Args:
        row (QueueRow): A row of the Queue tab.

    Returns:
        tuple: ("running",), ("hand", id) or ("merge", pr).
    """
    if row.marker == _RUNNING:
        return ("running",)
    return ("hand", row.id) if row.id is not None else ("merge", row.pr)


def queue_index(at, rows):
    """Index of the Queue selection among `rows`; None without rows.

    Args:
        at (tuple | None): The identity of the row selected and its index; None selects the
            first row.
        rows (list[QueueRow]): The rows now.

    Returns:
        int | None: The selection's row, else the index it had; either is held to the last row.
    """
    if not rows:
        return None
    if at is None:
        return 0
    wanted, index = at
    found = next((i for i, row in enumerate(rows) if identity(row) == wanted), None)
    return min(found if found is not None else index, len(rows) - 1)


def typed_ref(prompt):
    """The ref typed after `:run `, stripped.

    Args:
        prompt (str | None): The command line's text, or None while it is closed.

    Returns:
        str | None: The ref; None when the command line holds no such ref.
    """
    found = prompt is not None and _RUN_PROMPT.fullmatch(prompt)
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


def _preview_lines(prompt, preview, rows):
    """The preview box for the ref typed after `:run `, once its result is in; else None.

    The place counts the running run and the hand entries up to the new one; the merge-queue
    entries come after it.
    """
    ref = typed_ref(prompt)
    if preview is None or preview[0] != ref:
        return None
    result = preview[1]
    if isinstance(result, str):
        return [f"{ref} does not resolve"]
    state = "clean tree" if result["clean"] else "uncommitted changes in the checkout"
    ahead = [row for row in rows if row.marker == _RUNNING or row.kind == "test"]
    place = f"joins as {len(ahead) + 1}" + (f", after {ahead[-1].what}" if ahead else "")
    return [
        "will queue",
        f'resolves to {result["sha"][:7]} "{result["subject"]}", {state}',
        place,
    ]


def _stage(chosen, run, newest, bench):
    """The stage shown: the one chosen, else the automatic stage.

    The automatic stage is the open stage of the newest run while it is live, none (the summary)
    for the newest run while the bench is idle, else the first stage that failed, errored or was
    left open.
    """
    if chosen is not None:
        return next((s for s in run.stages if s.name == chosen), None)
    if newest and bench.live:
        return next((s for s in run.stages if s.state == "open"), None)
    if newest and not bench.disabled:
        return None
    return next((s for s in run.stages if s.state in ("failed", "error", "open")), None)


def _head(stage, live):
    """(pane head, the running tasks) for the stage shown.

    A suite's head is its counts line and a plain stage's its output label. A build adds its
    progress line and bar, and while running a `running now` line and every running task, oldest
    first.
    """
    if stage is None:
        return "summary", ()
    if stage.sections:
        return "suite", ()
    if stage.progress is None:
        return "plain", ()
    if not (live and stage.state == "open"):
        return "build", ()
    return "build", tuple(sorted(stage.progress.tasks, key=_by_start))


def _by_start(task):
    """Sort key for running tasks: by start time, a task without one last."""
    return (task.started is None, task.started.timestamp() if task.started else 0)


def is_open(folds, section):
    """Whether a section's body shows: the choice in `folds`, else open while running or failed.

    Args:
        folds (dict): Section id to True (open) or False (folded).
        section (Section): The section.

    Returns:
        bool: True if the body shows.
    """
    return folds.get(section.id, section.outcome in ("open", "failed"))


def rows(stage):
    """Every log row of a stage, folded or not: its own lines, then each section's header and body.

    Args:
        stage (Stage): The stage.

    Returns:
        list[Row]: The rows in display order.
    """
    out = [
        Row((stage.name, None, i), "line", _LOG_PREFIX.sub("", line))
        for i, line in enumerate(stage.lines)
    ]
    for section, end in zip(stage.sections, _section_ends(stage), strict=True):
        out.append(Row((stage.name, section.id, 0), "head", section.name, section, end))
        body = [*section.lines, *section.failure]
        out.extend(
            Row((stage.name, section.id, i), "body", line, section)
            for i, line in enumerate(body, 1)
        )
    return out


def _section_ends(stage):
    """When each section of a stage ended: the next started section's start, else the stage's end.

    Returns:
        list[datetime | None]: One end per section, in section order.
    """
    ends, following = [], stage.ended
    for section in reversed(stage.sections):
        ends.append(following)
        if section.started is not None:
            following = section.started
    return ends[::-1]


def default_anchor(stage):
    """Where the log cursor lands until the user moves it; None on a stage with no rows.

    Args:
        stage (Stage): The stage shown.

    Returns:
        tuple | None: The first failed section's header, else the running section's header,
        else the first row; a stage without sections lands on its last line.
    """
    if not stage.sections:
        return (stage.name, None, len(stage.lines) - 1) if stage.lines else None
    wanted = next((s for s in stage.sections if s.outcome == "failed"), None) or next(
        (s for s in stage.sections if s.outcome == "open"), None
    )
    if wanted:
        return (stage.name, wanted.id, 0)
    return rows(stage)[0].anchor


def search(stage, at, pattern, direction):
    """The next (1) or previous (-1) row of `stage` after the anchor `at` that holds `pattern`.

    It wraps, and covers every line of the stage, folded bodies included.

    Args:
        stage (Stage): The stage shown.
        at (tuple): The anchor of the row the cursor is on.
        pattern (str): The text searched for, case-sensitive.
        direction (int): 1 or -1.

    Returns:
        Row | None: The match; None on a miss.
    """
    every = rows(stage)
    start = next(i for i, row in enumerate(every) if row.anchor == at)
    for step in range(1, len(every) + 1):
        row = every[(start + direction * step) % len(every)]
        if pattern in row.text:
            return row
    return None
