# Hedgehog metadata

The charm collects metadata after all three services pass health checks.
Collection runs during `update-status`, `config-changed`, and `upgrade-charm`.
The `upgrade-charm` hook does not change workload files or services.

## Collected fields

The standard blockchain section includes these values:

- ecosystem, network, client name, and client version;
- binary path and redacted effective command line;
- the block-zero genesis hash.

The Bitcoin section includes these values:

- chain name, expected network magic, and protocol version;
- block height, header height, best block hash, and initial-download state;
- effective non-secret flags;
- pruning mode, target, and height;
- index names, sync state, and indexed height;
- peer-to-peer, internal RPC, proxy, and ZeroMQ ports.

The resource section includes systemd memory, CPU, and task limits.
Unlimited values become JSON `null` values.

Juju topology, application config, and relation data use the common schema.
The charm recursively redacts those values before serialization.

## Credential handling

`collector-s3-credentials` accepts a Juju secret reference.
The referenced secret requires these keys:

- `bucket`
- `region`
- `access-key-id`
- `secret-access-key`

It accepts `endpoint-url`, `key-prefix`, and `session-token` as optional keys.
The payload never contains the secret reference or secret contents.

The redactor removes credentials from nested config and relation values.
It also removes credentials from URLs, command lines, headers, and environment text.
Collection errors pass through the same redactor.

## Operator verification

Use a non-production model for the first verification.

```bash
juju run bitcoin-rpc/0 get-node-info
juju ssh bitcoin-rpc/0 -- sudo jq . /tmp/dwellir-metadata-uploader/bitcoin-rpc-0.json
```

Confirm the network and genesis hash before routing traffic.
Confirm pruning and index settings match the endpoint contract.
Compare effective flags and ports across every backend.
Compare systemd limits where the host exposes finite limits.

Never paste the payload into a ticket before checking redaction.

## Homogeneity checks

Hedgehog should compare stable configuration fields across a backend pool.
Block height and best block hash are observations, not drift inputs.

Use these fields as hard drift signals:

- genesis hash and chain name;
- client family and supported major version;
- pruning mode and index availability;
- effective flags, excluding names and instance labels;
- RPC, peer-to-peer, proxy, and ZeroMQ ports;
- finite memory, CPU, and task limits.

Use these fields as health signals:

- initial block download state;
- index sync state;
- block and header height;
- service health and metadata collection age.
