from pathlib import Path

import pytest


def test_home_is_a_temporary_directory(tmp_path_factory: pytest.TempPathFactory) -> None:
    assert tmp_path_factory.getbasetemp() in Path.home().parents
