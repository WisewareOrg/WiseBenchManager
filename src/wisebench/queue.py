"""Queue: runs queued by hand in `records/queue.db`, then the merge queue's head entry."""

import json
import re
import sqlite3
import subprocess
from contextlib import closing

from wisebench.bench_test import BenchFailure

_REF_RE = re.compile(r"pr-(\d+)-([0-9a-f]+)")


class Queue:
    """Runs queued by hand, then the GitHub merge queue's head entry, for one checkout.

    Hand entries live in the `queue` table of `<directory>/queue.db`; the merge queue's head is
    read with `git ls-remote` against the checkout. `entries()` defines the run order and `take()`
    claims its first entry; `head()` is swappable for a different queue adapter (e.g. Mergify)
    behind the same signature.
    """

    def __init__(self, checkout, directory):
        """Bind one Queue to a checkout and the directory holding `queue.db`.

        Args:
            checkout (pathlib.Path): The layer's clone `git fetch`, `git ls-remote` and
                `git rev-parse` run in.
            directory (pathlib.Path): The records directory `queue.db` sits in; created on first
                use.
        """
        self.checkout = checkout
        self.directory = directory

    def _db(self):
        """Open a connection to `queue.db`, creating the directory, file and table if absent.

        Returns:
            sqlite3.Connection: In autocommit mode, in WAL mode; the caller closes it.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.directory / "queue.db", timeout=30, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            "CREATE TABLE IF NOT EXISTS queue (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "added TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')), "
            "entry TEXT NOT NULL)"
        )
        return db

    def add(self, ref, set_name, skip_build, pytest_args):
        """Queue a run of `ref` by hand, pinned to the commit `ref` names now.

        Args:
            ref (str): The git ref to run, as typed.
            set_name (str | None): The replay set name, or None.
            skip_build (bool): Whether the run skips `build`, `bundle` and the host suite.
            pytest_args (list[str]): Arguments for the target suite.

        Returns:
            tuple[int, str, int]: The new entry's id, the commit sha `ref` resolved to, and its
            place: the number of hand entries queued up to and including this one.

        Raises:
            subprocess.CalledProcessError: `ref` does not resolve to a commit in the checkout.
        """
        sha = subprocess.run(
            ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
            cwd=self.checkout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=True,
        ).stdout.strip()
        entry = {
            "ref": ref,
            "sha": sha,
            "set": set_name,
            "skip_build": skip_build,
            "args": pytest_args,
        }
        with closing(self._db()) as db:
            entry_id = db.execute(
                "INSERT INTO queue (entry) VALUES (?)", (json.dumps(entry),)
            ).lastrowid
            (place,) = db.execute(
                "SELECT COUNT(*) FROM queue WHERE id <= ?", (entry_id,)
            ).fetchone()
        return entry_id, sha, place

    def entries(self):
        """List what waits, in run order: hand entries oldest first, then the merge queue's head.

        Runs `head()`, so it fetches from `origin`.

        Returns:
            list[dict]: A hand entry is `{kind: "hand", id, added, ref, sha, set, skip_build,
            args}`; the merge head is `{kind: "merge", ref, sha, pr}`.

        Raises:
            BenchFailure: More than one merge-queue ref qualifies.
            subprocess.CalledProcessError: `git fetch`, `git ls-remote` or `git rev-parse` failed.
            sqlite3.DatabaseError: `queue.db` is unreadable.
        """
        with closing(self._db()) as db:
            rows = db.execute("SELECT id, added, entry FROM queue ORDER BY id").fetchall()
        entries = [
            {"kind": "hand", "id": id_, "added": added, **json.loads(entry)}
            for id_, added, entry in rows
        ]
        head = self.head()
        if head is not None:
            entries.append({"kind": "merge", **head})
        return entries

    def remove(self, entry_id):
        """Delete one hand entry; of concurrent callers for the same id exactly one gets True.

        Args:
            entry_id (int): The id `add` returned.

        Returns:
            bool: True if this call deleted the entry; False if it was never queued, was removed,
            or was already taken.
        """
        with closing(self._db()) as db:
            return db.execute("DELETE FROM queue WHERE id = ?", (entry_id,)).rowcount == 1

    def take(self):
        """Claim the first waiting entry: the oldest hand entry, else the merge queue's head.

        A hand entry is deleted when claimed, so it is never returned again whatever its run's
        outcome; one removed in the meantime is skipped.

        Returns:
            dict | None: The claimed entry as `entries()` lists it, or None when nothing waits.

        Raises:
            BenchFailure: More than one merge-queue ref qualifies.
            subprocess.CalledProcessError: `git fetch`, `git ls-remote` or `git rev-parse` failed.
            sqlite3.DatabaseError: `queue.db` is unreadable.
        """
        for entry in self.entries():
            if entry["kind"] == "merge" or self.remove(entry["id"]):
                return entry
        return None

    def head(self):
        """Read the queue entry whose own base is `origin/main`'s current tip.

        Reads `refs/heads/gh-readonly-queue/main/*` and keeps the refs named
        `pr-<PR_NUMBER>-<BASE_SHA>` whose `<BASE_SHA>` equals `origin/main`'s current tip; a
        stale entry (based on an earlier tip) reads as no job, the same as an empty queue.

        Returns:
            dict | None: `{ref, sha, pr}`: the ref name, the ref's own sha (the commit to build,
            from `git ls-remote`'s own column, not the ref name) and the PR number the ref names,
            or None when no ref qualifies.

        Raises:
            BenchFailure: More than one ref qualifies.
            subprocess.CalledProcessError: `git fetch`, `git ls-remote` or `git rev-parse`
                failed.
        """
        subprocess.run(["git", "fetch", "origin"], cwd=self.checkout, check=True)
        main = subprocess.run(
            ["git", "rev-parse", "origin/main"],
            cwd=self.checkout,
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        remote = subprocess.run(
            ["git", "ls-remote", "origin", "refs/heads/gh-readonly-queue/main/*"],
            cwd=self.checkout,
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout

        matches = []
        for line in remote.splitlines():
            if not line:
                continue
            sha, ref = line.split("\t")
            name = ref.rsplit("/", 1)[-1]
            match = _REF_RE.fullmatch(name)
            if match and match.group(2) == main:
                matches.append({"ref": ref, "sha": sha, "pr": int(match.group(1))})

        if not matches:
            return None
        if len(matches) > 1:
            raise BenchFailure("more than one gh-readonly-queue ref based on origin/main")
        return matches[0]
