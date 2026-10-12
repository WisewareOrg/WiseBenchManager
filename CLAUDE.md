# Agent working rules — WiseBenchManager

The global rules in `~/.claude/CLAUDE.md` apply here unchanged. These are the rules layered on top for
this repository, which is a generic pytest hardware-in-the-loop runner and nothing else.

## What this repository is not allowed to know

- **No application knowledge, ever.** No unit name, path on the board, page state, probe, replay set or
  config key of any consumer appears in this code. If a change needs one, it belongs in the consumer's
  `conftest.py` or tests, built on the fixtures here. A name from meta-wisekiosk or WiseKiosk in this
  tree is a defect.
- **No reference to a consumer.** No pin, import, URL or path to a layer repository. The runner is handed
  a checkout and a `bench.toml` at run time; that file and the suites in it are the whole contract.
- **No state between runs.** Nothing is carried from one run to the next except the DISABLED
  marker, which exists to make the next run safe.

## How it is built

- **Python, one language, written to be read.** Small classes with one job each; a `just` recipe or a
  shell line is glue only. Python is read here; shell is not.
- **Well-supported tools before code of ours.** Every piece names what covers it (labgrid for ssh,
  mitmproxy for replay, pytest for the suites and the record, systemd and `flock` for scheduling, `gh`
  for GitHub). A bespoke piece is written only with a sentence saying why no maintained tool fits, and
  that sentence has to hold up under review.
- **Every check is a test that has been watched failing.** A fixture or runner step is proven on a fake
  board (scripted answers) with a test that fails when the check is removed, and once on the real board.
  A check that cannot fail has measured nothing.
- **Provenance is structural.** There is no way to skip the build or hand the runner an artefact.
  `bench test <ref>` refuses a dirty tree and builds from the resolved commit; the first target-suite
  test asserts the booted board names that commit. Do not add a bypass, for any reason, including "the
  build takes a while".
- **Outcomes are two things only.** A failing hook or test is the candidate's and is posted. An error in a
  fixture or the precondition is the bench's: DISABLED, timer stopped, nothing posted. Never invent a
  third class, and never post on the bench's failure.
- **A killed run leaves the board on its candidate slot.** The rollback runs in a `finally`; a run
  killed outright leaves nothing undone, and the next run's own install overwrites whatever is there.
  Which slot the board rests on between runs is not a concern.

## How decisions are made

- **A design decision is an ADR before it is code.** `docs/decisions/` holds them, in the template there;
  an ADR with no rejected alternative is not an ADR. Cite one as `ADR NNNN rev M`, never a bare number.
- **A delegate escalates every ambiguity; it never invents an answer.** A choice settled only by "the
  code already does it this way" is an invention.
- **Estimates are estimates.** A size an agent derives is labelled so; a budget exists only when one is
  stated explicitly.

## How changes land

- Branch and PR; the maintainer merges. The PR body carries a reading guide for any diff over a screen:
  what it does, the stops in reading order, what to skip, where the risk sits.
- Review is the `/pr-ready` flow: three reviewers (content, documentation discipline, test quality) in
  fresh contexts, findings to the implementer, escalations to the maintainer; the PR is not called ready
  before they have closed.
- Comments state mechanism, one or two lines, no history, no issue numbers; the why lives in the ADR or
  the README. No temporal words ("now", "no longer").
- This repository is public. No address, hostname, SSID, MAC, serial or key reaches a tracked file; a
  test whose output carries one is a failing test.
