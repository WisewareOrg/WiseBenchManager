# Contributing to WiseBenchManager

The human contributor entry point: how a change gets merged. **What WiseBenchManager is** is the
[README](README.md); working rules for an AI agent are in [`CLAUDE.md`](CLAUDE.md).

## Before you build anything

A design decision is an ADR before it is code, and an ADR with no rejected alternative is not one.
Well-supported tools come before code of ours — a bespoke piece carries the sentence saying why no
maintained tool fits, and that sentence has to hold up under review. Every check is proven on a fake
board with a test that fails when the check is removed, and once on the real board. Outcomes are two
things only: a failing hook or test is the candidate's and is posted; an error in a fixture or the
precondition is the bench's own, and nothing is posted. See [`CLAUDE.md`](CLAUDE.md) §
"How it is built" and § "How decisions are made".

## Tickets, branches, and titles

Open an issue first: the branch name is derived from it. Ticket, branch-naming, linkage and
PR-title rules — including the ticket types (PROC-010) — are stated once in
[WisewareOrg/.github's `PROCESS.md`](https://github.com/WisewareOrg/.github/blob/main/PROCESS.md),
not restated here.

Enforced by the `check-branch` and `pr-title` CI checks.

## Getting a change merged

Branch and PR; the maintainer merges. The repository squash-merges: the PR title becomes the commit
subject on `main`, and the branch's commit messages become the body. The PR body carries a reading
guide for any diff over a screen: what it does, the stops in reading order, what to skip, where the
risk sits.

Review is the `/pr-ready` flow: three reviewers (content, documentation discipline, test quality) in
fresh contexts, findings to the implementer, escalations to the maintainer; the PR is not called
ready before they have closed.

A Renovate pull request for a `minor`, `patch`, `pin`, `pinDigest`, `digest` or `lockFileMaintenance`
update automerges on green; a `major` update waits for review. See
[wise-renovate's README](https://github.com/WisewareOrg/wise-renovate/blob/main/README.md) §
"Policy (the shared preset, `default.json`)".

## Review checklist

Each question is an obligation on the author that leaves no artifact, so no check decides it — the
reviewer is the mechanism.

**Cite a question by number *and* name** — `question 6, *Every check watched failing*`. A bare
number resolves silently to whatever occupies it after a renumber, in documents no sweep reliably
reaches. New questions are appended for the same reason; inserting one is permitted and renumbers
everything below.

1. **No application knowledge.** Does the change add a unit name, board path, page state, probe,
   replay set or config key that belongs to a consumer? ([`CLAUDE.md`](CLAUDE.md) § "What this
   repository is not allowed to know")
2. **No consumer reference.** Does the change add a pin, import, URL or path to a layer repository?
   ([`CLAUDE.md`](CLAUDE.md) § "What this repository is not allowed to know")
3. **No state between runs.** Does the change carry anything from one run to the next beyond the
   DISABLED marker? ([`CLAUDE.md`](CLAUDE.md) § "What this repository is not allowed to know")
4. **Small classes, shell as glue.** Is new logic a small Python class with one job, with any shell
   reduced to glue rather than read as logic? ([`CLAUDE.md`](CLAUDE.md) § "How it is built")
5. **The why-no-maintained-tool sentence.** Where the change adds a bespoke piece instead of a
   well-supported tool, does it carry the sentence saying why no maintained tool fits, and does that
   sentence hold up under review? ([`CLAUDE.md`](CLAUDE.md) § "How it is built")
6. **Every check watched failing.** Has a new fixture or runner step been proven on a fake board,
   with a test that fails when the check is removed, and once on the real board?
   ([`CLAUDE.md`](CLAUDE.md) § "How it is built")
7. **No new provenance bypass.** Does the change add another way to skip the build, beyond the one
   named exception? ([`CLAUDE.md`](CLAUDE.md) § "How it is built")
8. **Two outcomes only.** Does the change keep outcomes to exactly two — the candidate's failure
   posted, or the bench's own error leading to DISABLED with nothing posted — rather than inventing
   a third? ([`CLAUDE.md`](CLAUDE.md) § "How it is built")
9. **An ADR with a rejected alternative before code.** Does a design decision in the change have an
   ADR, and does that ADR carry a rejected alternative? ([`CLAUDE.md`](CLAUDE.md) § "How decisions
   are made")
10. **Estimates labelled.** Is a size the change derives labelled as an estimate, with no budget
    implied unless one is stated explicitly? ([`CLAUDE.md`](CLAUDE.md) § "How decisions are made")
11. **A reading guide for long diffs.** Where the diff is over a screen, does the PR body carry a
    reading guide — what it does, the stops in reading order, what to skip, where the risk sits?
    ([`CLAUDE.md`](CLAUDE.md) § "How changes land")
12. **Comments as mechanism only.** Does each comment the change adds state mechanism, one or two
    lines, with no history, no issue numbers and no temporal words? ([`CLAUDE.md`](CLAUDE.md) §
    "How changes land")
