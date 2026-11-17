"""`status.watch` and its `Shell`: the terminal, the exit codes, and the reads and writes."""

import fcntl
import inspect
import io
import os
import select
import struct
import subprocess
import sys
import termios
import time
from datetime import UTC, datetime, timedelta

import pytest
from helpers import (
    add_settings,
    clone_root,
    has_commit,
    run_unit_settings,
    source_repo,
    start_argv,
    stop_argv,
    stub_merge_queue,
    write_bench_toml,
)
from status_helpers import shown

from wisebench import queue, status

START = datetime(2026, 11, 10, 10, 0, 0, tzinfo=UTC)


def _checkout(tmp_path):
    """The fake clone's layer checkout the `bench_clone` fixture makes under `tmp_path`."""
    return clone_root(tmp_path) / "checkout"


def immediately(work):
    """Run background work at once, on the caller's thread."""
    work()


class Watch:
    """A `status.Shell` over the fake clone, fed keys and ticks as the main loop feeds it.

    It ticks once on being made, as `watch` does before the loop starts. Every key is pressed and
    then ticked, as `status._input` does; `tick` is the one-second alarm with the clock held and
    `wait` the alarm after the clock moves on. `frames` holds every frame shown, in order.
    """

    def __init__(self, tmp_path, run=immediately):
        directory = tmp_path / "records"
        directory.mkdir(exist_ok=True)
        self.now = START
        self.frames = []
        self.shell = status.Shell(_checkout(tmp_path), directory, run=run)
        self.tick()

    def elapsed(self):
        """Seconds the clock has moved on since the watch was made."""
        return (self.now - START).total_seconds()

    def tick(self):
        """The alarm: one refresh at the current time."""
        self.shell.tick(self.now)
        self.frames.append(self.shell.frame)

    def wait(self, seconds):
        """Move the clock on by `seconds`, then refresh."""
        self.now += timedelta(seconds=seconds)
        self.tick()

    def press(self, *keys):
        """Press each key, in urwid's names, refreshing after each."""
        for key in keys:
            self.shell.press(key)
            self.tick()

    def text(self):
        """The text of the frame last shown."""
        return shown(self.shell.frame)


