# 0001 — Implement the runner on pytest, with labgrid's pytest plugin for the board and pytest-reportlog for the record

**Status:** accepted
**Decided:** 2026-10-11 (the runner survey that founded this repository; built in WisewareOrg/WiseBenchManager#2 core)
**Rev:** 1

## Revisions

- **rev 1** — 2026-10-29 — first written (WisewareOrg/WiseBenchManager PR #31 ADR 0001 as trade studies).

## Context

The runner needs a test harness for a Yocto layer's suites: one run on the host against the built
artefacts, one against the booted board. What the choice is held to:

- A test owns its own setup and teardown, so the runner does not change when a test's needs change.
- Tests run host-side and reach the board over ssh.
- Every result lands in a machine-readable record, with whatever facts a test attaches.
- One language, read in review: Python; a maintained tool before code of our own.
- One board over ssh: no power control, no serial console, no second board planned.

## Decision

pytest is the harness. Both suites are pytest, and setup and teardown are fixtures. labgrid's own
pytest plugin and env file provide the board. pytest-reportlog's JSON-lines report is the record,
with one run-level file of the runner's own beside it, and a test attaches its facts through
`record_property`.

## Alternatives considered

- **oeqa runtime tests.** Rejected: oeqa runs a test but cannot set one up, so every state a test
  needs becomes code in whatever drives it. Poky marks its target loader for refactoring. As a
  custom test target it fuses runner and layer into one tree, and with no QEMU tier planned its
  QEMU integration carries no weight.
- **LAVA, KernelCI.** Rejected: a database, scheduler and workers for one board; no OTA deploy
  action; tests run on the device rather than host-side; verdicts are not GitHub check runs.
- **Fuego, Avocado, pytest-embedded.** Rejected: Fuego is unmaintained since 2020; Avocado has no
  deploy or boot model; pytest-embedded is scoped to microcontrollers.
- **labgrid as the whole runner.** Rejected: its value is places, a coordinator, power and serial
  drivers and board strategies, none of which one board over ssh uses, and it has no RAUC strategy.
- **pytest with the runner's own ssh transport, test record and test marker.** Rejected: each
  re-implements what labgrid's plugin, pytest-reportlog and `record_property` already do.

## Consequences

- A new check is a test file and, at most, a fixture beside it, in the layer.
- Reopened if a second board, power control or a serial console arrives: labgrid's coordinator and
  strategies become worth adopting, and the tests' shape does not change.
