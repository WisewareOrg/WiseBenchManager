"""View rules: what the watch view shows, as plain functions of the run model and the queue.

Which stage shows, which sections start open, where a log's cursor starts and stays, how the
running run merges into the queue, what `:run` previews, and the text and colour of every row.
Nothing here draws; `status` puts these lines on the screen.
"""

import re
from bisect import bisect_right
from datetime import datetime

from rich.style import Style
from rich.text import Text

from wisebench.bench_state import DASH
from wisebench.runlog import KAS_STAMP

GROUND, TEXT, DIM, LOG, RULE = "#0c0c0c", "#cccccc", "#8b8b8b", "#a8a8a8", "#3a3a3a"
BRIGHT, GREEN, AMBER, RED, BLUE = "#ffffff", "#98c379", "#e5c07b", "#e06c75", "#61afef"
RUNNING_TINT, FAILED_TINT, SELECTION = "#2a2614", "#3a1d20", "#1f2a36"

TAIL = "tail"
QUEUE = (("", 3), ("source", 14), ("what", 32), ("sha", 9), ("added", 8), ("", 24))
HISTORY = (
    ("record", 18), ("what", 24), ("sha", 9), ("result", 13), ("took", 6), ("", 14), ("", 10)
)  # fmt: skip
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
_LOG_PREFIX = re.compile(KAS_STAMP + r"[A-Z]+\s+- ")
_CONTROLS = {c: "^" + chr(c ^ 64) for c in [*range(32), 127]} | {
    c: f"<{c:02x}>" for c in range(0x80, 0xA0)
}


def line(*parts):
    """One line of text in colour, drawn safely whatever the text holds.

    Tabs are expanded and every C0, DEL and C1 control character is replaced by a visible form,
    so no text from a log, GitHub or an exception can act on the terminal. No markup is read.

    Args:
        *parts (tuple): `(text, colour)` or `(text, colour, bold)`; a colour of None is the
            text colour.

    Returns:
        rich.text.Text: The line, cut with an ellipsis rather than wrapped.
    """
    out = Text(no_wrap=True, overflow="ellipsis", end="")
    for text, colour, *bold in parts:
        safe = str(text).expandtabs(8).translate(_CONTROLS)
        out.append(safe, Style(color=colour, bold=bool(bold and bold[0])))
    return out


def lines(*rows):
    """Lines of text one under another.

    Args:
        *rows (rich.text.Text): The lines.

    Returns:
        rich.text.Text: The lines joined by newlines.
    """
    return Text("\n", no_wrap=True, overflow="ellipsis").join(rows)


def clock(moment):
    """A moment as the screen shows it.

    Args:
        moment (datetime | None): The moment.

    Returns:
        str: `HH:MM:SS` in local time, or a dash for None.
    """
    return moment.astimezone().strftime("%H:%M:%S") if moment else DASH


def took(started, ended, running, now):
    """How long a stage, section or task took.

    Args:
        started (datetime | None): Its start.
        ended (datetime | None): Its end.
        running (bool): It is running now.
        now (datetime): The current time.

    Returns:
        str: `MmSSs` from start to end, or to now while running; a dash otherwise.
    """
    end = ended or (now if running else None)
    if started is None or end is None:
        return DASH
    seconds = int((end - started).total_seconds())
    return f"{seconds // 60}m{seconds % 60:02d}s"


def run_took(run, running, now):
    """How long a run took, in whole minutes.

    Args:
        run (Run): The run.
        running (bool): The bench is running it.
        now (datetime): The current time.

    Returns:
        str: `Nm` from its start to its end, or to now while running; a dash otherwise.
    """
    end = now if running else run.ended
    if run.started is None or end is None:
        return DASH
    return f"{int((end - run.started).total_seconds()) // 60}m"


def result(run, running):
    """A run's result as a word in its colour.

    Args:
        run (Run): The run.
        running (bool): The bench is running it.

    Returns:
        tuple[str, str]: `running` in amber, else `passed`, `failed`, `bench error` or
        `no result`, green for a pass, red for a failure or error, dim otherwise.
    """
    if running:
        return "running", AMBER
    word = {"error": "bench error", None: "no result"}.get(run.result, run.result)
    return word, {"passed": GREEN, "failed": RED, "error": RED}.get(run.result, DIM)


def name_of(stage):
    """A stage's name as the screen shows it.

    Args:
        stage (Stage): The stage.

    Returns:
        str: The name with underscores as spaces: `host_suite` reads `host suite`.
    """
    return stage.name.replace("_", " ")


