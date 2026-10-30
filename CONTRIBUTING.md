# Contributing to WiseBenchManager

The human contributor entry point: how a change gets merged. **What WiseBenchManager is** is the
[README](README.md); working rules for an AI agent are in [`CLAUDE.md`](CLAUDE.md).

## What this repository is not allowed to know

**No application knowledge, ever.** No unit name, path on the board, page state, probe, replay set
or config key of any consumer appears in this code. If a change needs one, it belongs in the
consumer's `conftest.py` or tests, built on the fixtures here. A name from meta-wisekiosk or
WiseKiosk in this tree is a defect.

**No reference to a consumer.** No pin, import, URL or path to a layer repository. The runner is
handed a checkout and a `bench.toml` at run time; that file and the suites in it are the whole
contract.

**No state between runs.** Nothing is carried from one run to the next except the DISABLED marker,
which exists to make the next run safe.

## Before you build anything

**A design decision is an ADR before it is code.** [`docs/decisions/`](docs/decisions/README.md)
holds them, in the template there; an ADR with no rejected alternative is not an ADR. Cite one as
`ADR NNNN rev M`, never a bare number.

## How a change is built and tested

**Python, one language, written to be read.** Small classes with one job each; a `just` recipe or a
shell line is glue only. Python is read here; shell is not.

**Well-supported tools before code of ours.** Every piece names what covers it (labgrid for ssh,
mitmproxy for replay, pytest for the suites and the record, systemd and `flock` for scheduling, `gh`
and PyGithub for GitHub). A bespoke piece is written only with a sentence saying why no maintained
tool fits, and that sentence has to hold up under review.

**Every check is a test that has been watched failing.** A fixture or runner step is proven on a
fake board (scripted answers) with a test that fails when the check is removed, and once on the
real board. A check that cannot fail has measured nothing.

**Provenance is structural where the runner controls it, checked where it does not.** `bench test
<ref>` refuses a dirty tree and builds from the resolved commit; the runner names that commit to the
layer through `bench_candidate`, the only channel it uses, so a build is never swapped for an
artefact from outside. The layer's own target-suite setup installs what that build produced, and the
first target-suite test checks that the booted board names that commit -- checked, not structurally
guaranteed. `bench test <ref> --skip-build` is the one named exception: it installs nothing and runs
the target suite against the board as it is, its record says so explicitly, and such a run is never
evidence for a review or an investigation, which need a build. `bench run` never skips the build. Do
not add another bypass, for any reason, including "the build takes a while".

**Outcomes are two things only.** A failing hook or test is the candidate's and is posted. An error
in a fixture or the precondition is the bench's: DISABLED, timer stopped, nothing posted. Never
invent a third class, and never post on the bench's failure.

## Tickets, branches, and titles

Open an issue first: the branch name is derived from it. Ticket, branch-naming, linkage and
PR-title rules — including the ticket types (PROC-010) — are stated once in
[WisewareOrg/.github's `PROCESS.md`](https://github.com/WisewareOrg/.github/blob/main/PROCESS.md),
not restated here.

Enforced by the `check-branch` and `pr-title` CI checks.

## Getting a change merged

Branch and PR; the maintainer merges. The PR body carries a reading guide for any diff over a
screen: what it does, the stops in reading order, what to skip, where the risk sits.

The repository squash-merges, commit title `PR_TITLE` and commit message `COMMIT_MESSAGES`: the PR
title becomes the commit subject on `main`, and the branch's commit messages become the body.

A Renovate pull request for a `minor`, `patch`, `pin`, `pinDigest`, `digest` or `lockFileMaintenance`
update automerges on green; a `major` update waits for review. See
[wise-renovate's README](https://github.com/WisewareOrg/wise-renovate/blob/main/README.md) §
"Policy (the shared preset, `default.json`)".

This repository is public. No address, hostname, SSID, MAC, serial or key reaches a tracked file; a
test whose output carries one is a failing test.

## Review checklist

Review is the `/pr-ready` flow: three reviewers (content, documentation discipline, test quality) in
fresh contexts, findings to the implementer, escalations to the maintainer; the PR is not called
ready before they have closed.

Comments state mechanism, one or two lines, no history, no issue numbers; the why lives in the ADR
or the README. No temporal words ("now", "no longer").

Each question below is an obligation on the author that leaves no artifact, so no check decides it —
the reviewer is the mechanism.

**Cite a question by number *and* name** — `question 6, *Every check watched failing*`. A bare
number resolves silently to whatever occupies it after a renumber, in documents no sweep reliably
reaches. New questions are appended for the same reason; inserting one is permitted and renumbers
everything below.

1. **No application knowledge.** Does the change add a unit name, board path, page state, probe,
   replay set or config key that belongs to a consumer? (§ ["What this repository is not allowed to
   know"](#what-this-repository-is-not-allowed-to-know))
2. **No consumer reference.** Does the change add a pin, import, URL or path to a layer repository?
   (§ ["What this repository is not allowed to know"](#what-this-repository-is-not-allowed-to-know))
3. **No state between runs.** Does the change carry anything from one run to the next beyond the
   DISABLED marker? (§ ["What this repository is not allowed to
   know"](#what-this-repository-is-not-allowed-to-know))
4. **Small classes, shell as glue.** Is new logic a small Python class with one job, with any shell
   reduced to glue rather than read as logic? (§ ["How a change is built and
   tested"](#how-a-change-is-built-and-tested))
5. **The why-no-maintained-tool sentence.** Where the change adds a bespoke piece instead of a
   well-supported tool, does it carry the sentence saying why no maintained tool fits, and does that
   sentence hold up under review? (§ ["How a change is built and
   tested"](#how-a-change-is-built-and-tested))
6. **Every check watched failing.** Has a new fixture or runner step been proven on a fake board,
   with a test that fails when the check is removed, and once on the real board? (§ ["How a change
   is built and tested"](#how-a-change-is-built-and-tested))
7. **No new provenance bypass.** Does the change add another way to skip the build, beyond the one
   named exception? (§ ["How a change is built and tested"](#how-a-change-is-built-and-tested))
8. **Two outcomes only.** Does the change keep outcomes to exactly two — the candidate's failure
   posted, or the bench's own error leading to DISABLED with nothing posted — rather than inventing
   a third? (§ ["How a change is built and tested"](#how-a-change-is-built-and-tested))
9. **An ADR with a rejected alternative before code.** Does a design decision in the change have an
   ADR, and does that ADR carry a rejected alternative? (§ ["Before you build
   anything"](#before-you-build-anything))
10. **A reading guide for long diffs.** Where the diff is over a screen, does the PR body carry a
    reading guide — what it does, the stops in reading order, what to skip, where the risk sits?
    (§ ["Getting a change merged"](#getting-a-change-merged))
11. **Comments as mechanism only.** Does each comment the change adds state mechanism, one or two
    lines, with no history, no issue numbers and no temporal words? (§ ["Review
    checklist"](#review-checklist))
