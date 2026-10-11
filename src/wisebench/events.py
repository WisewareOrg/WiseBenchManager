"""The event stream the status view and the terminal read."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FILENAME = "events.jsonl"


def utc_now() -> str:
    """The current UTC time in ISO 8601 with a `Z` suffix."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class EventLog:
    """Appends one JSON object per line to `events.jsonl`: `ts`, `kind`, then the event's fields."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / FILENAME

    def emit(self, kind: str, **fields: Any) -> None:
        line = json.dumps({"ts": utc_now(), "kind": kind, **fields}, default=str)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
