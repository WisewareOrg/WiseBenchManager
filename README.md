# WiseBenchManager

A generic pytest hardware-in-the-loop runner that takes a ref through build, OTA install, boot,
tests on the booted board, rollback and a verdict. Two commands: `bench run` (timer-triggered,
builds the merge queue's head and gates it) and `bench test <ref> [--set <name>]` (the same path
by hand, with a replay set named when a run must see the same page every time).

Design decisions live in docs/decisions/.
