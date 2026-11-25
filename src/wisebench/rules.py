"""The watch view's rules: which run, stage, test and queue entry the operator sees.

Plain values in and out; `view` words them.
"""

import re
from collections import Counter
from datetime import datetime
from typing import NamedTuple

from wisebench.bench_state import DASH, Bench
from wisebench.runlog import Section

_RUN_PROMPT = re.compile(r":run\s+(.*)")


class Seen(NamedTuple):
    """One read of the disk.

    Args:
        runs (list[Run]): Every run, newest first.
        bench (Bench): The bench's state.
        stopped (str): The `DISABLED` file's text; blank while the bench is not disabled.
        now (datetime | None): When it was read, while a run is live; nothing else shows the time.
        directory (str): The records directory's name.
        board (str | None): The added board read; None for the first board.
    """

    runs: list
    bench: Bench
    stopped: str
    now: datetime | None
    directory: str
    board: str | None = None

    @property
    def newest(self):
        """Run | None: The newest run."""
        return self.runs[0] if self.runs else None

    @property
    def running(self):
        """Run | None: The run the bench is executing."""
        return self.newest if self.bench.live else None


NOTHING = Seen([], Bench(False, False, DASH), "", None, "")


class Waiting(NamedTuple):
    """One row of the queue.

    Args:
        key (str): `running`, `hand <id>` or `merge <pr>`.
        by_hand (bool): It was queued by hand.
        label (str): What it runs.
        sha (str): Its commit; blank until it has one.
        added (datetime | None): When it was queued by hand.
        stage (str): The running run's open stage.
    """

    key: str
    by_hand: bool
    label: str
    sha: str
    added: datetime | None = None
    stage: str = ""


def queue(seen, entries):
    """The queue: the running run, the hand entries, then the merge-queue entries.

    The running run takes its title from the merge entry with its PR, and the merge entry for the
    commit it checked out is left out.

    Args:
        seen (Seen): The read: the running run, if any.
        entries (list[dict]): `Queue.entries()`.

    Returns:
        list[Waiting]: The rows.
    """
    rows, run, running_sha = [], seen.running, None
    if run:
        opened = next((s.name for s in run.stages if s.state == "open"), "")
        running_sha = sha(run)
        merge = next((e for e in entries if e["kind"] == "run" and e["pr"] == run.pr), None)
        label = what(run) + (f" {merge['title']}" if run.pr and merge else "")
        rows.append(Waiting("running", run.kind == "test", label, running_sha, None, opened))
    for entry in entries:
        commit = entry["sha"] or ""
        if entry["kind"] == "test":
            added = datetime.fromisoformat(entry["added"])
            rows.append(Waiting(f"hand {entry['id']}", True, entry["ref"], commit, added))
        elif not commit or commit != running_sha:
            title = f"PR #{entry['pr']} {entry['title']}"
            rows.append(Waiting(f"merge {entry['pr']}", False, title, commit))
    return rows


def what(run):
    """What a run tested: `PR #n`, else its ref.

    Args:
        run (Run): The run.

    Returns:
        str: The label.
    """
    return f"PR #{run.pr}" if run.pr is not None else run.ref or ""


def sha(run):
    """The commit the run checked out; blank before it has.

    Args:
        run (Run): The run.

    Returns:
        str: The sha.
    """
    return next((s.sha for s in run.stages if s.name == "checkout"), None) or ""


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
        Counter: How many of its tests have each outcome.
    """
    return Counter(s.outcome for s in stage.sections if s.kind == "test")


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


def outline(stage):
    """A suite's sections as its tree shows them, with when each ended.

    A suite's own output, when it has any, comes first, as a section that failed when the stage
    errored; a section ends when the next started one starts, the last when the stage does.

    Args:
        stage (Stage): The suite.

    Returns:
        list[tuple[Section, datetime | None]]: Each section and its end.
    """
    rows, end = [], stage.ended
    for section in reversed(stage.sections):
        rows.append((section, end))
        end = section.started or end
    if stage.lines:
        outcome = "failed" if stage.state == "error" else "passed"
        key = (stage.name, "output", "", 0)
        output = Section(key, "output", "output", outcome, None, stage.lines, ())
        rows.append((output, None))
    return rows[::-1]


def landing(sections):
    """Where a suite's cursor lands: its first failed section, else its open one, else its first.

    Args:
        sections (list[Section]): The suite's sections as `outline` gives them.

    Returns:
        int: The place.
    """
    rank = {"failed": 0, "open": 1}
    return min(range(len(sections)), key=lambda i: rank.get(sections[i].outcome, 2))
