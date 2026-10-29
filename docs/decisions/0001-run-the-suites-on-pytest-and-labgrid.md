# 0001 — Run the suites with a small runner on pytest and labgrid's plugin, not a test framework of its own

**Status:** accepted
**Decided:** 2026-10-11 (WisewareOrg/WiseBenchManager#2 core: pytest-reportlog record, run-level file, labgrid for ssh)
**Rev:** 1

## Revisions

- **rev 1** — 2026-10-29 — first written.

## Context

A Yocto layer needs one path from a git ref to a verdict on one physical board: build, install,
test on the host and on the booted board, report to GitHub. The path runs on a timer to gate a merge
queue and by hand for investigations.

The path it replaces was a shell driver around Yocto's oeqa runtime framework. oeqa runs a test but
cannot set one up, so every state a test needed became driver code, and the driver changed with
every test ticket. What the choice is held to:

- A test owns its own setup and teardown; the runner does not change when a test's needs change.
- One language, read in review: Python.
- A maintained tool before code of our own.
- One board over ssh: no power control, no serial console, no second board planned.

## Decision

A small Python runner in its own repository. Every test is pytest, in a host suite and a target
suite the layer names. labgrid's own pytest plugin and env file are the board; pytest-reportlog's
JSON-lines report is the record. The runner adds only the job around them: the layer's hooks, the
lock and the queue, the check run, and the fixtures no maintained tool provides.

## Alternatives considered

- **Keep the shell driver and oeqa.** Rejected: oeqa has no fixtures, so the driver grows with every
  test's needs; poky marks oeqa's target loader for refactoring; shell is not read in review here.
- **Keep the shell driver, move the tests to pytest.** Rejected: two languages, and the driver is
  rewritten later anyway.
- **An oeqa custom test target.** Rejected: the most Yocto-native route, but it fuses runner and
  layer into one tree, on the loader poky marks for refactoring. No QEMU tier is planned, so oeqa's
  QEMU integration carries no weight.
- **labgrid as the whole runner.** Rejected: its value is places, a coordinator, power and serial
  drivers and board strategies, none of which one board over ssh uses, and it has no RAUC strategy.
  Its pytest plugin and env file are used as the board.
- **LAVA, KernelCI.** Rejected: a database, scheduler and workers for one board; no OTA deploy
  action; tests run on the device rather than host-side; verdicts are not GitHub check runs.
- **Fuego, Avocado, pytest-embedded.** Rejected: Fuego is unmaintained since 2020; Avocado has no
  deploy or boot model; pytest-embedded is scoped to microcontrollers.
- **The runner's own ssh transport, record writer, test marker and fake board.** Rejected: each
  re-implements what labgrid's plugin, pytest-reportlog and `record_property` already do.

## Consequences

- A new check is a test file and, at most, a fixture beside it, in the layer.
- The suites run under the runner's interpreter, so a layer's test helpers import only what the
  runner's lock file ships, plus the layer's own `pythonpath`.
- Reopened if a second board, power control or a serial console arrives: labgrid's coordinator and
  strategies become worth adopting, and the tests' shape does not change.
