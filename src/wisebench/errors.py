"""The runner's one error type."""


class BenchFailure(Exception):
    """The bench's own failure: raised from fixtures and hooks, it surfaces as a pytest ERROR."""
