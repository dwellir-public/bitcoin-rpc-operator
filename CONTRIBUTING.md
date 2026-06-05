# Contributing

To make contributions to this charm, you'll need a working [development setup](https://juju.is/docs/sdk/dev-setup).

The repo also contains the [bitcoin-rpc-proxy](./bitcoin-rpc-proxy/) Go service; working on that part requires a Go toolchain (version per its [go.mod](./bitcoin-rpc-proxy/go.mod)). See its [README](./bitcoin-rpc-proxy/README.md) and `make` targets (`build`, `fmt`, `lint`, `test`) for the development workflow.

You can create an environment for development with `tox`:

```shell
tox devenv -e integration
source venv/bin/activate
```

## Testing

This project uses `tox` for managing test environments. There are some pre-configured environments
that can be used for linting and formatting code when you're preparing contributions to the charm:

```shell
tox run -e format        # update your code according to linting rules
tox run -e lint          # code style
tox run -e static        # static type checking
tox run -e unit          # unit tests
tox run -e integration   # integration tests
tox                      # runs 'format', 'lint', 'static', and 'unit' environments
```

## Build the charm

Build the charm in this git repository using:

```shell
charmcraft pack
```