def sha7(run):
    """The commit a run checked out, short.

    Args:
        run (Run): The run.

    Returns:
        str: The first seven characters of the checkout stage's sha, or a dash.
    """
    sha = run.stages[0].sha
    return sha[:7] if sha else DASH


def what(run):
    """What a run tested.

    Args:
        run (Run): The run.

    Returns:
        str: `PR #n` for a PR, else its ref.
    """
    return f"PR #{run.pr}" if run.pr is not None else run.ref or ""


def tone(state, running):
    """The glyph, colour and weight of a stage's or a section's state.

    Args:
        state (str): A stage state or a section outcome.
        running (bool): Its stage is open in the live run; an open one is then running.

    Returns:
        tuple[str, str, bool]: Green tick for passed, bold red cross for failed or error, bold
        amber dot while running, dim `?` for one left open, dim dash or dot otherwise.
    """
    if state == "passed":
        return "✓", GREEN, False
    if state in ("failed", "error"):
        return "✗", RED, True
    if state == "open":
        return ("●", AMBER, True) if running else ("?", DIM, False)
    return ("–" if state == "skipped" else "·"), DIM, False


def stopped_at(run):
    """Where a run stopped.

    Args:
        run (Run): The run.

    Returns:
        Stage | None: The first stage that failed, errored or was left open.
    """
    return next((s for s in run.stages if s.state in ("failed", "error", "open")), None)


def auto_stage(run, newest, bench):
    """The stage a run shows until the user picks one.

    Args:
        run (Run): The run shown.
        newest (bool): It is the newest run.
        bench (Bench): The bench's state.

    Returns:
        Stage | None: The newest run's open stage while it is live; None (its summary) while the
        bench is idle; otherwise where the run stopped.
    """
    if newest and bench.live:
        return next((s for s in run.stages if s.state == "open"), None)
    return None if newest and not bench.disabled else stopped_at(run)


def starts_open(section):
    """Whether a section's body shows before the user folds it.

    Args:
        section (Section): The section.

    Returns:
        bool: True while it runs or once it failed.
    """
    return section.outcome in ("open", "failed")


def section_ends(stage):
    """When each section of a stage ended.

    Args:
        stage (Stage): The stage.

    Returns:
        list[datetime | None]: Per section, the next started section's start, else the stage's
        end.
    """
    ends, following = [], stage.ended
    for section in reversed(stage.sections):
        ends.append(following)
        following = section.started or following
    return ends[::-1]


def own_line(text):
    """A stage's own output line as the log shows it.

    Args:
        text (str): The line as logged.

    Returns:
        str: The line without kas's timestamp and level prefix.
    """
    return _LOG_PREFIX.sub("", text)


