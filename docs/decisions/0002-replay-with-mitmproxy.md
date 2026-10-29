# 0002 — Replay recorded traffic with mitmproxy's own server replay, a miss failing the test

**Status:** accepted
**Decided:** 2026-10-15 (WisewareOrg/WiseBenchManager#5 replay on mitmproxy; WisewareOrg/WiseBenchManager#20 the proxy reports the requests it served)
**Rev:** 1

## Revisions

- **rev 1** — 2026-10-29 — first written.

## Context

Some target-suite tests need the board to see the same upstream responses on every run, over TLS,
with no upstream reachable. What the choice is held to:

- A maintained tool before code of our own.
- Replay never reaches a real upstream, and a request outside the recording is visible to the test.
- A record of what was served and what was refused, without a format of this runner's own.
- A failure is the candidate's or the bench's, never a third thing.

## Decision

`mitmdump` server replay from a HAR file the layer hands the `proxy` fixture. A miss kills the
connection, connections are made lazily and upstream certificates are never fetched, so replay needs
no reachable upstream. mitmproxy generates the CA. mitmproxy's own output and the flow file it saves,
read back with its own `FlowReader`, are the record of what it served; no addon of this runner's own
runs inside mitmproxy. What a recording keeps or drops is decided at recording time, by whoever
records it.

A miss is the candidate's: the fixture reports it beside the requests served, for a test to fail
on, and the session goes on to its other tests.

## Alternatives considered

- **A TLS replay proxy of our own.** Rejected: a few hundred lines plus tests plus yearly leaf
  certificate minting, to do what mitmproxy does with a handful of options and a HAR file. The
  "standard library only" preference it was built on contradicts the preference for maintained
  tools.
- **mitmproxy with an addon of ours** writing `SERVE`/`HIT`/`MISS` lines and stripping a configured
  header list when recording. Rejected: mitmproxy's own flow lines, its killed-request warning and
  its saved flow file already say what was served and what missed, and stripping at record time is
  the recorder's call, made with mitmproxy's own options.
- **A miss voids the run as the bench's failure.** Rejected: a request outside the recording is
  something the candidate did, so it is the candidate's failure, and voiding the run hides every
  other test's result behind it.

## Consequences

- Recordings are made by hand, the one time a proxy reaches a real upstream.
- Installing mitmproxy's CA on the board is the layer's precondition, not the runner's.
- A miss fails only the test that asserts on it; one no test checks fails nothing, though its count
  is in the record.
