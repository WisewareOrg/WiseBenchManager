"""`marks.reason`: the exception-first-line helper shared by every stage/run marker's `reason=`.

The marker grammar itself has no unit test here: it is pinned by reading the literal lines
`bench.log` ends up holding (see test_bench_test.py, test_bench_run.py, test_cli.py), not by a
writer/parser pair of its own.
"""

from wisebench import marks


def test_reason_is_the_exceptions_first_line():
    """reason() is the first line of str(exc) for an exception with a message."""
    assert marks.reason(RuntimeError("first line\nsecond line")) == "first line"


def test_reason_is_empty_for_an_exception_with_no_message():
    """reason() is "" for an exception whose str() is empty (e.g. a bare KeyboardInterrupt)."""
    assert marks.reason(KeyboardInterrupt()) == ""
    assert marks.reason(SystemExit()) == ""
