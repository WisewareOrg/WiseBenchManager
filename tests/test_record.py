import pytest

from wisebench import record
from wisebench.record import Credit, Marks


def item(outcome: str, item_id: str | None = None, **marks: bool) -> record.TestRecord:
    return record.TestRecord(nodeid="t", marks=Marks(item_id=item_id, **marks), outcome=outcome)


def provenance(outcome: str = "passed") -> record.TestRecord:
    return item(outcome, provenance=True)


def status(test: record.TestRecord, ota_ran: bool = True, *others: record.TestRecord) -> tuple:
    return Credit(ota_ran, [test, *(others or [provenance()])]).status(test)


def test_passing_item_is_passed_when_the_session_is_creditable() -> None:
    assert status(item("passed", "TST011")) == ("passed", None)


def test_recorded_item_is_recorded_when_the_session_is_creditable() -> None:
    assert status(item("passed", "TST014", recorded=True)) == ("recorded", None)


def test_without_ota_nothing_is_credited() -> None:
    assert status(item("passed", "TST011"), False) == ("not credited", "ota did not run")
    recorded = item("passed", "TST014", recorded=True)
    assert status(recorded, False) == ("not credited", "ota did not run")


def test_a_fixture_error_anywhere_credits_nothing() -> None:
    broken = item("error")
    broken.bench_failed = True
    assert status(item("passed", "TST011"), True, provenance(), broken) == (
        "not credited",
        "the bench failed",
    )


def test_no_provenance_test_credits_nothing() -> None:
    other = item("passed")
    assert status(item("passed", "TST011"), True, other) == (
        "not credited",
        "no provenance test ran",
    )


@pytest.mark.parametrize("outcome", ["failed", "skipped", "error"])
def test_a_provenance_test_that_did_not_pass_credits_nothing(outcome: str) -> None:
    assert status(item("passed", "TST011"), True, provenance(), provenance(outcome)) == (
        "not credited",
        "a provenance test did not pass",
    )


@pytest.mark.parametrize("outcome", ["failed", "skipped", "error", "xfailed"])
def test_an_item_whose_test_did_not_pass_is_not_credited(outcome: str) -> None:
    assert status(item(outcome, "TST011")) == ("not credited", f"test {outcome}")
    recorded = item(outcome, "TST014", recorded=True)
    assert status(recorded) == ("not credited", f"test {outcome}")


def test_a_test_without_an_item_has_no_status() -> None:
    assert status(item("passed")) == (None, None)


def test_an_item_is_credited_when_every_test_carrying_it_is() -> None:
    tests = [item("passed", "TST011"), item("passed", "TST011"), provenance()]
    assert Credit(True, tests).items(tests) == {"TST011": {"status": "passed", "reason": None}}


def test_one_failing_test_withholds_its_item() -> None:
    tests = [item("passed", "TST011"), item("failed", "TST011"), provenance()]
    assert Credit(True, tests).items(tests) == {
        "TST011": {"status": "not credited", "reason": "test failed"}
    }


def test_recorded_items_and_untagged_tests() -> None:
    tests = [item("passed", "TST014", recorded=True), item("passed"), provenance()]
    assert Credit(True, tests).items(tests) == {"TST014": {"status": "recorded", "reason": None}}


def test_tests_disagreeing_on_recorded_withhold_the_item() -> None:
    tests = [item("passed", "TST011"), item("passed", "TST011", recorded=True), provenance()]
    assert Credit(True, tests).items(tests) == {
        "TST011": {"status": "not credited", "reason": "its tests disagree on recorded"}
    }
