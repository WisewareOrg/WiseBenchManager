# 0002 — Replay recorded traffic with mitmproxy's own server replay and its own output

**Status:** accepted
**Decided:** 2026-10-12 (WisewareOrg/WiseBenchManager#5 replay: `proxy(har)` on mitmproxy)
**Rev:** 1

## Revisions

- **rev 1** — 2026-10-29 — first written.

## Context

Some target-suite tests need the board to see the same upstream responses on every run, over TLS,
with no upstream reachable. What the choice is held to:

- A maintained tool before code of our own.
- Replay never reaches a real upstream; a request outside the recording is visible to the test.
- A record of what was served and what was refused, without a format of this runner's own.

## Decision

`mitmdump` server replay from a HAR file the layer hands the `proxy` fixture. A miss kills the
connection, connections are made lazily and upstream certificates are never fetched, so replay needs
no reachable upstream. mitmproxy generates the CA. mitmproxy's own output and the flow file it saves,
read back with its own `FlowReader`, are the record of what it served; no addon of this runner's own
rewrites or parses its traffic. What a recording keeps or drops is decided at recording time, by
whoever records it.

## Alternatives considered

- **A TLS replay proxy of our own.** Rejected: a few hundred lines plus tests plus yearly leaf
  certificate minting, to do what mitmproxy does with four options and a HAR file. The "standard
  library only" preference it was built on contradicts the preference for maintained tools.
- **mitmproxy with an addon of ours** writing `SERVE`/`HIT`/`MISS` lines and stripping a configured
  header list when recording. Rejected: mitmproxy's own flow lines, its killed-request warning and
  its saved flow file already say what was served and what missed, and stripping at record time is
  the recorder's call, made with mitmproxy's own options.

## Consequences

- Recordings are made by hand, the one time a proxy reaches a real upstream.
- Installing mitmproxy's CA on the board is the layer's precondition, not the runner's.
- The runner sees one HAR path per fixture use and never a replay set's directory.
- The fixture's behaviour rests on mitmproxy option names (`server_replay_extra`,
  `connection_strategy`, `upstream_cert`); a mitmproxy upgrade is a `uv.lock` change and is
  reviewed as one.
