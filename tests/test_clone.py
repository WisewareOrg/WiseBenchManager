"""`clone`: the WiseBenchManager clone the installed package lives in is the bench."""

from wisebench import clone


def test_root_is_the_clone_the_package_is_installed_in_not_the_working_directory(bench_clone):
    assert clone.root().resolve() == bench_clone.root.resolve()


def test_locate_names_the_checkout_and_the_records_in_the_same_clone_as_root():
    assert clone.locate() == (clone.root() / "checkout", clone.root() / "records")
