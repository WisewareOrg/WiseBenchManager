# 0006 — Build the watch view on urwid

**Status:** accepted
**Decided:** 2026-11-17 (WisewareOrg/WiseBenchManager #77 replace the curses watch view with urwid)
**Rev:** 1

## Revisions

- **rev 1** — 2026-11-17 — first written (#77 replace the hand-written curses watch view with
  urwid).

## Context

`bench status --watch` was a curses screen on a framework of its own: the event loop, the layout
arithmetic, colour-pair allocation and its 256-colour fallback, scrolling, cursor clamping and a
fake curses window to test it, with tests pinning text to cell positions. No maintained viewer reads
the runner's markers, so the view is the runner's own; a maintained TUI library still provides most
of that framework.

What the choice is held to:

- The view behaves as it does, keys included: the same tabs, the same chords, the same writes
  through `Queue` and `bench_state`, and `entries()` off the draw path (ADR 0005 rev 1).
- A maintained tool before code of our own.
- Tests cover what the view does for the operator, not what the toolkit does.

## Decision

**The watch view is built on urwid: its main loop, palette, containers and scrolling list.**

1. **Every key goes to `view.handle`; no urwid widget takes one.** urwid's `MainLoop` hands keys to
   an input filter, which presses each key and then refreshes. A named key with no meaning in the
   view does nothing. Mouse handling is off, so the terminal's own text selection still copies a
   sha.
2. **The domain stays free of any toolkit.** `view.resolve(state, snap)` derives the frame, and
   `view.handle(state, key, frame, page)` acts on the frame that was shown. The screen keeps urwid's
   lists across refreshes, sets each list's focus from the frame, and lets urwid scroll only as far
   as keeps that row in view.
3. **The palette declares the canvas's colours exactly, as 24-bit colour.** A terminal without
   24-bit colour shows approximations of them.
4. **Six pieces stay ours, because urwid does not cover them:**
   - **The chords** (`gg`, `dd`, `za`, `zR`, `zM`): urwid has no key sequences.
   - **`/` search across folded sections:** no urwid widget searches rows it does not show.
   - **The Queue, History and log selections, held by identity:** a `ListBox`'s focus follows an
     index, so it would move to another row as rows come and go.
   - **The command line:** urwid's `Edit` keeps a second buffer and cursor, while the line's text
     drives the `:run` preview and the tab switch from the view's own state.
   - **The sanitiser that shows control characters as carets:** urwid passes ESC and BEL to the
     terminal raw, so log text would drive the operator's terminal.
   - **The daemon thread that reads the queue:** `concurrent.futures` joins its workers at exit,
     so a `q` during a GitHub read would hold the terminal; urwid's executors need its asyncio loop
     for one thread's work.

## Alternatives considered

- **Textual.** Rejected: it has had no release since 2026-06-30.
- **prompt_toolkit.** Rejected: it has no tabs, table or tree widgets, so the layout would stay
  ours.
- **Staying on hand-written curses.** Rejected: the layout, colour fallback, scrolling and the fake
  screen that tests them are a framework this repository maintains for no reason of its own.
- **urwid's tree widgets for the folded log.** Rejected: which sections are open is a rule of the
  view, and tree nodes would hold that state and need reconciling with it every second. The log is a
  flat list refilled from the view's rows.

## Consequences

- **urwid is a runtime dependency,** LGPL-2.1, imported unmodified. mitmproxy pins urwid for its own
  console, so it runs from its own environment and its pin never sets the runner's.
- **A Queue longer than its pane scrolls.** `dd` still acts only on the selected row, which the
  list's focus keeps on screen.
- **Without a terminal the view exits 2** with `BENCH ERROR: bench status --watch needs a terminal`;
  `q`, `:q` and Ctrl-C exit 0.
- **The tests drive the view at three tiers:** the domain through `resolve` and `handle`, the shell
  through `Shell.tick` and `Shell.press`, and the screen as the text of the rendered widget. None
  asserts a cell, a colour or a width.