class LogRows:
    """A stage's log rows: its own lines, then each section's header and, unfolded, its body.

    Rows are made only when asked for, so a log of any length costs what is drawn. A row's key
    is `(section id or None, row within its block)`, a section's header being its row 0; the key
    `TAIL` is the last row.

    Args:
        stage (Stage): The stage.
        folds (dict): Section id to True (unfolded) or False (folded), the user's choices.
        running (bool): The stage is open in the live run.
        now (datetime): The current time.
    """

    def __init__(self, stage, folds, running, now):
        """Lay out the stage's blocks: its own lines, then one block per section.

        Args:
            stage (Stage): The stage.
            folds (dict): Section id to True (unfolded) or False (folded).
            running (bool): The stage is open in the live run.
            now (datetime): The current time.
        """
        self.stage, self.running, self.now = stage, running, now
        self.blocks = [(None, False, None, len(stage.lines))]
        for section, end in zip(stage.sections, section_ends(stage), strict=True):
            opened = folds.get(section.id, starts_open(section))
            size = len(section.lines) + len(section.failure)
            self.blocks.append((section, opened, end, 1 + size * opened))
        self.starts = [0]
        for *_, count in self.blocks:
            self.starts.append(self.starts[-1] + count)

    def __len__(self):
        """The number of rows.

        Returns:
            int: The rows of every block.
        """
        return self.starts[-1]

    def at(self, index):
        """The block a row is in.

        Args:
            index (int): The row.

        Returns:
            tuple: The block `(section or None, unfolded, end, rows)` and the row within it.
        """
        block = min(bisect_right(self.starts, index), len(self.blocks)) - 1
        return self.blocks[block], index - self.starts[block]

    def __getitem__(self, index):
        """A row's line: an own line, a section header, or an indented body line.

        Args:
            index (int): The row.

        Returns:
            rich.text.Text: The line.
        """
        (section, opened, end, _), offset = self.at(index)
        if section is None:
            return line((own_line(self.stage.lines[offset]), LOG))
        if offset == 0:
            return section_head(section, opened, end, self.running, self.now)
        lines, failure = section.lines, section.failure
        shown = lines[offset - 1] if offset <= len(lines) else failure[offset - 1 - len(lines)]
        return line(("      " + shown, LOG))

    def key(self, index):
        """The key that keeps the cursor on a row as the log grows.

        Args:
            index (int): The row.

        Returns:
            tuple: `(section id or None, row within its block)`.
        """
        (section, *_), offset = self.at(index)
        return (section and section.id, offset)

    def index(self, key):
        """Where a key's row is now.

        Args:
            key (tuple | str | None): A row's key, `TAIL`, or None for where the cursor starts:
                the first failed section's header, else the running one's, else the first row;
                a stage without sections starts on its last row.

        Returns:
            int: The row; a key in a folded body gives its section's header.
        """
        sections = self.stage.sections
        if key is None:
            first = next((s for s in sections if s.outcome == "failed"), None)
            first = first or next((s for s in sections if s.outcome == "open"), None)
            key = (first.id, 0) if first else (None, 0) if sections else TAIL
        if key == TAIL:
            return len(self) - 1
        for (section, _, _, count), start in zip(self.blocks, self.starts, strict=False):
            if (section and section.id) == key[0] and count:
                return start + min(key[1], count - 1)
        return 0

    def find(self, pattern, key, direction):
        """The next row holding a pattern, folded bodies included, round from the end.

        Args:
            pattern (str): The text to find.
            key (tuple): The key of the row to start after.
            direction (int): 1 forward, -1 backward.

        Returns:
            tuple | None: The matching row's key, or None when no row holds the pattern.
        """
        rows = [((None, i), own_line(text)) for i, text in enumerate(self.stage.lines)]
        for section in self.stage.sections:
            rows.append(((section.id, 0), section.name))
            body = (*section.lines, *section.failure)
            rows += [((section.id, i), text) for i, text in enumerate(body, 1)]
        keys = [found for found, _ in rows]
        here = keys.index(key) if key in keys else 0
        for step in range(1, len(rows) + 1):
            found, text = rows[(here + direction * step) % len(rows)]
            if pattern in text:
                return found
        return None


def section_head(section, opened, end, running, now):
    """A section's header in the log.

    Args:
        section (Section): The section.
        opened (bool): Its body shows.
        end (datetime | None): When it ended, as `section_ends` gives it.
        running (bool): Its stage is open in the live run.
        now (datetime): The current time.

    Returns:
        rich.text.Text: The fold glyph, the glyph and name in the outcome's colour (a setup's
        `setup` in blue), then how long it took.
    """
    fold = ("▾ " if opened else "▸ ") if section.lines or section.failure else "  "
    glyph, colour, bold = tone(section.outcome, running)
    parts = [(fold, DIM), (f"{glyph} ", colour, bold)]
    if section.kind == "setup":
        parts += [("setup", BLUE, bold), (" ", colour, bold)]
    parts.append((section.name, colour, bold))
    if section.started is not None:
        live = running and section.outcome == "open"
        parts.append((f"  {took(section.started, end, live, now)}", DIM))
    return line(*parts)


def stage_row(stage, running, now):
    """A row of the stage list.

    Args:
        stage (Stage): The stage.
        running (bool): It is open in the live run.
        now (datetime): The current time.

    Returns:
        rich.text.Text: Glyph, name, start and took, all in the stage's colour.
    """
    glyph, colour, bold = tone(stage.state, running)
    text = f"{glyph} {name_of(stage)}".ljust(17)
    if stage.started is not None and stage.state not in ("not-started", "skipped"):
        text += clock(stage.started).ljust(10) + took(stage.started, stage.ended, running, now)
    return line((text, colour, bold))


def run_header(run, newest, bench):
    """The Run view's header line.

    Args:
        run (Run): The run shown.
        newest (bool): It is the newest run.
        bench (Bench): The bench's state.

    Returns:
        rich.text.Text: What it tested, its sha and record; `last run` first while the bench is
        idle, and its result and end when it is a past run.
    """
    parts = [(what(run), BRIGHT, True), (f"  {sha7(run)}  record {run.record}", DIM)]
    if not newest:
        word, colour = result(run, False)
        parts += [("  ", None), (word, colour, True), (f"  ended {clock(run.ended)}", DIM)]
    elif not bench.live and not bench.disabled:
        parts.insert(0, ("last run  ", DIM))
    return line(*parts)