def test_watch_returns_2_and_says_it_needs_a_terminal_when_not_on_one(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert status.watch(_checkout(tmp_path), tmp_path) == 2
    assert capsys.readouterr().err == "BENCH ERROR: bench status --watch needs a terminal\n"


@pytest.mark.parametrize("key", [b"q", b"\x03"], ids=["q", "ctrl-c"])
def test_the_real_shell_on_a_pty_shows_the_first_screen_and_exits_0_with_the_mouse_left_off(
    tmp_path, bench_clone, key
):
    """A real terminal: the first screen, `q` or Ctrl-C exit 0, and no mouse reporting turned on."""
    import pty

    rows, cols = 24, 80
    code = (
        "import sys; from wisebench import status; "
        f"sys.exit(status.watch({str(bench_clone.checkout)!r}, {str(tmp_path)!r}))"
    )
    pid, fd = pty.fork()
    if pid == 0:
        try:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.environ["TERM"] = "xterm-256color"
            os.execv(sys.executable, [sys.executable, "-c", code])
        finally:
            os._exit(127)

    output = b""

    def read_until(needles, seconds):
        """Collect the child's output until every needle appears, it closes, or `seconds` pass."""
        nonlocal output
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not all(n in output for n in needles):
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                output += chunk

    try:
        read_until((b"1 Run", b"0 records"), 5)
        os.write(fd, key)
        read_until((b"\0",), 3)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            done, status_code = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            time.sleep(0.05)
        else:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            pytest.fail(f"the shell did not exit after {key!r}")
    finally:
        os.close(fd)

    assert os.waitstatus_to_exitcode(status_code) == 0
    assert b"1 Run" in output
    assert b"0 records" in output
    assert b"\x1b[?1000h" not in output


# -- the Queue tab through the shell -------------------------------------------------------

HAND = {
    "kind": "test",
    "id": 3,
    "added": "2026-11-10T03:15:00+00:00",
    "ref": "feature-a",
    "sha": "1" * 40,
    "set": None,
    "skip_build": False,
    "args": [],
}
HAND2 = {**HAND, "id": 4, "ref": "feature-b", "sha": "2" * 40}
SHA = "1234567" + "0" * 33


def stub_queue(monkeypatch, clock=None, entries=None, preview=None, add=None, remove=None):
    """Replace the four `Queue` methods the view may call with recorders.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores the methods at teardown.
        clock (Watch | None): When given, each call is recorded with the seconds elapsed.
        entries (callable | None): Returns `entries()`'s value, or raises; default no entries.
        preview (callable | None): Called with the ref; default a clean checkout.
        add (callable | None): Called with `add`'s arguments; default `(5, SHA, 1)`.
        remove (callable | None): Called with the id; default True.

    Returns:
        list[tuple]: Every call, in order: `("entries",)`, `("preview", ref)`,
        `("add", ref, set_name, skip_build, pytest_args)`, `("remove", id)`; with a clock, a
        trailing elapsed-seconds item.
    """
    calls = []

    def record(*call):
        calls.append((*call, clock.elapsed()) if clock else call)

    def read(self):
        record("entries")
        return entries() if entries else []

    def show(self, ref, source=None):
        record("preview", ref)
        if preview:
            return preview(ref)
        return {"sha": SHA, "subject": f"subject of {ref}", "clean": True}

    def queue_up(self, ref, set_name, skip_build, pytest_args, source=None):
        arguments = (ref, set_name, skip_build, pytest_args)
        record("add", *arguments)
        return add(*arguments) if add else (5, SHA, 1)

    def take_out(self, entry_id):
        record("remove", entry_id)
        return remove(entry_id) if remove else True

    monkeypatch.setattr(queue.Queue, "entries", read)
    monkeypatch.setattr(queue.Queue, "preview", show, raising=False)
    monkeypatch.setattr(queue.Queue, "add", queue_up)
    monkeypatch.setattr(queue.Queue, "remove", take_out)
    return calls


def removals(calls):
    """The `remove` calls among `calls`."""
    return [call for call in calls if call[0] == "remove"]


def test_entering_the_queue_reads_it_once_through_a_queue_over_the_clones_checkout(
    monkeypatch, tmp_path, bench_clone
):
    owners = []
    calls = stub_queue(monkeypatch, entries=lambda: [HAND])
    read = queue.Queue.entries
    monkeypatch.setattr(
        queue.Queue,
        "entries",
        lambda self: (owners.append((self.checkout, self.directory)), read(self))[1],
    )
    watch = Watch(tmp_path)
    watch.press("2")
    watch.tick()
    watch.tick()
    assert calls == [("entries",)]
    assert owners == [(bench_clone.checkout, tmp_path / "records")]
    assert "feature-a" in watch.text()


def test_no_read_happens_while_another_tab_shows_however_long_it_stays(monkeypatch, tmp_path):
    calls = stub_queue(monkeypatch)
    watch = Watch(tmp_path)
    watch.tick()
    watch.wait(40)
    watch.tick()
    watch.wait(40)
    assert calls == []


def test_leaving_the_queue_stops_the_reads_and_entering_it_again_reads_again(monkeypatch, tmp_path):
    calls = stub_queue(monkeypatch)
    watch = Watch(tmp_path)
    watch.press("2")
    watch.tick()
    watch.press("1")
    watch.wait(40)
    watch.wait(40)
    watch.tick()
    assert len(calls) == 1
    again = stub_queue(monkeypatch)
    watch = Watch(tmp_path)
    watch.press("2")
    watch.tick()
    watch.press("1", "2")
    watch.tick()
    assert len(again) == 2


def test_shift_tab_and_tab_reach_the_view_as_its_keys_and_go_opposite_ways(monkeypatch, tmp_path):
    calls = stub_queue(monkeypatch)
    watch = Watch(tmp_path)
    watch.press("shift tab", "shift tab")
    watch.tick()
    assert calls == [("entries",)]
    forward = stub_queue(monkeypatch)
    watch = Watch(tmp_path)
    watch.press("tab")
    watch.tick()
    assert forward == [("entries",)]


def test_a_named_key_with_no_mapping_changes_nothing(tmp_path):
    watch = Watch(tmp_path)
    watch.press(*":q", "up")
    assert watch.shell.frame.prompt == ":q"


def test_while_the_queue_shows_it_is_read_every_30_seconds(monkeypatch, tmp_path):
    watch = Watch(tmp_path)
    calls = stub_queue(monkeypatch, clock=watch)
    watch.press("2")
    watch.tick()
    for _ in range(8):
        watch.wait(10)
        watch.tick()
    times = [call[-1] for call in calls]
    assert len(times) == 3
    assert times[0] == 0
    assert all(
        30 <= later - earlier <= 40 for earlier, later in zip(times, times[1:], strict=False)
    )


def test_a_failed_read_shows_the_bench_failure_text_and_the_view_goes_on(monkeypatch, tmp_path):
    from wisebench.bench_test import BenchFailure

    boom = BenchFailure("merge queue read failed")
    stub_queue(monkeypatch, entries=lambda: (_ for _ in ()).throw(boom))
    watch = Watch(tmp_path)
    watch.press("2")
    watch.tick()
    watch.tick()
    assert "merge queue read failed" in watch.text()


def test_a_failed_read_keeps_the_last_good_rows(monkeypatch, tmp_path):
    outcomes = [[HAND], RuntimeError("queue.db is locked")]

    def entries():
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    stub_queue(monkeypatch, entries=entries)
    watch = Watch(tmp_path)
    watch.press("2")
    watch.tick()
    watch.tick()
    watch.wait(31)
    watch.tick()
    watch.tick()
    text = watch.text()
    assert "queue.db is locked" in text and "feature-a" in text


def settle(watch):
    """Let five seconds pass, a refresh each, so every immediate read has landed."""
    for _ in range(5):
        watch.wait(1)


def test_run_previews_the_typed_ref_and_draws_the_result(monkeypatch, tmp_path):
    calls = stub_queue(monkeypatch)
    watch = Watch(tmp_path)
    watch.press("2")
    watch.tick()
    watch.press(*":run feature-c")
    settle(watch)
    assert ("preview", "feature-c") in calls
    text = watch.text()
    assert "will queue" in text and 'resolves to 1234567 "subject of feature-c"' in text


def test_a_preview_of_a_ref_no_longer_typed_is_never_drawn(monkeypatch, tmp_path):
    stub_queue(
        monkeypatch,
        preview=lambda ref: {"sha": SHA, "subject": f"result for {ref}", "clean": True},
    )
    watch = Watch(tmp_path)
    watch.press("2")
    watch.tick()
    watch.press(*":run alpha")
    settle(watch)
    seen = len(watch.frames)
    watch.press(*["backspace"] * 5, *"beta")
    settle(watch)
    before = "\n".join(shown(frame) for frame in watch.frames[:seen])
    after = "\n".join(shown(frame) for frame in watch.frames[seen:])
    assert "result for alpha" in before
    assert "result for alpha" not in after
    assert "result for beta" in after


def test_enter_on_run_queues_the_ref_says_so_and_reads_the_queue_again(monkeypatch, tmp_path):
    calls = stub_queue(monkeypatch, entries=lambda: [HAND])
    watch = Watch(tmp_path)
    watch.press("2")
    watch.tick()
    watch.press(*":run feature-c")
    for _ in range(3):
        watch.wait(1)
    watch.press("enter")
    watch.tick()
    watch.tick()
    names = [call[0] for call in calls]
    assert ("add", "feature-c", None, False, []) in calls
    assert names.count("add") == 1 and names.count("entries") == 2
    assert names.index("add") < len(names) - 1 - names[::-1].index("entries")
    assert "queued 5 1234567" in watch.text()


def test_run_of_an_unresolvable_ref_says_so_and_the_view_goes_on(monkeypatch, tmp_path):
    def refuse(*arguments):
        raise subprocess.CalledProcessError(128, ["git", "rev-parse"])

    stub_queue(monkeypatch, add=refuse)
    watch = Watch(tmp_path)
    watch.press("2", *":run nope", "enter")
    watch.tick()
    assert "nope does not resolve to a commit" in watch.text()


def test_dd_removes_the_hand_row_under_the_cursor_and_reads_the_queue_again(monkeypatch, tmp_path):
    calls = stub_queue(monkeypatch, entries=lambda: [HAND])
    watch = Watch(tmp_path)
    watch.press("2")
    watch.tick()
    watch.tick()
    watch.press("d", "d")
    watch.tick()
    watch.tick()
    assert calls == [("entries",), ("remove", 3), ("entries",)]


def test_dd_removes_the_selected_row_however_far_down_the_queue_it_is(monkeypatch, tmp_path):
    """`dd` acts on the row selected and drawn, past where a 24-row pane once stopped."""
    entries = [{**HAND, "id": i + 1, "ref": f"r{i:02d}"} for i in range(30)]
    calls = stub_queue(monkeypatch, entries=lambda: entries)
    watch = Watch(tmp_path)
    watch.press("2")
    watch.tick()
    watch.press(*"j" * 25)
    assert "r25" in watch.text()
    watch.press("d", "d")
    assert removals(calls) == [("remove", 26)]


def test_a_batch_of_keys_acts_key_by_key_so_jdd_removes_the_row_j_moved_to(monkeypatch, tmp_path):
    calls = stub_queue(monkeypatch, entries=lambda: [HAND, HAND2])
    watch = Watch(tmp_path)
    watch.press("2")
    watch.tick()
    status._input(watch.shell, ["j", "d", "d"], watch.now)
    assert removals(calls) == [("remove", 4)]


def test_a_successful_dd_says_which_run_was_removed(monkeypatch, tmp_path):
    stub_queue(monkeypatch, entries=lambda: [HAND])
    watch = Watch(tmp_path)
    watch.press("2")
    watch.tick()
    watch.tick()
    watch.press("d", "d")
    watch.tick()
    watch.tick()
    assert "removed queued run 3" in watch.text()


def test_dd_on_a_row_already_gone_says_there_is_no_such_run(monkeypatch, tmp_path):
    stub_queue(monkeypatch, entries=lambda: [HAND], remove=lambda entry_id: False)
    watch = Watch(tmp_path)
    watch.press("2")
    watch.tick()
    watch.tick()
    watch.press("d", "d")
    watch.tick()
    watch.tick()
    assert "no queued run 3" in watch.text()


@pytest.mark.parametrize("which", ["add", "remove"])
def test_any_failure_of_add_or_remove_becomes_the_message_and_the_view_goes_on(
    monkeypatch, tmp_path, which
):
    import sqlite3

    def locked(*arguments):
        raise sqlite3.OperationalError("database is locked")

    stub_queue(monkeypatch, entries=lambda: [HAND], **{which: locked})
    watch = Watch(tmp_path)
    watch.press("2")
    watch.tick()
    if which == "add":
        watch.tick()
        watch.press(*":run feature-c", "enter")
    else:
        watch.press("d", "d")
    watch.tick()
    watch.tick()
    assert "database is locked" in watch.text()


def held_work():
    """Background work held until the test releases it.

    Returns the list `run` appends to and a function that runs all of it.
    """
    held = []

    def release():
        while held:
            held.pop(0)()

    return held, release


def test_at_most_one_preview_is_in_flight_and_the_newest_typed_ref_follows_its_result(
    monkeypatch, tmp_path
):
    calls = stub_queue(monkeypatch)
    held, release = held_work()
    watch = Watch(tmp_path, run=held.append)
    watch.press("2")
    watch.tick()
    watch.press(*":run m", *"ain")
    release()
    watch.tick()
    release()
    watch.tick()
    assert [call[1] for call in calls if call[0] == "preview"] == ["m", "main"]


def test_no_second_preview_is_asked_when_the_ref_typed_is_back_to_the_one_requested(
    monkeypatch, tmp_path
):
    calls = stub_queue(monkeypatch)
    held, release = held_work()
    watch = Watch(tmp_path, run=held.append)
    watch.press("2")
    watch.tick()
    watch.press(*":run m", "x", "backspace")
    release()
    watch.tick()
    release()
    watch.tick()
    assert [call[1] for call in calls if call[0] == "preview"] == ["m"]


def test_one_entries_read_is_in_flight_at_a_time_however_long_it_takes(monkeypatch, tmp_path):
    calls = stub_queue(monkeypatch)
    held, release = held_work()
    watch = Watch(tmp_path, run=held.append)
    watch.press("2")
    for _ in range(3):
        watch.wait(31)
    release()
    watch.tick()
    watch.wait(31)
    release()
    watch.tick()
    assert [call for call in calls if call[0] == "entries"] == [("entries",)] * 2


def test_a_failed_read_while_the_command_line_is_open_leaves_no_message_behind_after_esc(
    monkeypatch, tmp_path
):
    stub_queue(monkeypatch, entries=lambda: (_ for _ in ()).throw(RuntimeError("remote is down")))
    watch = Watch(tmp_path)
    watch.press(*":run x")
    watch.tick()
    seen = len(watch.frames)
    watch.press("esc")
    watch.tick()
    after = "\n".join(shown(frame) for frame in watch.frames[seen:])
    assert "remote is down" not in after


def test_a_resize_goes_back_to_urwid_to_lay_out_again_and_never_reaches_the_view(
    monkeypatch, tmp_path
):
    watch = Watch(tmp_path)
    pressed = []
    monkeypatch.setattr(watch.shell, "press", lambda key: pressed.append(key) or False)
    assert status._input(watch.shell, ["window resize"], watch.now) == ["window resize"]
    assert pressed == []


def test_half_page_keys_before_anything_is_rendered_raise_nothing(tmp_path):
    directory = tmp_path / "records"
    directory.mkdir()
    shell = status.Shell(_checkout(tmp_path), directory)
    shell.tick(START)
    shell.press("ctrl d")


def test_the_shell_runs_background_work_through_the_worker_thread_by_default():
    assert inspect.signature(status.Shell).parameters["run"].default is status._background


def test_background_work_runs_in_order_on_one_thread_that_is_not_the_callers():
    import threading

    done, threads, order = threading.Event(), [], []

    def work(name):
        def run():
            threads.append(threading.get_ident())
            order.append(name)
            if name == "last":
                done.set()

        return run

    for name in ("first", "second", "last"):
        status._background(work(name))
    assert done.wait(5), "the worker never ran the work"
    assert order == ["first", "second", "last"]
    assert len(set(threads)) == 1 and threading.get_ident() not in threads


ASK = "Enable the bench?"


def bench_command(tmp_path, *, unit=True, disabled=True, run=immediately):
    """A watch over a records directory with a bench.toml, DISABLED, or neither.

    Returns the Watch and the records directory.
    """
    directory = tmp_path / "records"
    directory.mkdir(exist_ok=True)
    if unit:
        write_bench_toml(_checkout(tmp_path))
        add_settings(run_unit_settings())
    if disabled:
        (directory / "DISABLED").write_text("2026-11-13T00:00:00+00:00\nboom\n")
    return Watch(tmp_path, run=run), directory


def changes(systemctl):
    """The systemctl calls that are not the timer query."""
    return [call for call in systemctl.calls if call[2] != "is-active"]


def test_disable_stops_the_timer_says_so_and_leaves_disabled_alone(tmp_path, systemctl):
    watch, directory = bench_command(tmp_path)
    watch.press(*":disable", "enter")
    watch.tick()
    assert changes(systemctl) == [stop_argv("bench-run")]
    assert "bench disabled" in watch.text()
    assert (directory / "DISABLED").exists()


def test_disable_with_no_disabled_file_writes_none(tmp_path, systemctl):
    watch, directory = bench_command(tmp_path, disabled=False)
    watch.press(*":disable", "enter")
    watch.tick()
    assert changes(systemctl) == [stop_argv("bench-run")]
    assert not (directory / "DISABLED").exists()


def test_enable_then_y_removes_disabled_starts_the_timer_and_says_so(tmp_path, systemctl):
    watch, directory = bench_command(tmp_path)
    watch.press(*":enable", "enter")
    watch.tick()
    watch.press("y")
    watch.tick()
    assert changes(systemctl) == [start_argv("bench-run")]
    assert not (directory / "DISABLED").exists()
    assert "bench enabled" in watch.text()
    assert ASK not in watch.text()


def test_enable_then_y_with_no_disabled_file_still_starts_the_timer(tmp_path, systemctl):
    watch, _ = bench_command(tmp_path, disabled=False)
    watch.press(*":enable", "enter", "y")
    watch.tick()
    assert changes(systemctl) == [start_argv("bench-run")]
    assert "bench enabled" in watch.text()


def test_a_failed_queue_read_landing_while_enable_waits_leaves_the_question_showing(
    monkeypatch, tmp_path, systemctl
):
    stub_queue(monkeypatch, entries=lambda: (_ for _ in ()).throw(RuntimeError("remote is down")))
    held, release = held_work()
    watch, _ = bench_command(tmp_path, disabled=False, run=held.append)
    watch.press("2")
    watch.tick()
    watch.press(*":enable", "enter")
    release()
    watch.tick()
    text = watch.text()
    watch.press("n")
    assert ASK in text
    assert "remote is down" not in text
    assert changes(systemctl) == []


def test_disable_with_no_run_unit_says_so_and_calls_nothing(tmp_path, systemctl):
    watch, directory = bench_command(tmp_path, unit=False)
    watch.press(*":disable", "enter")
    watch.tick()
    assert systemctl.calls == []
    assert "no [run] unit in the bench's settings file" in watch.text()
    assert (directory / "DISABLED").exists()


def test_enable_with_no_run_unit_says_so_and_leaves_disabled_in_place(tmp_path, systemctl):
    watch, directory = bench_command(tmp_path, unit=False)
    watch.press(*":enable", "enter", "y")
    watch.tick()
    assert systemctl.calls == []
    assert "no [run] unit in the bench's settings file" in watch.text()
    assert (directory / "DISABLED").exists()


@pytest.mark.parametrize(
    ("verb", "command", "answer", "text"),
    [
        (
            "stop",
            ":disable",
            dict(returncode=1, stderr="not loaded\n"),
            "systemctl failed: not loaded",
        ),
        ("stop", ":disable", dict(returncode=4), "systemctl failed: exit 4"),
        ("start", ":enable", dict(returncode=1, stderr="no unit\n"), "systemctl failed: no unit"),
    ],
)
def test_a_returned_failure_text_is_shown_verbatim(
    tmp_path, systemctl, verb, command, answer, text
):
    systemctl.answer(verb, **answer)
    watch, _ = bench_command(tmp_path)
    watch.press(*command, "enter", *(["y"] if verb == "start" else []))
    watch.tick()
    shown_text = watch.text()
    assert text in shown_text
    assert "bench disabled" not in shown_text
    assert "bench enabled" not in shown_text


def test_enable_that_cannot_remove_disabled_shows_why_and_does_not_start(tmp_path, systemctl):
    (tmp_path / "records" / "DISABLED").mkdir(parents=True)
    watch, _ = bench_command(tmp_path, disabled=False)
    watch.press(*":enable", "enter", "y")
    watch.tick()
    assert changes(systemctl) == []
    assert "cannot remove DISABLED:" in watch.text()


def watch_source(monkeypatch, tmp_path, bench_clone, dirty_checkout=False, typed_dir=None):
    """Type `:run -C <src> HEAD` over a real `Queue` and let the preview land.

    Returns:
        tuple: The source's tip commit, and the Watch.
    """
    stub_merge_queue(monkeypatch, bench_clone.checkout)
    sha = source_repo(tmp_path / "src")
    if dirty_checkout:
        (bench_clone.checkout / "tracked.txt").write_text("changed\n")
    watch = Watch(tmp_path)
    watch.press("2")
    watch.tick()
    watch.press(*f":run -C {typed_dir or tmp_path / 'src'} HEAD")
    watch.tick()
    watch.tick()
    return sha, watch


def test_the_preview_of_a_directory_reports_the_state_of_the_checkout_not_of_the_directory(
    monkeypatch, tmp_path, bench_clone
):
    """The run builds in the checkout, so its uncommitted change is the one to warn of."""
    _, watch = watch_source(monkeypatch, tmp_path, bench_clone, dirty_checkout=True)
    text = watch.text()
    assert "uncommitted changes in the checkout" in text
    assert "clean tree" not in text


def test_run_with_a_home_relative_directory_previews_and_queues_its_commit(
    monkeypatch, tmp_path, bench_clone
):
    """`~/src` is typed, HOME is `tmp_path`: the preview resolves in `src` and Enter queues it."""
    sha, watch = watch_source(monkeypatch, tmp_path, bench_clone, typed_dir="~/src")
    assert f'resolves to {sha[:7]} "unpushed work"' in watch.text()
    assert not has_commit(bench_clone.checkout, sha)
    watch.press("enter")
    watch.tick()
    watch.tick()
    assert f"queued 1 {sha[:7]}" in watch.text()
    assert has_commit(bench_clone.checkout, sha)
    held = queue.Queue(bench_clone.checkout, tmp_path / "records").entries()
    assert [(e["ref"], e["sha"]) for e in held] == [("HEAD", sha)]
