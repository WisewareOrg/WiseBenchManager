import pytest

from wisebench import record
from wisebench.record import Credit, Marks


def run_of(outcome: str, item: str | None = None, **marks: bool) -> record.TestRecord:
    return record.TestRecord(nodeid="t", marks=Marks(item_id=item, **marks), outcome=outcome)


PROVENANCE = run_of("passed", provenance=True)
BROKEN = record.TestRecord(nodeid="b", marks=Marks(), outcome="error", bench_failed=True)


@pytest.mark.parametrize(
    ("test", "ota_ran", "others", "status"),
    [
        (run_of("passed", "TST011"), True, [PROVENANCE], ("passed", None)),
        (run_of("passed", "TST014", recorded=True), True, [PROVENANCE], ("recorded", None)),
        (run_of("passed", "TST011"), False, [PROVENANCE], ("not credited", "ota did not run")),
        (
            run_of("passed", "TST011"),
            True,
            [PROVENANCE, BROKEN],
            ("not credited", "the bench failed"),
        ),
        (run_of("passed", "TST011"), True, [], ("not credited", "no provenance test ran")),
        (
            run_of("passed", "TST011"),
            True,
            [run_of("failed", provenance=True)],
            ("not credited", "a provenance test did not pass"),
        ),
        (run_of("xpassed", "TST011"), True, [PROVENANCE], ("not credited", "test xpassed")),
        (run_of("passed"), True, [PROVENANCE], (None, None)),
    ],
)
def test_the_credit_rule(test, ota_ran: bool, others: list, status: tuple) -> None:
    assert Credit(ota_ran, [test, *others]).status(test) == status


@pytest.mark.parametrize(
    ("tests", "item"),
    [
        ([run_of("passed", "TST011"), run_of("passed", "TST011")], ("passed", None)),
        ([run_of("passed", "TST011"), run_of("failed", "TST011")], ("not credited", "test failed")),
        (
            [run_of("passed", "TST011"), run_of("passed", "TST011", recorded=True)],
            ("not credited", "its tests disagree on recorded"),
        ),
    ],
)
def test_an_item_is_credited_only_when_every_test_carrying_it_is(tests: list, item: tuple) -> None:
    items = Credit(True, [*tests, PROVENANCE]).items([*tests, PROVENANCE])
    assert items == {"TST011": {"status": item[0], "reason": item[1]}}
