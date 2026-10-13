# 0001 — Verify the appliance with a Python bench runner in its own repository, pytest for every test, mitmproxy for replay

**Status:** accepted
**Decided:** 2026-10-13 (meta-wisekiosk#223 generic Yocto test runner: survey, plan and the resulting decisions; rev 2: WiseBenchManager#2 core, minimal scope; rev 3: WiseBenchManager#4 `bench test`, the rollback and the recovery step are removed; rev 4: WiseBenchManager#5 replay has no addon, mitmproxy's own output is the record; rev 5: WiseBenchManager#18 the runner owns no device procedure, the layer's own target-suite fixture installs the candidate)
**Rev:** 5

## Revisions
- **rev 5** — 2026-10-13 — decision 1 drops the bundle path from the runner's config; decision 2
  gains the `bench_candidate` fixture; decision 6 drops the install sentence. The runner's
  `Ota.install` (labgrid `put`, `rauc install`, reboot) duplicated and drifted from the layer's own
  update procedure (`kiosk-send-direct`, `tools/kiosk-install.sh`, `kiosk-reboot`), crashing twice
  on the bench board's hardware watchdog where the layer's own path never has. `ota.py`,
  `tests/test_ota.py`, the `[bundle]` table and the bundle hash in the environment and run record
  are removed (WisewareOrg/WiseBenchManager#18).
- **rev 4** — 2026-10-12 — decision 3's addon (writing `SERVE`/`HIT`/`MISS` lines and stripping a
  configured header list when recording) is removed: mitmproxy's own output is the record of
  what it served, and what a recording keeps or drops is the recorder's own call at recording
  time, by the documented command (WisewareOrg/WiseBenchManager#5).
- **rev 3** — 2026-10-11 — decision 6's recovery step (the mid-OTA marker, `Ota.recover`) and
  its rollback are both removed: rauc always installs to whichever slot is inactive, so which
  slot the board rests on is not a concern, either before or after a run
  (WisewareOrg/WiseBenchManager#4).
- **rev 2** — 2026-10-11 — the board is labgrid's own pytest plugin and env file, with no fixture of
  the runner's own (decision 2); the record is pytest-reportlog's report, with one run-level file
  beside it (decision 5); the tst-marker and credit sentences are struck. A bespoke ssh transport, a
  fake board, a bespoke record writer, markers and a hostname guard are rejected alternatives: the
  runner uses maintained tools and knows nothing of the appliance
  (WisewareOrg/WiseBenchManager#2 Core, minimal scope).
- **rev 1** — 2026-10-11 — first written (meta-wisekiosk#223 generic Yocto test runner).

## Context

The appliance (the meta-wisekiosk image on the kiosk board) needs two verification commands. `bench run`, on a timer: if the merge queue has an entry, build it, OTA it to the bench board, run the tests on the booted board, roll back, report pass or fail to GitHub, and post nothing if the bench itself broke. `bench test <ref> [--set <name>]`: the same path by hand for investigations, with a replay set named when a run must see the same page every time. Per-PR gating on the real board and repeatable performance numbers are both required.

The first implementation grew inside meta-wisekiosk without a design-space analysis: a 620-line shell driver that also held every test's setup (replay windows, a second boot, probe deployment, mode reads, void paths), a hand-written TLS replay proxy with its own recorder and certificate minting, a 500-line shell self-test of the driver's own text, a 1,000-line log viewer, and device tests under Yocto's oeqa runtime framework. oeqa can run a test but cannot set one up, so every state a test needed became driver code, and the driver changed with every test ticket. Python is read, not shell; well-supported tools are preferred over bespoke machinery, and the runner is built once, not rewritten piecemeal.

## Decision

1. The bench runner, WiseBenchManager, is a generic pytest hardware-in-the-loop runner: a Python package in its own WisewareOrg repository, cloned on the pipeline host and run from its checkout at a tag, pointed at a meta-wisekiosk checkout and a small config (board address and hostname, hooks, GitHub target) at run time. Neither repository pins or imports the other; every run record names the runner's version. The split is deliberate and the runner is meant to be hard to change once right.
2. Every test is pytest, in two suites the config names: `host_suite` runs on the pipeline host against the built artefacts before anything is installed; `target_suite` runs against the booted board. Setup and teardown are fixtures. labgrid's own pytest plugin provides the board: an env file in the layer names the board's address, templated from the environment, and the layer points pytest at it with `--lg-env`; the runner adds no fixture of its own for the board itself. Everything that names this appliance — its env file, its units, its config, its page state, its probe, its buildinfo stamp — lives in the layer's own `conftest.py` and tests. The runner's own pytest plugin adds one more fixture beyond `proxy`: `bench_candidate`, the commit a normal run just built, `None` outside the runner; a layer's own target-suite fixture reads it to install that commit on the board through its own update procedure, the runner itself never touching the board.
3. Replay uses mitmproxy: server replay from a recorded HAR file, a miss kills the connection and voids the run as the bench's failure, upstream certificates are never fetched, connections are made lazily so replay needs no reachable upstream, and mitmproxy generates the replay CA with one accept step installing it on the board. mitmproxy's own output — one line per flow, tagged when replayed, and a warning naming a killed non-replay request — is the record of what it served; no addon of this runner's own parses or rewrites it. What a recording keeps or drops is decided at recording time, by the command the runner's README documents. The sets (config, expected page state, recording) are the layer's data; the runner never sees a set directory, only the one HAR path a layer fixture hands it.
4. The layer (meta-wisekiosk) keeps only what is the appliance's: the host suite (artefact checks before install) and the target suite (checks on the booted board) with their parsing helpers, a config file naming its hooks and the two suites, the replay sets as data, the requirement items, `kiosk-preflight` and the OTA recipes for hand use. The oeqa framework, the shell driver, the bespoke proxy, the driver's self-test and the log viewer are deleted at the cut-over.
5. pytest-reportlog's JSON-lines report is the record: every test report, with whatever a layer's fixtures and tests attach through `record_property`. The runner adds one file beside it per run — its own version, the pytest command line, start and end times, the exit status — and nothing else; a verdict posted as a GitHub check run so a merge queue (Mergify) can require it is a later ticket.
6. A failing test is the candidate's failure and is posted; an error in a fixture or a precondition is the bench's failure, writes DISABLED, stops the timer and posts nothing.
7. Scheduling stays with well-supported tools: a systemd user timer and `flock`. Queue-head selection is a small adapter, swapped when the organisation moves the merge queue to Mergify.
8. The operator's view is `bench status --watch`, about 150 lines on a maintained terminal library reading the runner's own event stream, keeping the functionality of the existing TUI and no more.
9. Provenance is structural, not recorded on trust: `bench test <ref>` has no way to skip the build or supply an artefact. It resolves the ref to a commit, refuses a dirty tree, builds from that commit, installs what it built, and the first target-suite test asserts that `/etc/buildinfo` on the booted board names that commit. A run therefore cannot test something it did not build from a ref it can name. Arguments after `--` reach pytest unchanged; the record lists the tests that ran, the requirement items they discharged and the exact command line, so a partial run reads as partial.

## Alternatives considered

- **Keep the shell driver and oeqa.** Rejected: the driver grows with every test's needs because oeqa has no fixtures; its self-test parses its own wording; cases do not assert and verdict objects sit between test and fact; poky marks oeqa's target loader for refactoring; shell is not reviewed here.
- **Keep the shell driver, move the tests to pytest.** Rejected: two languages, and the five steps would be rewritten later anyway; the goal is to do it once.
- **labgrid as the whole runner.** Rejected: its value is places, a coordinator, power and serial drivers and board strategies, none used for one board over ssh, and it has no RAUC strategy, so that code is ours either way; no new hardware is planned. Its own pytest plugin and env file are the board, with no fixture of the runner's own (rev 2).
- **A bespoke ssh transport, a fake board, a bespoke record writer, a `tst` marker and a hostname guard.** Rejected in rev 2: each re-implements what labgrid's own plugin and pytest-reportlog already do, or encodes something the runner is not meant to know.
- **LAVA, KernelCI.** Rejected: a server footprint (database, scheduler, workers) for one board, no OTA deploy action, tests run on the device rather than host-side, verdicts are not GitHub statuses.
- **Fuego, Avocado, pytest-embedded.** Rejected: Fuego is unmaintained since 2020; Avocado has no deploy/boot model; pytest-embedded is scoped to microcontrollers.
- **A self-hosted Actions runner on `merge_group`.** Rejected: a long-lived registered runner on the build host, which organisation policy forbids; flock and the timer already provide the trigger.
- **Our own TLS replay proxy.** Rejected: 367 lines plus tests plus yearly leaf minting to do what mitmproxy does with four options and a HAR file; the "stdlib only" preference it was built on contradicts the preference for well-supported tools.
- **An oeqa custom test target.** Rejected: the most Yocto-native route, but it fuses runner and layer at the seam this decision separates, and poky marks the loader for refactoring. No QEMU tier will exist, so oeqa's QEMU integration carries no weight.
- **`uv tool install` / pipx.** Not needed: the host clones the repository and runs it at a tag; the record names the tag.
- **Keep `Ota.install` in the runner.** Rejected in rev 5: it duplicated the layer's own update procedure and the two drifted, crashing the board's hardware watchdog twice where the layer's own path never has; its tests ran only against a fake driver, so that class of failure could not show there. "labgrid has no RAUC strategy, so that code is ours either way" (rev 1) settled who wrote the code, not who owns the procedure — the layer already had one.

## Consequences

- A new check is a test file and, at most, a fixture beside it; the runner does not change when a test's needs change.
- The runner owns no device procedure of its own; a layer with a different update mechanism needs only its own `bench_candidate`-reading fixture, not a runner change. The run record and the environment carry the commit only, not a bundle hash.
- meta-wisekiosk loses on the order of seven thousand lines (the driver, its self-test, the viewer, the proxy and its tests, the oeqa framework and case shells); the runner adds on the order of a thousand lines of code plus its tests (an estimate).
- Requirement items point at pytest functions; the record names the items discharged, the image commit, the dirty flag, the set hash and the runner version.
- The merge queue is gated on one physical board, so merges serialise at a job's length; this is accepted.
- Reopened if a second board, power control or a serial console arrives (labgrid becomes worth adopting; the tests' shape does not change), or if the organisation's CI policy changes what may run on the build host.
- An investigation's run is as traceable as a gate's: same command path, same record, no bypass.
