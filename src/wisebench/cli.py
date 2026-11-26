"""`bench` command line.

Argument parsing for `bench test`, `unqueue`, `run`, `status`, `on` and `off`, and their exit
mapping.
"""

import argparse
import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path

from wisebench import status
from wisebench.bench_run import BenchRun
from wisebench.bench_state import switch
from wisebench.bench_test import BenchFailure
from wisebench.clone import locate
from wisebench.config import Config, settings_path
from wisebench.lock import Lock
from wisebench.queue import Queue


def _raise_on_sigterm(signum, frame):
    """Turn `SIGTERM` (systemd's `TimeoutStartSec` kill) into a bench failure.

    Args:
        signum (int): Unused.
        frame (types.FrameType | None): Unused.

    Raises:
        BenchFailure: Always.
    """
    raise BenchFailure("terminated: the job exceeded TimeoutStartSec")


def _load_bench_env():
    """Read `~/.config/wisebench/bench.env` into `os.environ`, its values over the caller's.

    Blank lines and `#` lines are skipped; every other line splits at its first `=`, and one pair
    of double quotes around the value is removed.

    Raises:
        BenchFailure: The file cannot be read.
    """
    path = Path.home() / ".config" / "wisebench" / "bench.env"
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise BenchFailure(f"cannot read {path}: {exc}") from exc
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, value = line.strip().partition("=")
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        os.environ[key.strip()] = value


def main(argv=None):
    """Parse arguments and run one `bench` command, on the first board unless it names another.

    `test` and `unqueue` change the queue only, needing neither `LG_ENV` nor the `Lock`; `run`
    holds the `Lock` for the whole of its run; `on` and `off` are `switch`.

    Args:
        argv (list[str] | None): Arguments to parse, defaulting to `sys.argv[1:]`.

    Returns:
        int: 0 queued/removed/switched (or, for `run`, finished/no job/off/disabled/lock held), 2
        a board that is not there, a ref that is not a commit, an id not queued, an unreadable
        queue, or the bench's own failure; for `status`, the watch loop's exit code.

    Raises:
        SystemExit: argparse rejected the arguments, or printed `--help`.
    """
    argv = sys.argv[1:] if argv is None else argv
    if "--" in argv:
        split = argv.index("--")
        own_args, pytest_args = argv[:split], argv[split + 1 :]
    else:
        own_args, pytest_args = argv, []

    parser = argparse.ArgumentParser(prog="bench")
    subparsers = parser.add_subparsers(dest="command", required=True)
    test_parser = subparsers.add_parser("test")
    test_parser.add_argument("ref")
    test_parser.add_argument("--set", dest="set_name", default=None)
    test_parser.add_argument("--skip-build", action="store_true")
    test_parser.add_argument("-C", dest="source", default=None)
    unqueue_parser = subparsers.add_parser("unqueue")
    unqueue_parser.add_argument("entry_id", type=int)
    run_parser = subparsers.add_parser("run")
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--watch", action="store_true", required=True)
    for board_parser in (test_parser, unqueue_parser, run_parser):
        board_parser.add_argument("--board")
    for word in ("on", "off"):
        subparsers.add_parser(word).add_argument("board")
    parser.set_defaults(board=None)
    args = parser.parse_args(own_args)

    try:
        checkout, directory = locate(args.board)
        if args.command in ("on", "off"):
            print(switch(args.board, args.command == "on"))
            return 0
    except (OSError, ValueError) as exc:
        print(f"BENCH ERROR: {exc}", file=sys.stderr)
        return 2
    if args.command == "status":
        return status.watch(checkout, directory)

    if args.command == "test":
        try:
            entry_id, sha, place = Queue(checkout, directory).add(
                args.ref, args.set_name, args.skip_build, pytest_args, args.source
            )
        except subprocess.CalledProcessError:
            print(f"BENCH ERROR: {args.ref} does not resolve to a commit", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"BENCH ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"queued {entry_id} {sha} place {place}")
        return 0

    if args.command == "unqueue":
        try:
            removed = Queue(checkout, directory).remove(args.entry_id)
        except Exception as exc:
            print(f"BENCH ERROR: {exc}", file=sys.stderr)
            return 2
        if not removed:
            print(f"BENCH ERROR: no queued run {args.entry_id}", file=sys.stderr)
            return 2
        print(f"removed {args.entry_id}")
        return 0

    try:
        _load_bench_env()
    except BenchFailure as exc:
        print(f"BENCH ERROR: {exc}", file=sys.stderr)
        return 2
    if args.board is not None:
        for key in [key for key in os.environ if key.startswith("LG_")]:
            del os.environ[key]
        os.environ["LG_ENV"] = str(checkout.parent / "env.yaml")
    lg_env = os.environ.get("LG_ENV")
    if not lg_env:
        print("BENCH ERROR: LG_ENV is not set", file=sys.stderr)
        return 2

    try:
        config = Config.load(checkout / "bench.toml", settings_path())
        bench_run = BenchRun(checkout, config, directory, added=args.board is not None)
    except Exception as exc:
        print(f"BENCH ERROR: {exc}", file=sys.stderr)
        return 2
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(Lock(directory))
        except BenchFailure:
            print("bench run: another run holds the lock")
            return 0
        previous_handler = signal.signal(signal.SIGTERM, _raise_on_sigterm)
        try:
            bench_run.run(lg_env)
        except Exception as exc:
            print(f"BENCH ERROR: {exc}", file=sys.stderr)
            return 2
        finally:
            signal.signal(signal.SIGTERM, previous_handler)
    return 0


if __name__ == "__main__":
    sys.exit(main())
