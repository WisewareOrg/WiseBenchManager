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

The board is reached through labgrid's own pytest plugin, pointed at an env file with `--lg-env
<path>` or the `LG_ENV` variable; no `-p` flag of ours is needed alongside it. labgrid's YAML loader
resolves an `!template` scalar against the OS environment, but only for variable names starting with
`LG_` (`labgrid.util.yaml.resolve_templates`, called from `labgrid.Config`), so an env file names the
board's address as:

```yaml
targets:
  main:
    resources:
      RawNetworkPort: {}
    drivers:
      SSHDriver:
        address: !template $LG_ADDRESS
```

with `LG_ADDRESS` set in the environment that invokes pytest. That env file, and everything else
naming this board, lives in the layer, not here.
