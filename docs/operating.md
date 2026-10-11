# Operating the runner

How the runner is installed and run against a layer's suites. What the runner is and the layer
contract are in the [README](../README.md); the design is ADR 0001 rev 2.

## Install at a tag

```sh
git clone https://github.com/WisewareOrg/WiseBenchManager.git
git -C WiseBenchManager checkout <tag>
uv sync --project WiseBenchManager --frozen
```

`uv sync --frozen` installs exactly the versions in `uv.lock`. The package's version is derived from
the checked-out tag by hatch-vcs when uv builds it; the run-level file carries that version.

## Where results go

`BENCH_RESULT_DIR` names the directory a run's records land under; it defaults to `records/` under
the directory pytest is started in. Each run gets one subdirectory, named for its UTC start time,
holding pytest-reportlog's `report.jsonl` and the runner's own `run.json`.

## The board

Three facts from labgrid 26.0 are the contract between this runner and the layer's env file:

- The file's path comes from `--lg-env <path>` or the `LG_ENV` variable
  (`labgrid/pytestplugin/hooks.py`); no `-p` flag of ours is needed alongside it.
- `!template` substitutes only `LG_`-prefixed environment variables (`labgrid/config.py`), so the
  board's address is named `LG_BENCH_ADDRESS`:

  ```yaml
  targets:
    main:
      drivers:
        SSHDriver:
          address: !template $LG_BENCH_ADDRESS
  ```

- The `target` fixture requires a target named `main` in that file
  (`labgrid/pytestplugin/fixtures.py`); a config without one raises `UserError`.

That env file, and everything else naming this board, lives in the layer, not here.
