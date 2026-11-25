# 0006 — Build the watch view on Textual's own widgets, keys and reactive model

**Status:** accepted
**Decided:** 2026-11-25 (WisewareOrg/WiseBenchManager#77 watch view on Textual)
**Rev:** 1

## Revisions

- **rev 1** — 2026-11-25 — first written (#77 watch view on Textual).

## Context

`bench status --watch` reads the records, the bench's state and the queue, and changes the bench on a
typed command. Its rules and words are this bench's own: which stage shows, how the running run
merges with the queue, what `:run` would queue. Drawing them on a terminal is not, and the first view
did that by hand on `curses`: an event loop, layout arithmetic, colour pairs, scrolling, cursor
clamping, a line editor, a worker thread, and a fake window to test it all. The question is how much
of that a maintained tool takes, and what is left here to maintain. What it is held to:

- A maintained tool before code of our own, and a bespoke piece only with a sentence saying why no
  maintained tool covers it.
- Text from a log or from GitHub never drives the terminal; mouse reporting stays off.
- No read or write holds up the screen, and a change acts on the entry the operator chose.
- The look and behaviour follow the design canvas of WisewareOrg/WiseBenchManager#12 `bench status
  --watch`.

Each candidate was built, except the Neovim plugin, which was estimated. Lines of code leave out
docstrings and blank lines; CSS counts in both. Branching is ruff's C901, summed over functions, with
the worst function after it.

| Build | Lines | Code | Functions | Test lines | Branching, worst |
|---|---|---|---|---|---|
| Hand-written `curses` | 1,732 | 1,148 | 81 | 3,165 | 279, 28 |
| urwid, the view ported | 1,709 | 1,109 | 86 | 2,549 | — |
| urwid, the view ported, then its own widgets adopted | 1,703 | 1,084 | 97 | 2,702 | — |
| Textual, the view ported | 1,677 | 1,118 | 108 | 2,483 | — |
| urwid, built fresh | 1,430 | 792 | 63 | 686 | — |
| Textual, built fresh, one redraw loop | 1,521 | 803 | 78 | 854 | 187, 10 |
| Textual, built fresh, reactive, own widgets and keys | 2,286 | 1,325 | 132 | 1,671 | 250, 8 |
| **Textual, stock widgets and keys** | **1,336** | **720** | **76** | not written when chosen | 157, 10 |

The last branching figure was taken before the vim keys were added to that build.

## Decision

**The watch view is built on Textual, with Textual's own widgets, keys, and reactive and worker
model. The code here holds only the bench's rules, words, layout and colours.**

- Textual's `Static`, `Tabs`, `ContentSwitcher`, `DataTable`, `Tree`, `Log`, `Input` and `HelpPanel`
  draw everything; CSS gives the layout and the colours of the design canvas.
- Keys are Textual's. `j` `k` `h` `l` `g` `G` `^d` `^u` press the arrow, Home, End or page key they
  stand for; `?` opens Textual's keys panel. There are no two-key commands, so no pending-key state.
  Anything that changes the bench is a typed `:` command.
- What is shown follows reactive attributes, and every read and write runs off the screen's thread in
  a Textual worker.

The pieces kept as our own, and why no maintained tool covers each:

- **`Rows`, a subclass of `DataTable`:** a refilled `DataTable` forgets its cursor and reports the fill
  as a move, and its Home and End go to a row's ends, so `Rows` holds its rows as data, keeps the cursor
  on its row, updates only changed cells, and binds Home and End to the first and last row.
- **`Tests`, a subclass of `Tree`:** `Tree` colours the row under the cursor over its label's own
  colours, which would hide that a test failed or is running, so one method keeps the label's colours
  and takes only the cursor's ground.
- **`clean`, the sanitiser:** Textual writes the characters of a string to the terminal as they are,
  so control characters in text from a log, a record or GitHub are shown as text before they reach a
  widget.
- **The daemon-thread reads:** asyncio joins its executor's threads when the process exits, and
  Textual's thread workers run on that executor, so a read through either would hold up `q` until it
  finished.

## Alternatives considered

- **Staying on the hand-written `curses` view.** No dependency, and it worked. Rejected: 1,148 lines
  of code, a worst function branching 28 ways, and 3,165 lines of tests, much of it for a fake window
  and cell positions, to do what a maintained toolkit does.
- **urwid, porting the existing view, then adopting its own widgets.** Rejected: 1,109 lines of code,
  then 1,084, against 1,148. A port keeps the old view's state, frame and key-routing model, and those
  are the size. The port first drew every log row on each key, 958 to 1,140 ms per key on a
  7,882-line log and about 10 ms once fixed.
- **urwid, built fresh from the canvas.** The smallest of the fresh builds that kept keys of their
  own, at 792 lines of code against 803 for the same design on Textual. Close in size, so rejected
  for Textual as the toolkit new projects choose (37,732 GitHub stars against 3,027) and whose
  maintainer states that they will continue. A urwid build limited to urwid's own widgets
  and keys was not made. On maintenance the two are alike in shape: each has one principal
  maintainer, with 364 commits to Textual and 479 to urwid in the year to 2026-11-25. urwid
  had eight releases from 2026-10-28 to that date; Textual's last was 8.2.8, of 2026-06-30.
- **Textual, porting the existing view.** Rejected: 1,118 lines of code and 108 functions, for the
  same reason as the urwid port.
- **Textual, built fresh with one loop that redraws and one list widget of our own.** 803 lines of
  code. Rejected: it drives widgets from its own loop, and the defects found came from there. A 2 s
  `Queue.add` stalled its event loop 2.02 s (0.03 s in the build chosen). `:remove` after the selected
  run had left the queue removed another run, in two cases; the build chosen takes the target when
  the command line opens, and was right in both. Home, End, the arrows and the page keys moved no
  cursor, because the scroll view under the list took them.
- **Textual, built fresh and reactive, with widgets and keys of our own.** Nothing on screen was
  given up. Rejected: at 2,286 lines and 1,325 of code it was the largest of all, and its 132
  functions were custom keys and the widgets written to carry them. Dropping them for Textual's own
  is what cut the view to 720.
- **A Neovim plugin.** Vim's keys for free. Rejected on its look, and estimated, not built, at 1,700
  to 1,900 lines to draw the canvas, or 1,100 to 1,150 with Neovim's own idioms: it needs Lua beside
  Python, and Neovim on every host that watches.

## Consequences

- **The size is the bench's own.** The code that is left is its rules, words, layout and colours; the
  widgets, scrolling, colour depth, line editing, tests' driver and keys panel are Textual's.
- **A key is cheap.** On an 8,241-line log and an 11,728-line suite a key takes 9 to 40 ms of CPU,
  `PgDn` in History over 594 runs about 67 ms, and opening every test of an 11,728-line suite at once
  930 ms. Reading the disk takes about 160 ms each second and 2.3 s the first time (594 runs), on a
  thread of its own, so Run is blank for those first seconds. `q` during a 6 s read of GitHub took
  5.5 s on an executor thread and 0.6 s on a daemon thread.
- **A dependency, with one maintainer.** `textual` and the six packages it brings (`rich`,
  `markdown-it-py`, `mdit-py-plugins`, `linkify-it-py`, `mdurl`, `platformdirs`) join the runner's
  lock. At the decision Textual's last release was 8.2.8 of 2026-06-30; its maintainer states that they
  continue to maintain it, and that review can wait.
- **Some keys differ from vim.** `g` is the top where vim takes `gg`, `^d` and `^u` move a page where
  vim moves half, Enter or Space folds, there is no previous match, and `Tab` moves focus, not the
  view. A plain stage's log has no line cursor, so a search scrolls to its match.
- **Some of the design canvas is left out.** The PR title beside its number in Run and History, the
  exit code, the idle board's waiting line and timer period, when a merge-queue entry was added, how long
  ago the merge queue was read, `Tab` completion of `:run`, the `:enable` echo and the `za` fold hint
  are not shown. The canvas's `4 Bench` tab is #25 Bench view's.
- **It forecloses** key sequences, a pending key, a key that reads the next key, and a look that
  Textual's CSS cannot give its widgets; each would bring back a widget of our own.

**Premise that would reopen this:** Textual ceasing to be maintained, or a need that its widgets
cannot meet without a widget of our own as large as the one it replaces.
