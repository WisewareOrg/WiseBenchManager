"""Two added boards through real `bench run` processes on a toy layer, in a scratch clone and HOME.

The clone holds a copy of the package, so `clone.root()` is the scratch one; `systemctl` is a
recorder first on PATH; the units name nothing; there is no `[github]` table. Each step of the toy
layer appends what it sees to one file, and can run one other command of the bench from inside
itself, which makes two boards' runs overlap without a sleep.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import git, repo_with_one_commit

import wisebench

PACKAGE = Path(wisebench.__file__).resolve().parent

STEP = """
import json, os, pathlib, subprocess, sys

def head():
    tree = os.environ["BENCH_TREE"]
    return subprocess.check_output(["git", "-C", tree, "rev-parse", "HEAD"], text=True).strip()

def step(name):
    board = pathlib.Path(os.environ["LG_ENV"]).parent.name
    inside = pathlib.Path(os.environ["TOY"], f"inside-{board}-{name}.json")
    before = head()
    said = None
    if inside.exists():
        command = json.loads(inside.read_text())
        inside.unlink()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("BENCH_", "LG_"))}
        said = subprocess.run(command, env=env, capture_output=True, text=True).stdout
    seen = {"board": board, "step": name, "before": before, "after": head(), "said": said,
            "sha": os.environ["BENCH_SHA"],
            "labgrid": {k: v for k, v in os.environ.items() if k.startswith("LG_")}}
    with open(os.environ["TOY"] + "/steps", "a") as out:
        out.write(json.dumps(seen) + "\\n")

if __name__ == "__main__":
    step(sys.argv[1])
"""
SUITE = "import step\ndef test_step(pytestconfig):\n    step.step({name!r})\n"
TARGET = (
    SUITE + "    assert pytestconfig.getoption('--lg-env') == __import__('os').environ['LG_ENV']\n"
)


class Toy:
    """The scratch clone, its two boards and the toy layer they run."""

    def __init__(self, home):
        """Build the HOME, the clone, the layer and the boards `a` and `b` under `home`."""
        self.home, self.root = home, home / "clone"
        shutil.copytree(
            PACKAGE, self.root / "src" / "wisebench", ignore=shutil.ignore_patterns("__pycache__")
        )
        self.layer = home / "layer"
        self.layer.mkdir()
        repo_with_one_commit(self.layer)
        hook = f"{sys.executable} step.py"
        (self.layer / "bench.toml").write_text(
            f'[hooks]\nprecondition = "{hook} precondition"\nbuild = "{hook} build"\n'
            f'bundle = "{hook} bundle"\n[tests]\nhost_suite = "host"\ntarget_suite = "target"\n'
        )
        (self.layer / "step.py").write_text(STEP)
        (self.layer / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = "."\n')
        for name, body in (("host", SUITE), ("target", TARGET)):
            (self.layer / name).mkdir()
            (self.layer / name / f"test_{name}.py").write_text(body.format(name=name))
        git(self.layer, "add", ".")
        git(self.layer, "commit", "-q", "-m", "layer")
        bin_dir = home / "bin"
        bin_dir.mkdir()
        (bin_dir / "systemctl").write_text(
            f"#!{sys.executable}\nimport sys\nopen({str(home / 'systemctl')!r}, 'a')"
            ".write(' '.join(sys.argv[1:]) + '\\n')\n"
        )
        (bin_dir / "systemctl").chmod(0o755)
        settings = home / ".config" / "wisebench"
        settings.mkdir(parents=True)
        (settings / "bench.env").write_text(
            f"LG_ENV={home / 'first.yaml'}\nLG_BENCH_ADDRESS=first.invalid\n"
        )
        for name in ("a", "b"):
            checkout = self.root / "boards" / name / "checkout"
            subprocess.run(["git", "clone", "-q", self.layer, checkout], check=True)
            Path(self.lg_env(name)).write_text("targets: {}\n")
        self.env = {
            "HOME": str(home),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "PYTHONPATH": str(self.root / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TOY": str(home),
        }
        self.refuse_anything_live()

    def refuse_anything_live(self):
        """Print what a `bench` process of this clone resolves, and refuse a path outside it."""
        code = (
            "from wisebench import clone, config\n"
            "for path in (clone.root(), *clone.locate(), *clone.locate('a'),"
            " config.settings_path()): print(path)"
        )
        seen = subprocess.run(
            [sys.executable, "-c", code], env=self.env, capture_output=True, text=True, check=True
        ).stdout
        print(seen, self.home)
        assert all(Path(line).is_relative_to(self.home) for line in seen.splitlines())

    def lg_env(self, name):
        """The labgrid file of board `name`."""
        return str(self.root / "boards" / name / "env.yaml")

    def command(self, *args):
        """The argv of one `bench` command."""
        return [sys.executable, "-m", "wisebench.cli", *args]

    def bench(self, *args):
        """Run one `bench` command to its end."""
        return subprocess.run(self.command(*args), env=self.env, capture_output=True, text=True)

    def inside(self, board, step, *args):
        """Have `board`'s next `step` run one `bench` command from inside itself."""
        path = self.home / f"inside-{board}-{step}.json"
        path.write_text(json.dumps(self.command(*args)))

    def steps(self, board):
        """What each step of `board`'s runs saw, in order."""
        lines = (self.home / "steps").read_text().splitlines()
        return [seen for seen in map(json.loads, lines) if seen["board"] == board]

    def records(self, board):
        """The record folders of `board`, oldest first."""
        return sorted((self.root / "boards" / board / "records").glob("2*"))


@pytest.fixture
def toy(tmp_path):
    """A `Toy` in its own HOME, apart from the fake clone of `bench_clone`."""
    home = tmp_path / "toy"
    home.mkdir()
    return Toy(home)


def test_two_boards_run_at_once_each_on_its_own_commit_file_and_record(toy):
    """Board b's whole run, on a newer commit, happens inside a's host suite.

    Seeds: one checkout for both boards; `--lg-env` given without `LG_ENV`; one records directory.
    """
    older = toy.bench("test", "--board", "a", "origin/HEAD").stdout.split()[2]
    git(toy.layer, "commit", "-q", "--allow-empty", "-m", "newer")
    newer = toy.bench("test", "--board", "b", "-C", str(toy.layer), "HEAD").stdout.split()[2]
    assert older != newer
    for name in ("a", "b"):
        toy.bench("on", name)
    toy.inside("a", "host", "run", "--board", "b")

    assert toy.bench("run", "--board", "a").returncode == 0

    names = ["precondition", "build", "bundle", "host", "target"]
    for board, sha in (("a", older), ("b", newer)):
        steps = toy.steps(board)
        assert [seen["step"] for seen in steps] == names
        assert {seen[key] for seen in steps for key in ("before", "after", "sha")} == {sha}
        assert all(seen["labgrid"] == {"LG_ENV": toy.lg_env(board)} for seen in steps)
        (record,) = toy.records(board)
        log = (record / "bench.log").read_text()
        assert log.endswith("run passed\n")
        for suite in ("host_suite", "target_suite"):
            run = json.loads((record / suite / "run.json").read_text())
            assert run["test_commit"] == sha
    assert not (toy.home / "systemctl").exists()
