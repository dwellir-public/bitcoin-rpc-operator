# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## What this is

A [Juju](https://juju.is/) machine charm (Python, `ops` framework) that installs and operates a Bitcoin Core full node as the `bitcoind` systemd service, plus a `bitcoind-monitor` Prometheus exporter service. It is a machine charm: it runs directly on an Ubuntu 24.04 host and drives it via `apt`, `pip`, `systemctl`, and shelling out to system tools.

## Commands

```bash
tox -e format      # ruff format + ruff check --fix
tox -e lint        # codespell + ruff check + ruff format --check
tox -e static      # pyright type checks
tox -e unit        # pytest + coverage on tests/unit
tox -e unit -- -k test_name   # single unit test (posargs pass through)
tox -e integration            # pytest-operator, needs a live Juju controller

charmcraft pack    # build the .charm artifact (build-on Ubuntu 24.04)
```

`PYTHONPATH` for tests/runtime is `lib:src` (set by tox); `src/` modules import each other flat (`import constants as c`, `import utils`), not as a package.

## Working rules

- This repo is the standalone source of truth for the bitcoin charm; make changes here.
- Prefer minimal, tested changes over broad charm redesign.
- Keep chain-specific runtime behavior in config and `service-args`. Only add charm logic when a real deployment blocker requires it.
- Before a release build (`charmcraft pack`), run focused unit tests for the two risk areas: binary download/extraction (`utils.install_bitcoin`) and service-arg handling (`utils.update_service_args`).
- `charmcraft pack` builds on Ubuntu 24.04. From a non-Ubuntu host, run it inside an Ubuntu 24.04 container or VM.

## Conventions

- ruff: line-length 120 (E501 ignored), pydocstyle (`D`) enforced on `src`; tests exempt from docstring rules. All public functions need docstrings.
- Keep `charm.py` declarative; put anything that touches the host in `utils.py`.
