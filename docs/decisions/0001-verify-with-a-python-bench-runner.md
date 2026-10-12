# 0001 — Verify the appliance with a Python bench runner in its own repository, pytest for every test, mitmproxy for replay

**Status:** accepted
**Decided:** 2026-10-11 (meta-wisekiosk#223 generic Yocto test runner: survey, plan and the resulting decisions; rev 2: WiseBenchManager#2 core, minimal scope; rev 3: WiseBenchManager#4 `bench test`, recovery moves after the hooks and install verifies the booted slot)
**Rev:** 3

## Revisions
- **rev 3** — 2026-10-11 — decision 6 restated to match what `bench test` actually built: the
  next run recovers the board (`Ota.recover`) after the hooks and before installing, not as part
  of the precondition hook itself; `install` verifies the board booted the candidate slot it
  recorded and raises otherwise, rather than accepting whatever slot came back
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

1. The bench runner, WiseBenchManager, is a generic pytest hardware-in-the-loop runner: a Python package in its own WisewareOrg repository, cloned on the pipeline host and run from its checkout at a tag, pointed at a meta-wisekiosk checkout and a small config (board address and hostname, bundle path, hooks, GitHub target) at run time. Neither repository pins or imports the other; every run record names the runner's version. The split is deliberate and the runner is meant to be hard to change once right.
2. Every test is pytest, in two suites the config names: `host_suite` runs on the pipeline host against the built artefacts before anything is installed; `target_suite` runs against the booted board. Setup and teardown are fixtures. labgrid's own pytest plugin provides the board: an env file in the layer names the board's address, templated from the environment, and the layer points pytest at it with `--lg-env`; the runner adds no fixture of its own. Everything that names this appliance — its env file, its units, its config, its page state, its probe, its buildinfo stamp — lives in the layer's own `conftest.py` and tests.
3. Replay uses mitmproxy: server replay from a recorded HAR file, a miss kills the connection, upstream certificates are never fetched, mitmproxy generates the replay CA and one accept step installs it on the board. An addon of about eighty lines writes the `SERVE`/`HIT`/`MISS` lines the record keys on and strips a configurable header list when recording. The sets (config, expected page state, recording) are the layer's data; the runner never sees a set directory, only the one HAR path a layer fixture hands it.
4. The layer (meta-wisekiosk) keeps only what is the appliance's: the host suite (artefact checks before install) and the target suite (checks on the booted board) with their parsing helpers, a config file naming its hooks and the two suites, the replay sets as data, the requirement items, `kiosk-preflight` and the OTA recipes for hand use. The oeqa framework, the shell driver, the bespoke proxy, the driver's self-test and the log viewer are deleted at the cut-over.
5. pytest-reportlog's JSON-lines report is the record: every test report, with whatever a layer's fixtures and tests attach through `record_property`. The runner adds one file beside it per run — its own version, the pytest command line, start and end times, the exit status — and nothing else; a verdict posted as a GitHub check run so a merge queue (Mergify) can require it is a later ticket.
6. A failing test is the candidate's failure and is posted; an error in a fixture or a precondition is the bench's failure, writes DISABLED, stops the timer and posts nothing. The rollback runs in both cases. A run that dies between install and rollback leaves a marker; the next run recovers the board (`Ota.recover`) after the hooks and before installing, reading the marker, asking RAUC which slot booted, marking the candidate bad and rebooting if it is not the recorded good slot, verifying, clearing the marker and continuing. Only a failed recovery is the bench's failure. `install` itself verifies the board booted the candidate slot it recorded before continuing, raising if a rauc fallback left it on the previous slot instead.
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

## Consequences

- A new check is a test file and, at most, a fixture beside it; the runner does not change when a test's needs change.
- meta-wisekiosk loses on the order of seven thousand lines (the driver, its self-test, the viewer, the proxy and its tests, the oeqa framework and case shells); the runner adds on the order of a thousand lines of code plus its tests (an estimate).
- Requirement items point at pytest functions; the record names the items discharged, the image commit, the dirty flag, the set hash and the runner version.
- The merge queue is gated on one physical board, so merges serialise at a job's length; this is accepted.
- Reopened if a second board, power control or a serial console arrives (labgrid becomes worth adopting; the tests' shape does not change), or if the organisation's CI policy changes what may run on the build host.
- An investigation's run is as traceable as a gate's: same command path, same record, no bypass.