def head(run, stage, live, now):
    """The head of the Run view's pane, the lines over the stage's log.

    Args:
        run (Run): The run shown.
        stage (Stage | None): The stage shown; None is the run's summary.
        live (bool): The bench is running this run.
        now (datetime): The current time.

    Returns:
        list[rich.text.Text]: The summary (result, end, length, each suite's tally); a suite's
        tally and the keys of its log; a build's progress line and bar, its running tasks while
        it runs, then the output label; a plain stage's output label.
    """

    def tally(suite):
        """Count a suite's tests.

        Args:
            suite (Stage): The suite.

        Returns:
            tuple[int, int, int, int]: Started, queued, passed and failed.
        """
        tests = [s.outcome for s in suite.sections if s.kind == "test"]
        started = sum(1 for o in tests if o != "not-started")
        return started, len(tests), tests.count("passed"), tests.count("failed")

    if stage is None:
        word, colour = result(run, live)
        ended = "" if live else f"  ended {clock(run.ended)},"
        rows = [line((word, colour, True), (f"{ended} took {run_took(run, live, now)}", DIM))]
        for suite in run.stages[4:6]:
            _, queued, passed, failed = tally(suite)
            if queued:
                counts = f"{passed} passed" + (f"  {failed} failed" if failed else "")
                rows.append(line((f"{name_of(suite):<14}", DIM), (counts, None)))
        return rows
    running = live and stage.state == "open"
    _, colour, _ = tone(stage.state, running)
    if stage.sections:
        done, queued, passed, failed = tally(stage)
        return [
            line(
                (name_of(stage), colour, True),
                (f"  {done} of {queued}  ", None),
                (f"{passed} passed", GREEN),
                (f"  {failed} failed", RED if failed else DIM),
            ),
            line(("Enter into the log   za fold   Esc back to stages", DIM)),
            Text(),
        ]
    rows = []
    if stage.progress:
        progress = stage.progress
        text, filled = f"  {progress.phase}", 0
        if progress.total:
            percent = progress.done * 100 // progress.total
            text += f" {progress.done} of {progress.total}  {percent}%"
            filled = round(33 * progress.done / progress.total)
        rows += [
            line(("build", colour, True), (text, None)),
            line(("█" * filled, colour), ("█" * (33 - filled), RULE)),
            Text(),
        ]
        tasks = sorted(progress.tasks, key=lambda t: (t.started is None, t.started or now))
        if running and tasks:
            recipe, task = (max(len(getattr(t, f)) for t in tasks) + 2 for f in ("recipe", "task"))
            rows.append(line(("running now", DIM)))
            for t in tasks[:8]:
                elapsed = took(t.started, None, True, now)
                rows.append(line((f"{t.recipe:<{recipe}}{t.task:<{task}}{elapsed}", None)))
            rows.append(Text())
    return [*rows, line(("this stage's output", DIM))]


def disabled(text):
    """What a `DISABLED` file says.

    Args:
        text (str): The file: its UTC time on the first line, its reason on the second.

    Returns:
        tuple[str, datetime | None]: The reason and the time; a blank reason and None when the
        time does not parse.
    """
    stamp, _, reason = text.partition("\n")
    try:
        return reason.strip(), datetime.fromisoformat(stamp.strip())
    except ValueError:
        return "", None


def failure_box(reason, at, run):
    """The bench-failure box the Run view shows while the bench is disabled.

    Args:
        reason (str): The `DISABLED` file's reason.
        at (datetime | None): The `DISABLED` file's time.
        run (Run): The newest run.

    Returns:
        rich.text.Text: The heading, the reason, and where and when the bench stopped: the last
        failed or open setup of the stage the run stopped in, and the file's time in local time.
    """
    stage = stopped_at(run)
    setups = [
        s.name for s in (stage.sections if stage else ())
        if s.kind == "setup" and s.outcome in ("failed", "open")
    ]  # fmt: skip
    where = [f"in setup {setups[-1]}"] if setups else []
    when = [f"from the DISABLED file, {clock(at)}"] if at else []
    return lines(
        line(("bench failure, not the candidate's", RED, True)),
        line((reason, None)),
        line(("; ".join(where + when), DIM)),
    )


