# 0002 — Replay recorded traffic with mitmproxy's own server replay, a miss failing the test

**Status:** accepted
**Decided:** 2026-10-15 (WisewareOrg/WiseBenchManager#5 replay on mitmproxy; WisewareOrg/WiseBenchManager#20 the proxy reports the requests it served)
**Rev:** 1

## Revisions

- **rev 1** — 2026-10-29 — first written (WisewareOrg/WiseBenchManager PR #31 ADR 0001 as trade studies).

## Context

Per-PR gating and repeatable performance numbers are both required of the bench, and neither holds
if the board's application sees different upstream responses from one run to the next. So some
target-suite checks run with the board's upstreams replayed from a recording: over TLS, with no
upstream reachable, and with the application under test unchanged — a check may not edit the thing
it checks to make it testable.

Replay then raises a question of its own: **what happens when the board asks for something the
recording does not hold.** A miss is either evidence about the candidate or noise from the bench,
and the runner has exactly two outcomes — a failure is the candidate's and is posted, or the bench's
and is not. What it is held to:

- A maintained tool before code of our own.
- Replay never dials a real upstream, and a request outside the recording is visible to the check.
- A record of what was served and what was refused, in the tool's own format.
- The application under test is not modified.
- A miss lands in one of the two outcomes, never a third.

## Decision

**`mitmdump` server replay**, from a HAR file the layer hands the `proxy` fixture, on a loopback
port forwarded to the board.

- A miss kills the connection (`server_replay_extra=kill`); upstream certificates are never fetched
  (`upstream_cert=false`) and connections are made lazily, so replay needs no reachable upstream.
- mitmproxy generates the CA; installing it on the board is the layer's precondition.
- **mitmproxy's own output is the record of what it served:** its per-flow log, its warning on a
  killed request, and the flow file it saves, read back with its own `FlowReader`. No addon of this
  runner's own runs inside mitmproxy.
- What a recording keeps or drops is decided when it is recorded, by whoever records it.
- **A miss is the candidate's.** The fixture reports it through `killed()` beside `served()`, for a
  check to fail on, and records both counts; the session goes on to its other checks.

## Alternatives considered

- **A TLS replay proxy of our own.** It already existed and worked, needed only the standard library,
  answered a miss with a `502` the application could see, and wrote exactly the log lines its checks
  read. Rejected: a few hundred lines plus tests plus a yearly leaf-certificate re-mint, to do what
  mitmproxy does with a handful of options and a HAR file — and the "standard library only"
  preference it was built on contradicts the preference for maintained tools.
- **Hoverfly.** A maintained Go capture-and-simulate proxy, and the survey's viable second choice.
  Rejected for a middleware model less suited to the scrub addon planned at the time. With no addon
  (below) that reason does not apply, and mitmproxy keeps the lead on its native HAR support alone.
- **WireMock.** Forward proxying with a CA, and record and playback both. Rejected: pass-through to
  the real host is its default, so a miss dials out unless configured otherwise; it needs a JVM; its
  4.0 line is a beta.
- **vcrpy, go-vcr.** The simplest record-and-replay there is: no proxy, no CA. Rejected: they patch
  the client's HTTP stack in-process, which means editing the application under test.
- **mitmproxy with an addon of ours**, writing `SERVE`/`HIT`/`MISS` lines and stripping a configured
  header list when recording. One log grammar, scrubbed recordings for free. Rejected: mitmproxy's
  flow log, its killed-request warning and its saved flow file already say what was served and what
  missed, and what a recording drops is the recorder's call, made with mitmproxy's own options.
- **A miss voids the run as the bench's failure** (the fixture ends the session at teardown). No
  check has to remember to assert, and no miss passes silently. Rejected: a request outside the
  recording is something the candidate did, so it is the candidate's failure; voiding the run hides
  every other check's result behind it; and a fixture that reports only misses cannot tell "every
  request was served" from "nothing reached the proxy", since both log no kill.

## Consequences

- **A miss fails only the check that asserts on it.** One no check examines fails nothing; its count
  is still in the record, so it is visible after the fact but not gating.
- **Strictness is configuration, not mitmproxy's default.** `server_replay_extra` defaults to
  `forward`, which dials the real host on a miss; the fixture sets `kill` and turns `upstream_cert`
  off, and those two settings are what make a replay a replay.
- **A miss is a killed connection, not an HTTP error.** mitmproxy cannot answer `502`; the
  application sees a dropped connection, which is a different code path from an error response.
- **Recordings are made by hand**, the one time a proxy reaches a real upstream, and a recording is a
  HAR file the record hashes.
- **The board must trust mitmproxy's CA**, and getting it there is the layer's job.

**Premise that would reopen this:** mitmproxy ceasing to be maintained, or a check that needs a miss
answered with something a killed connection or mitmproxy's `404`/`500` cannot stand in for. Hoverfly
is the second choice already weighed. Preferring another proxy's interface is not that premise.
