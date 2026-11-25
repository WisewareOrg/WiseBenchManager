"""The palette, and text in it that is safe to draw."""

from rich.style import Style
from rich.text import Text
from textual.theme import Theme

COLOURS = {
    "passed": "#98c379",
    "failed": "#e06c75",
    "running": "#e5c07b",
    "hand": "#61afef",
    "dim": "#8b8b8b",
    "log": "#a8a8a8",
    "bright": "#ffffff",
    "rule": "#3a3a3a",
    "ground": "#0c0c0c",
    "ink": "#cccccc",
    "selection": "#1f2a36",
    "running-ground": "#2a2614",
}
KEYS = Text.assemble((" ? ", Style(color=COLOURS["ground"], bgcolor=COLOURS["ink"])), " keys")
THEME = Theme(
    name="bench",
    primary=COLOURS["hand"],
    success=COLOURS["passed"],
    warning=COLOURS["running"],
    error=COLOURS["failed"],
    foreground=COLOURS["ink"],
    background=COLOURS["ground"],
    surface=COLOURS["ground"],
    panel=COLOURS["ground"],
    variables=COLOURS,
)
_CONTROLS = {
    **{code: "^" + chr(code ^ 64) for code in (*range(32), 127)},
    **{code: f"<{code:x}>" for code in (*range(0x80, 0xA0), 0x2028, 0x2029)},
}


def clean(text):
    """`text` safe to put on a terminal: tabs expanded, controls and line separators shown as text.

    See ADR 0006 rev 1.

    Args:
        text (str): Text read from a log, a record name, git or GitHub.

    Returns:
        str: The text with no character a terminal acts on.
    """
    return text.expandtabs(8).translate(_CONTROLS)


def styled(text, meaning=None, bold=False):
    """Text safe to draw, in the colour of its meaning.

    Args:
        text (str): The text, from anywhere.
        meaning (str | None): A key of `COLOURS`, or None for the text colour.
        bold (bool): Draw it bold.

    Returns:
        Text: The cleaned text.
    """
    return Text(clean(text), Style(color=COLOURS.get(meaning), bold=bold))


def line(*parts):
    """One line of several colours.

    Args:
        *parts (tuple): Arguments of `styled`.

    Returns:
        Text: The parts joined.
    """
    return Text.assemble(*(styled(*part) for part in parts), no_wrap=True, overflow="ellipsis")


def table(rows):
    """Table rows with every cell padded to its column, so a row's ground has no gaps.

    Args:
        rows (list[tuple]): Per row, its key and its cells.

    Returns:
        list[tuple]: The rows.
    """
    for column in zip(*(cells for _, cells in rows), strict=True):
        width = max(cell.cell_len for cell in column) + 2
        for cell in column:
            cell.pad_right(width - cell.cell_len)
    return rows
