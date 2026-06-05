# TODO

## Research

### Network switching: fallout beyond the RPC port

The charm pins bitcoind's RPC endpoint unconditionally: `harden_service_args`
(`src/utils.py`) forces `-rpcbind` to loopback and `-rpcport` to
`BITCOIND_RPC_PORT` (`src/constants.py`), and both the proxy upstream
(`PROXY_UPSTREAM_URL`) and the monitor env derive from that same constant. So
selecting a non-mainnet network via `service-args` (`-testnet`/`-signet`/
`-regtest`) no longer moves the RPC port out from under the proxy and monitor —
but the rest of the switch is unhandled and untested.

Research what should happen when the user wants to switch networks:

- What is the full fallout of a network switch? The P2P port changes, the data
  directory gains a per-network subdirectory (separate chain state), and the
  proxy allowlist / prometheus wiring may carry mainnet assumptions.
- Is switching networks on a live unit even a supported workflow, or should it
  be a fresh deploy? Decide the intended UX before building plumbing.
- Verify end-to-end on at least one non-mainnet network (`-signet` is the
  cheapest) before declaring support.
- Until resolved: the mainnet-only assumption is documented as a known
  limitation in `README.md`.
- Final step, once resolved: update the docs — remove or rewrite the
  "Known limitations" entry in `README.md`, and revise the `service-args`
  config description in `charmcraft.yaml` if network selection becomes
  supported.
