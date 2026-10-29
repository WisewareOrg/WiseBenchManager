# Architecture decision records

**Rev:** 1

An ADR captures a decision **with a rejected alternative** — the "why not the other way" is the whole
point of writing one down. A decision with no real alternative considered is a changelog entry and
belongs in a commit message, not here. New ADR: copy [`TEMPLATE.md`](TEMPLATE.md), take the lowest
free number, and add it to the table.

**An ADR is versioned, not frozen.** Merged text is revisable, and a correction is a new rev rather
than a block appended to the old text. The rev is in the ADR's head, with one line per rev in its
*Revisions* section; prior text is in git. Nothing bounds when a rev is permitted — that is a
judgement call.

**A rev that changes what was chosen moves the `Decided` date with it**, because that date is when
the choice was taken, not when the work merged. A rev that changes only how the decision is stated
leaves it.

**A citation pins a rev** — `ADR NNNN rev M`, never bare, and a link to an ADR is titled the same
way. Revving an ADR therefore reaches every document citing it, each of which is then updated or
re-decided rather than left to age silently. A *Revisions* line pins the rev it names deliberately,
and is the one exemption. **Write an illustrative example with `NNNN`, never a live number** —
nothing distinguishes an example from a citation, so a real number in one is held to that ADR's
current rev and breaks when it revs.

**Supersession is expressed through revving.** The replacing ADR lands alongside, and the replaced
one takes a rev whose *Revisions* line records `superseded by ADR NNNN rev M` — wholly, with
`Status:` flipped, or in the named part. A decision simply gone is `deprecated`.

**Numbers are contiguous and reusable.** Where documents merge, the freed numbers return to the pool.
A number identifies a document rather than a moment: a squash commit on `main` naming one cannot be
amended, so a number in git history need not mean what it means here.

**A decision is stated plainly, with no attribution.** Where this document, or a requirement's
`rationale`, records a decision as settled — the choice itself, or the disposition of a rejected
alternative — it is written as the document's own position: no parenthetical marker naming who
decided it, no quote. The prose itself is the ruling; a later reader acts on what is written, not
on whose it was.

| # | Rev | Decided | Decision |
|---|---|---|---|
| [0001](0001-verify-with-a-python-bench-runner.md) | 7 | 2026-10-15 | Verify the appliance with a Python bench runner in its own repository, pytest for every test, mitmproxy for replay |
| [0002](0002-one-log-per-run-and-the-watch-view.md) | 1 | 2026-10-28 | One log per run, and the watch view that reads it |

## Revisions

- **rev 1** — 2026-10-11 — first written, alongside ADR 0001 (WisewareOrg/meta-wisekiosk#223 generic
  Yocto test runner).
