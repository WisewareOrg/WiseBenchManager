# 0002 — One log per run, written straight to a file

**Status:** accepted
**Decided:** 2026-10-29 (WisewareOrg/WiseBenchManager#27 every run writes its output to a file)
**Rev:** 1

## Revisions

- **rev 1** — 2026-10-29 — first written (#27 every run writes its output to a file).

## Context

The runner keeps no record of a run's own output. Each pytest suite writes its own
`records/<timestamp>/report.jsonl`/`run.json` (ADR 0001 decision 5), but the `precondition`,
`build` and `bundle` hooks write no record at all -- their output reaches a terminal (a hand
`bench test`) or the systemd journal (`bench run`) and nowhere else, even though the epic plan
behind `bench status --watch` (ADR 0001 decision 8) said a run's output would be "streamed and
kept". The watch view needs one place to read a run's whole output, with a line marking where
each stage starts and ends, for a run in progress and for one already finished.

## Decision

Every run -- a hand `bench test`, always; a `bench run` cycle, once it has taken a job -- gets
one file, `records/<stamp>/bench.log`. Every hook and both suites run with stdout and stderr
appended directly into it; nothing of theirs reaches a terminal. The runner appends `bench:`
marker lines to the same file: `run started`, `stage <name> started`, `stage <name> <result>`,
`run <result>`, each with a leading newline only when the file's last byte is not already one, so
a hook's unterminated output never runs into the marker that follows it.

This supersedes ADR 0001 decision 5 in its own text: decision 5's record ("pytest-reportlog's
JSON-lines report ... The runner adds one file beside it per run ... and nothing else") gains a
second file beside `report.jsonl`/`run.json` -- `bench.log`, holding everything the runner's own
hooks and suites print, plus the marker lines above. `report.jsonl`/`run.json` stay the record of
test outcomes, unchanged.

## Alternatives considered

- **`tee` to both a terminal and the file.** Rejected: there is no terminal to watch -- a service
  run has none at all, and a hand run's second destination would sit unread; the eventual reader
  of this output is the watch view, not a human at a live tty.
- **The systemd journal as the record.** Rejected: journal entries rotate and expire outside this
  runner's control, and a hand `bench test` never reaches the journal at all.
- **One file per stage.** Rejected: splits a single run's output across as many files as it has
  stages, with nothing tying their order together; one append-only file keeps "in order, as it
  happened" for free.

## Consequences

- `bench.log` is kept output with marker lines in it, not a new record format of this runner's
  own; `report.jsonl`/`run.json` remain the record pytest-reportlog and the plugin write.
- A later ticket reads this file -- a parser, then the watch view; nothing in this one parses it.
- A hand `bench test`'s hook and suite output no longer reaches the terminal; watching a run live
  means tailing `bench.log`.
