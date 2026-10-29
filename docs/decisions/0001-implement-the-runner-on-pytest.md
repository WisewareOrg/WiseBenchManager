# 0001 — Implement the runner on pytest, with labgrid's pytest plugin for the board and pytest-reportlog for the record

**Status:** accepted
**Decided:** 2026-10-11 (the runner survey that founded this repository; built in WisewareOrg/WiseBenchManager#2 core)
**Rev:** 1

## Revisions

- **rev 1** — 2026-10-29 — first written (WisewareOrg/WiseBenchManager PR #31 ADR 0001 as trade studies).

## Context

The runner takes a Yocto layer's ref to a verdict on one physical board: build it, put it on the
board, run the layer's checks on the host against the built artefacts and against the booted board,
and report. The checks need a harness, and two things pull on the choice.

First, **Yocto ships one.** oeqa's runtime tests are the native way to test an image, a layer's
custom `TEST_TARGET` controller is its documented extension point, and its `testresults.json`
already records each layer's commit. Any other harness gives that up.

Second, **a check has to own its own setup.** A check on a booted board needs state first — a
replay running, a service stopped, a file in place — and needs it undone after. A harness that runs
a test but cannot set one up pushes every such state into whatever drives it, so that driver changes
with every new check. That is the defect class this choice exists to remove.

The survey behind this choice scored ten candidates against the loop: build a ref, install over the
air, detect the booted slot, run tests host-side against the booted board, replay upstreams, record
with provenance, post a verdict, lock and schedule, and a self-test that can fail. **None closes the
loop**, so the question is not which product to adopt whole but which harness the glue is written
around. What it is held to:

- A check owns its setup and teardown, so the runner does not change when a check's needs change.
- Checks run host-side and reach the board over ssh; one board, no power control, no serial console.
- Every result lands in a machine-readable record, with whatever facts a check attaches to it.
- Python, the one language read in review here; a maintained tool before code of our own.

## Decision

**pytest is the harness.** Both suites are pytest, and setup and teardown are fixtures. Three
maintained pieces carry everything a check needs from the runner:

- **labgrid's own pytest plugin and env file are the board.** The layer's env file names it and the
  layer points pytest at it with `--lg-env`; the runner adds no board fixture of its own.
- **pytest-reportlog's JSON-lines report is the record**, with one run-level file of the runner's
  own beside it naming the runner's version, the command line, the times and the exit status.
- **`record_property` is how a check attaches its facts**, and pytest-reportlog writes them.

## Alternatives considered

- **oeqa runtime tests.** The most Yocto-native route, and what a layer's test side already is.
  Rejected on the second force above: a case runs but cannot set itself up, and cases do not assert;
  verdict objects sit between the test and the fact it checks. As a custom `TEST_TARGET` controller
  it would make `testimage` change the board, fusing runner and layer into one tree, on a loader poky itself marks *"Don't base your targets on this
  code it will be refactored"*. Its record carries no dirty flag and no replay hash. No QEMU tier is
  planned, so its QEMU integration, the thing it does best, carries no weight.
- **labgrid as the whole runner.** The closest fit on paper: a pytest plugin, `SSHDriver` for the
  transport, a `Strategy` state machine as the documented place for install, reboot and slot
  detection, fake drivers for a board-less self-test, and an active release line. Rejected because
  what it adds over its plugin is places, a coordinator, power and serial drivers and board
  strategies, none of which one board over ssh uses; it has no RAUC strategy, so that code is
  written either way; and its coordinator lock has no timer. **Its pytest plugin and env file are
  kept as the board.**
- **pytest with a board transport of our own** (a plain fabric fixture for run, put, get and port
  forward). Less surface than labgrid, and the plan this runner started from chose it. Rejected:
  it re-implements what labgrid's plugin already does, maintained; the same holds for a record
  writer of our own over pytest-reportlog, and a test marker of our own over `record_property`.
- **LAVA.** The serious lab system: real scheduling, health checks, monthly releases, 21 deploy
  methods. Rejected on footprint and shape: a PostgreSQL, web, scheduler and worker stack wanting
  4 GB of RAM at minimum to run one board, scheduling nothing here needs; none of its deploy methods
  is an OTA install, so the install becomes a test-shell command; its test shell runs on the device,
  not host-side; its verdicts are notifications, not GitHub check runs.
- **KernelCI.** LAVA labs plus its own pipeline, built for upstream kernel testing; heavier than LAVA
  and adds nothing on the board side for a product image.
- **Yocto autobuilder.** Builds a ref and schedules, which nothing else surveyed does. Rejected: its
  test matrix is QEMU, its verdicts go to buildbot's UI, and its record is buildbot's log store.
- **Avocado.** A maintained, generic runner whose remote spawner runs tests over ssh — a real
  competitor to pytest as the harness. Rejected: it has no deploy or boot model, and labgrid's board
  integration is a pytest plugin, so the board would be ours to write again.
- **pytest-embedded.** pytest already, with a board model. Rejected: scoped to microcontrollers
  (ESP-IDF, serial, JTAG, QEMU), with no Linux-over-ssh or OTA service.
- **Fuego.** Jenkins in a container with an ssh transport. Rejected: last release December 2020.

## Consequences

- **A new check is a test file and, at most, a fixture beside it, in the layer.** The runner does
  not change when a check's needs change, which is the whole point of the choice.
- **The loop's glue is ours.** Nothing surveyed closes the loop, so running the layer's hooks, the
  lock, the queue and the verdict post are code in this repository, each kept to a call into a
  maintained tool.
- **The suites run under the runner's interpreter.** A layer's test helpers import only what the
  runner's lock file ships, plus the layer's own `pythonpath`; a layer cannot bring its own pytest.
- **labgrid is a pinned dependency, and its env file is the board's config.** The board's identity
  lives in a labgrid file beside `bench.toml`, not in `bench.toml` itself, because labgrid's plugin
  reads its own.
- **No test runs without a board.** There is no QEMU tier; a check of the booted image needs the
  bench.

**Premise that would reopen this:** a second board, power control or a serial console. Each is
what labgrid's coordinator, power drivers and strategies are for, and adopting more of labgrid is
the pre-analysed path; the checks' shape does not change, since they are pytest either way. A
harness with nicer syntax, or a faster one, is not that premise.