def queue_rows(runs, bench, entries):
    """The Queue view's rows: the running run, the hand-queued runs, then the merge queue.

    The running run takes its title from the merge-queue entry with its PR, and the entry whose
    sha the running run checked out is left out.

    Args:
        runs (list[Run]): Every run, newest first.
        bench (Bench): The bench's state.
        entries (list[dict]): The last good `Queue.entries()` read.

    Returns:
        list[tuple]: Per row, its key (`running`, `hand <id>` or `merge <pr>`), its hand entry's
        id or None, what it runs, and its cells as `(text, colour)` in `QUEUE`'s columns.
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
        cells = (("▶", AMBER), (source, None), (title, None), (sha7(run), None), ("", None))
        rows.append(("running", None, title, (*cells, (stage, AMBER))))
    for entry in entries:
        place, sha = (str(len(rows) + 1), None), ((entry["sha"] or "")[:7], None)
        if entry["kind"] == "test":
            added = datetime.fromisoformat(entry["added"]).astimezone().strftime("%H:%M")
            cells = (place, ("by hand", BLUE), (entry["ref"], None), sha, (added, None))
            rows.append((f"hand {entry['id']}", entry["id"], entry["ref"], cells))
        elif entry["sha"] is None or entry["sha"] != running_sha:
            title = f"PR #{entry['pr']} {entry['title']}"
            cells = (place, ("merge queue", None), (title, None), sha)
            rows.append((f"merge {entry['pr']}", None, title, cells))
    return rows


def table_row(columns, cells):
    """A row of a table whose columns are data.

    Args:
        columns (tuple): `(heading, width)` per column.
        cells (tuple): `(text, colour)` per column; missing trailing cells are left out.

    Returns:
        rich.text.Text: Each cell cut to its width less two and padded to its width.
    """
    parts = zip(cells, columns, strict=False)
    return line(*((text[: width - 2].ljust(width), colour) for (text, colour), (_, width) in parts))


def typed_ref(text):
    """The ref typed after `:run `.

    Args:
        text (str): The command line's text.

    Returns:
        str | None: The ref, stripped; None when the line holds no `:run <ref>`.
    """
    found = re.fullmatch(r":run\s+(.*)", text)
    return (found.group(1).strip() or None) if found else None


def split_run(text):
    """The directory and ref of what `:run` was given.

    Args:
        text (str): What follows `:run `, as `typed_ref` gives it.

    Returns:
        tuple[str | None, str]: The directory after `-C` and the ref after it; None and the text
        as it is when it does not read `-C <dir> <ref>`.
    """
    found = re.fullmatch(r"-C\s+(\S+)\s+(\S.*)", text)
    return (found.group(1), found.group(2)) if found else (None, text)


def preview(ref, outcome, rows):
    """The `:run` preview: what a ref resolves to and where it would join the queue.

    Args:
        ref (str): The ref typed.
        outcome (dict | Exception): `Queue.preview`'s result, or what it raised.
        rows (list[tuple]): The Queue's rows, as `queue_rows` gives them.

    Returns:
        rich.text.Text: The commit and its subject, whether the checkout is clean, and its place:
        after the running run and the hand-queued runs, ahead of the merge queue.
    """
    if isinstance(outcome, Exception):
        return line((f"{ref} does not resolve", RED))
    state = "clean tree" if outcome["clean"] else "uncommitted changes in the checkout"
    ahead = [row for row in rows if row[0] == "running" or row[1] is not None]
    place = f"joins as {len(ahead) + 1}" + (f", after {ahead[-1][2]}" if ahead else "")
    return lines(
        line(("will queue", BLUE, True)),
        line((f'resolves to {outcome["sha"][:7]} "{outcome["subject"]}", {state}', DIM)),
        line((place, DIM)),
    )


def history_cells(run, running, now):
    """A run's cells in History.

    Args:
        run (Run): The run.
        running (bool): The bench is running it.
        now (datetime): The current time.

    Returns:
        tuple: `(text, colour)` in `HISTORY`'s columns: record, what, sha, result, took, the
        stage that failed, and `by hand` for a run queued by hand.
    """
    failing = next((name_of(s) for s in run.stages if s.state in ("failed", "error")), "")
    return (
        (run.record, None),
        (what(run), None),
        (sha7(run), None),
        result(run, running),
        (f"{run_took(run, running, now):>4}", None),
        (failing, DIM),
        ("by hand" if run.kind == "test" else "", BLUE),
    )
