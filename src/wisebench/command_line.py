"""The line a command, a search or an answer is typed on."""

from textual.containers import Horizontal
from textual.widgets import Input, Static


class CommandLine(Horizontal):
    """The line a command, a search or an answer is typed on, and the hint beside it.

    Attributes:
        target (str | None): The queue row selected when it was opened, which `:remove` means.
        asked (bool): It was opened with a question, so what is typed answers that.
        answer (Callable | None): What `y` to that question does.
    """

    def __init__(self, **kwargs):
        """A closed line.

        Args:
            **kwargs: For `Horizontal`.
        """
        super().__init__(**kwargs)
        self.target = None
        self.asked = False
        self.answer = None

    def compose(self):
        """Lay out the input and its hint.

        Yields:
            textual.widget.Widget: The widgets.
        """
        yield Input(id="command", compact=True, select_on_focus=False)
        yield Static("Enter queue it  Esc cancel", id="runhint")

    def open(self, text, target=None, question="", answer=None):
        """Show the line holding `text`, and give it the focus.

        Args:
            text (str): What it starts with.
            target (str | None): The queue row selected, which `:remove` means.
            question (str): What it asks, shown where the text would be; blank for a plain line.
            answer (Callable | None): What `y` to the question does.
        """
        self.target, self.asked, self.answer = target, bool(question), answer
        self.display = True
        command = self.query_one(Input)
        command.placeholder = question
        command.value, command.cursor_position = text, len(text)
        self.screen.set_focus(command)

    def on_input_changed(self, event):
        """Hint at what Enter does while a `:run` is typed.

        Args:
            event (Input.Changed): The change.
        """
        self.query_one("#runhint").display = event.value.startswith(":run ")
