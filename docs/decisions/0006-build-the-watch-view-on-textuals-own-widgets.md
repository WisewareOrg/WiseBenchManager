# 0006 — Build the watch view on Textual's own widgets, keys and reactive model

**Status:** accepted
**Decided:** 2026-11-25 (WisewareOrg/WiseBenchManager#77 watch view on Textual)
**Rev:** 1

## Revisions

- **rev 1** — 2026-11-25 — first written (#77 watch view on Textual).

## Context

`bench status --watch` reads the records, the bench's state and the queue, and changes the bench on
a typed command. Its rules and words are this bench's own: which stage shows, how the running run
merges with the queue, what `:run` would queue. Drawing them on a terminal is not, and the view of
WisewareOrg/WiseBenchManager#22 Run and History views, and the tickets that extended it, did that by
hand on `curses`: an event loop, layout arithmetic, colour pairs, scrolling, cursor clamping, a line
editor, a worker thread, and a fake window to test it all. The question is how much of that a
maintained tool takes, and what is left here to maintain. What it is held to:

- A maintained tool before code of our own, and a bespoke piece only with a sentence saying why no
  maintained tool covers it.
- Text from a log or from GitHub never drives the terminal; mouse reporting stays off.
- No read or write holds up the screen, and a change acts on the entry the operator chose.
- The look and behaviour follow the design canvas of WisewareOrg/WiseBenchManager#12
  `bench status --watch`.

Each candidate was built, except the Neovim plugin, which was estimated. Lines of code leave out
docstrings and blank lines; CSS counts in both. Branching is ruff's C901, summed over functions,
with the worst function after it. The table was measured 2026-11-25. The builds not chosen were
measured as built, without the fixes made to the chosen build, and were not kept; the chosen build's
figures are the view's ten modules and its CSS.

| Build | Lines | Code | Functions | Test lines | Branching, worst |
|---|---|---|---|---|---|
| Hand-written `curses` | 1,732 | 1,148 | 81 | 3,165 | 279, 28 |
| urwid, the `curses` view ported | 1,709 | 1,109 | 86 | 2,549 | — |
| urwid, the `curses` view ported, then its own widgets adopted | 1,703 | 1,084 | 97 | 2,702 | — |
| Textual, the `curses` view ported | 1,677 | 1,118 | 108 | 2,483 | — |
| urwid, built fresh | 1,430 | 792 | 63 | 686 | — |
| Textual, built fresh, one redraw loop | 1,521 | 803 | 78 | 854 | 187, 10 |
| Textual, built fresh, reactive, own widgets and keys | 2,286 | 1,325 | 132 | 1,671 | 250, 8 |
| **Textual, stock widgets and keys** | **1,843** | **910** | **103** | 1,835 | 208, 9 |

## Decision

**The watch view is built on Textual, with Textual's own widgets, keys, and reactive and worker
model. The code here holds only the bench's rules, words, layout and colours.**

Size did not decide it: the build chosen has about as much code as the two fresh builds that kept
widgets or keys of their own, and it carries the fixes found in review, which they do not. What
decided it is that the widgets, the layout, the scrolling and the binding of keys to actions are
Textual's, with nothing of ours under them but the pieces below, and that the fresh Textual build
that kept a list widget and a redraw loop of its own got behaviour wrong (see the alternatives).

- Textual's `Static`, `Tabs`, `ContentSwitcher`, `DataTable`, `Tree`, `Log`, `Input` and `HelpPanel`
  draw everything; CSS gives the layout, and one registered Textual theme the colours of the design
  canvas.
- Keys are Textual's bindings. `j` `k` `h` `l` `g` `G` `^d` `^u` act as the arrow, Home, End or page
  key they stand for; `?` opens Textual's keys panel. There are no two-key commands, so no
  pending-key state. Anything that changes the bench is a typed `:` command.
- What is shown follows reactive attributes, and every read and write runs off the screen's thread
  in a Textual worker.

The pieces kept as our own, and why no maintained tool covers each:

- **`Rows`, a subclass of `DataTable`:** a refilled `DataTable` forgets its cursor and reports the
  fill as a move, and its Home and End go to a row's ends, so `Rows` holds its rows as data, keeps
  the cursor on its row, updates only changed cells, and binds Home and End to the first and last
  row.
- **`Tests`, a subclass of `Tree`:** `Tree` colours the row under the cursor over its label's own
  colours, which would hide that a test failed or is running, and it builds and measures a styled
  label for every line when a test opens, so `Tests` keeps the label's colours under the cursor and
  styles a line of a test's output as it is drawn.
- **`clean`, the sanitiser:** Textual writes the characters of a string to the terminal as they are,
  and `Log` splits a line at a Unicode line separator, so controls and separators in text from a
  log, a record or GitHub are shown as text before they reach a widget.
- **`Watch.on_event`, which acts on each key before the next is read:** Textual posts a key to the
  widget focused when the key is read, and runs the key's binding only after the key has bubbled
  back up, so a key read in the meantime goes to the old focus; each key is therefore resolved as it
  arrives, through Textual's own `screen.active_bindings` and `run_action`, and a vim key is
  resolved as the key it names. Two stock routes were rejected: priority bindings, because `:`
  overtakes a `j` still bubbling and `:remove` takes the wrong row, and a `key_enter` on `Input`,
  because Enter overtakes a Backspace still bubbling.
- **The daemon-thread reads:** asyncio joins its executor's threads when the process exits, and
  Textual's thread workers run on that executor, so a read through either would hold up `q` until it
  finished.

## Alternatives considered

- **Staying on the hand-written `curses` view.** No dependency, and it worked. Rejected: 1,148 lines
  of code, a worst function branching 28 ways, and 3,165 lines of tests, much of it for a fake
  window and cell positions, to do what a maintained toolkit does.
- **urwid, porting the `curses` view, then adopting its own widgets.** Rejected: 1,109 lines of
  code, then 1,084, against 1,148. A port keeps the `curses` view's state, frame and key-routing
  model, and those are the size. The port first drew every log row on each key, so a key was slow on
  a long log until that was fixed.
- **urwid, built fresh from the canvas.** The smallest of the fresh builds that kept keys of their
  own, at 792 lines of code against 803 for the same design on Textual; it keeps a list walker, a
  row widget and two-key commands of its own. Close in size, so rejected for Textual as the toolkit
  new projects choose (37,732 GitHub stars against 3,027) and whose maintainer states that they will
  continue. A urwid build limited to urwid's own widgets and keys was not made. On maintenance the
  two are alike in shape: each has one principal maintainer, with 364 commits to Textual and 479 to
  urwid in the year to 2026-11-25. urwid had eight releases from 2026-10-28 to that date, where
  Textual's last was 8.2.8, of 2026-06-30.
- **Textual, porting the `curses` view.** Rejected: 1,118 lines of code and 108 functions, for the
  same reason as the urwid port.
- **Textual, built fresh with one loop that redraws and one list widget of our own.** 803 lines of
  code. Rejected: it drives widgets from its own loop, and the defects found came from there. A slow
  `Queue.add` stalled its event loop for as long as the call took; the build chosen does not stall.
  `:remove` after the selected run had left the queue removed another run, in two cases; the build
  chosen takes the target when the command line opens, and was right in both. Home, End, the arrows
  and the page keys moved no cursor, because the scroll view under the list took them.
- **Textual, built fresh and reactive, with widgets and keys of our own.** Nothing on screen was
  given up. Rejected: at 2,286 lines and 1,325 of code it was the largest of all, and its 132
  functions were custom keys and the widgets written to carry them.
- **A Neovim plugin.** Vim's keys for free. Rejected on its look, and estimated, not built, at 1,700
  to 1,900 lines to draw the canvas, or 1,100 to 1,150 with Neovim's own idioms: it needs Lua beside
  Python, and Neovim on every host that watches.

## Consequences

- **The code left is the bench's own:** its rules, words, layout and colours; the widgets,
  scrolling, colour depth, line editing, tests' driver and keys panel are Textual's.
- **A very long open test costs more a key.** A key that steps onto a suite with a very long open
  test costs more than it did in the `curses` view, because `Tree` adds its rows one at a time; this
  is accepted. The records are read on a thread of their own, so Run is blank until the first read
  ends, and a read still running never holds up `q`, because it runs on a daemon thread.
- **A dependency, with one maintainer.** `textual`, and `rich`, which the view also imports, join
  the runner's lock with the five packages they bring (`markdown-it-py`, `mdit-py-plugins`,
  `linkify-it-py`, `mdurl`, `platformdirs`). Textual's maintainer states that they continue to
  maintain it, and that review can wait.
- **Some keys differ from vim.** `g` is the top where vim takes `gg`, `^d` and `^u` move a page
  where vim moves half, Enter or Space folds, there is no previous match, and `Tab` moves focus, not
  the view. A plain stage's log has no line cursor, so a search scrolls to its match.
- **Some of the design canvas is left out.** The PR title beside its number in Run and History, the
  exit code, the idle board's waiting line and timer period, when a merge-queue entry was added, how
  long ago the merge queue was read, `Tab` completion of `:run`, the `:enable` echo and the `za`
  fold hint are not shown. The canvas's `4 Bench` tab is the WisewareOrg/WiseBenchManager#25 Bench
  view.
- **Two small looks.** An empty bench shows `0 records in records/` and `IDLE`. In the command line,
  Backspace and the arrow keys do not restart the cursor's blink, because their bindings run without
  the key reaching `Input`.
- **It forecloses** key sequences, a pending key, a key that reads the next key, and a look that
  Textual's CSS cannot give its widgets; each would bring back a widget of our own.

**Premise that would reopen this:** Textual ceasing to be maintained, or a need that its widgets
cannot meet without a widget of our own as large as the one it replaces.
