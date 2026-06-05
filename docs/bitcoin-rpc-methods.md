# Bitcoin Core JSON-RPC Methods & Risk Classification

| Field | Value |
|---|---|
| Document date | 2026-06-03 |
| Repo commit | `9724fd1` (`9724fd144a36d4d2c898fc803099b8774ad3e016`) |
| Bitcoin Core version anchor | **31.0** (released 2026-04-19) |
| Charm | `bitcoin-operator` (Juju machine charm) |

> Anchored to Bitcoin Core 31.0, the latest stable at the time of writing. The charm
> installs whatever `version` config the operator sets (the README example pins `31.0`).
> If you deploy a different version, regenerate the authoritative list from a live node:
> `bitcoin-cli help` and per-command `bitcoin-cli help <method>`. Method names are
> **case-sensitive**.

## 1. Purpose

This node is operated as a validating/relay node. RPC may be reached by the local
Prometheus exporter and, depending on `service-args` (`-rpcbind`, `-rpcallowip`), by
other hosts. Bitcoin Core's RPC surface includes methods that can shut down the node,
manipulate its view of the chain, isolate it on the network, exfiltrate private keys, or
exhaust resources. This document enumerates the full method set and classifies each by
risk so we can build a default-deny filter (see the
[`bitcoin-rpc-proxy` README](../bitcoin-rpc-proxy/README.md)).

## 2. Native access controls (context)

Bitcoin Core ships some access control, summarized here because the proxy design builds on it:

- **`-rpcwhitelist=<user>:<m1>,<m2>,...`** + **`-rpcwhitelistdefault=1`** — the only native
  *per-method* filter. With `rpcwhitelistdefault=1`, any RPC user without an explicit
  whitelist is denied **all** methods (deny-by-default). Multiple entries for one user
  intersect (most restrictive wins). Filtering is **name-based only**: it does not inspect
  arguments, so a permitted `dumpwallet`/`loadwallet` can still touch any path the daemon
  can reach. The docs explicitly state these are *not* robust security boundaries.
- **`-rpcauth=<user>:<salt>$<hash>`** (repeatable, multi-user) / `-rpcuser`+`-rpcpassword`
  (single user) / cookie auth (default). Whitelists key off the RPC username.
- **`-rpcallowip`** (source-IP allow) and **`-rpcbind`** (listen interface; ignored unless
  `-rpcallowip` is set). RPC has **no transport encryption** — never expose to the public
  internet without a VPN/SSH tunnel/stunnel.
- **`-disablewallet`** — runs the node with **no wallet**; the entire Wallet category errors
  out. This is the cleanest way to remove the highest-risk (key/fund) surface on a node that
  does not need a wallet. Block validation, mempool, networking, and mining (incl.
  `getblocktemplate`) still work.

Native whitelisting is a strong first layer but lacks: argument inspection, per-method rate
limits, batch-request control, structured logging/metrics of denials, and decoupling from
bitcoind restarts. The proxy design addresses those gaps.

## 3. Risk tiers

| Tier | Meaning | Default posture for untrusted/remote callers |
|---|---|---|
| **SAFE** | Read-only, bounded cost, no key access | Allow |
| **CAUTION** | Read-only but expensive (DoS) or leaks operational info | Allow only with rate limits / for trusted callers |
| **DANGEROUS** | Mutates node/network/chain state, or broadcasts | Deny by default |
| **CRITICAL** | Key exposure, fund movement, or chain-view manipulation | Always deny (and prefer `-disablewallet`) |

## 4. Methods by category

31.0 deltas folded in: `settxfee` removed; `getmempoolcluster`, `getmempoolfeeratediagram`,
`getprivatebroadcastinfo`, `abortprivatebroadcast` added.

### Blockchain

| Method | Tier | Notes |
|---|---|---|
| `getbestblockhash` | SAFE | Tip hash. |
| `getblock` | SAFE | High verbosity returns large payloads. |
| `getblockchaininfo` | SAFE | |
| `getblockcount` | SAFE | |
| `getblockfilter` | SAFE | Requires `-blockfilterindex`. |
| `getblockfrompeer` | DANGEROUS | Directs node to fetch a block from a chosen peer. |
| `getblockhash` | SAFE | |
| `getblockheader` | SAFE | |
| `getblockstats` | CAUTION | Per-block stats; can be heavy over wide ranges. |
| `getchaintips` | SAFE | |
| `getchaintxstats` | SAFE | |
| `getdeploymentinfo` | SAFE | |
| `getdifficulty` | SAFE | |
| `getmempoolancestors` | SAFE | |
| `getmempoolcluster` | CAUTION | 31.0; cluster mempool query. |
| `getmempooldescendants` | SAFE | |
| `getmempoolentry` | SAFE | |
| `getmempoolfeeratediagram` | CAUTION | 31.0; whole-mempool feerate diagram. |
| `getmempoolinfo` | SAFE | |
| `getrawmempool` | CAUTION | `verbose=true` over a large mempool is expensive. |
| `gettxout` | SAFE | |
| `gettxoutproof` | CAUTION | |
| `gettxoutsetinfo` | CAUTION | **Full UTXO-set scan; very expensive** — classic DoS lever. |
| `gettxspendingprevout` | CAUTION | Mempool scan. |
| `preciousblock` | DANGEROUS | Biases tip selection. |
| `pruneblockchain` | DANGEROUS | Irreversibly prunes block data. |
| `savemempool` | DANGEROUS | Writes mempool to disk (IO). |
| `scantxoutset` | CAUTION | **UTXO-set scan against descriptors; CPU/IO heavy** — DoS lever. |
| `verifychain` | CAUTION | Re-verifies chain DB; expensive/blocking. |
| `verifytxoutproof` | SAFE | |

### Control

| Method | Tier | Notes |
|---|---|---|
| `getmemoryinfo` | SAFE | Operational info. |
| `getrpcinfo` | CAUTION | Reveals active commands and log path. |
| `help` | SAFE | |
| `logging` | DANGEROUS | Changes log categories at runtime (disable audit logs / flood disk). |
| `stop` | DANGEROUS | **Shuts the daemon down** — denial of service. |
| `uptime` | SAFE | |

### Generating (regtest/test-oriented)

| Method | Tier | Notes |
|---|---|---|
| `generateblock` | DANGEROUS | Mining; meaningful mainly on regtest. |
| `generatetoaddress` | DANGEROUS | |
| `generatetodescriptor` | DANGEROUS | |

### Mining

| Method | Tier | Notes |
|---|---|---|
| `getblocktemplate` | CAUTION | Expensive; needed only by miners. |
| `getmininginfo` | SAFE | |
| `getnetworkhashps` | SAFE | |
| `prioritisetransaction` | DANGEROUS | Alters local block-template selection. |
| `submitblock` | DANGEROUS | Injects a block. |
| `submitheader` | DANGEROUS | Injects a header. |

### Network

| Method | Tier | Notes |
|---|---|---|
| `addnode` | DANGEROUS | Alters peer connections (eclipse risk). |
| `clearbanned` | DANGEROUS | Clears ban list. |
| `disconnectnode` | DANGEROUS | Drops a peer. |
| `getaddednodeinfo` | CAUTION | Reveals peer topology. |
| `getconnectioncount` | SAFE | |
| `getnettotals` | SAFE | |
| `getnetworkinfo` | CAUTION | May leak local/external addresses. |
| `getnodeaddresses` | CAUTION | |
| `getpeerinfo` | CAUTION | Reveals peer IPs/topology. |
| `listbanned` | CAUTION | |
| `ping` | SAFE | |
| `setban` | DANGEROUS | Manipulates ban list (isolate node / unban attacker). |
| `setnetworkactive` | DANGEROUS | **Disables all P2P networking** — partition/DoS. |

### Rawtransactions

| Method | Tier | Notes |
|---|---|---|
| `analyzepsbt` | SAFE | |
| `combinepsbt` | SAFE | |
| `combinerawtransaction` | SAFE | |
| `converttopsbt` | SAFE | |
| `createpsbt` | SAFE | Local construction; no broadcast. |
| `createrawtransaction` | SAFE | Local construction; no broadcast. |
| `decodepsbt` | SAFE | |
| `decoderawtransaction` | SAFE | |
| `decodescript` | SAFE | |
| `finalizepsbt` | SAFE | |
| `fundrawtransaction` | CAUTION | Wallet-funded; touches wallet UTXOs. |
| `getrawtransaction` | SAFE | |
| `joinpsbts` | SAFE | |
| `sendrawtransaction` | DANGEROUS | **Broadcasts an arbitrary transaction to the network.** |
| `signrawtransactionwithkey` | CRITICAL | **Signing oracle with supplied private keys.** |
| `testmempoolaccept` | SAFE | Dry-run; no broadcast. |
| `utxoupdatepsbt` | SAFE | |
| `abortprivatebroadcast` | DANGEROUS | 31.0; mutates private broadcast queue. |
| `getprivatebroadcastinfo` | CAUTION | 31.0; reveals queued private broadcasts. |

### Signer

| Method | Tier | Notes |
|---|---|---|
| `enumeratesigners` | CAUTION | Lists external signers. |

### Util

| Method | Tier | Notes |
|---|---|---|
| `createmultisig` | SAFE | No wallet needed. |
| `deriveaddresses` | SAFE | |
| `estimaterawfee` | SAFE | |
| `estimatesmartfee` | SAFE | |
| `getdescriptorinfo` | SAFE | |
| `getindexinfo` | SAFE | |
| `signmessagewithprivkey` | CRITICAL | **Signs with a supplied private key.** |
| `validateaddress` | SAFE | |
| `verifymessage` | SAFE | |

### Wallet

Entire category is **CRITICAL/DANGEROUS** for untrusted exposure. On a node that does not
need a wallet, run with `-disablewallet` to remove the category entirely. Key-exposure and
fund-movement methods are CRITICAL; the rest are DANGEROUS (state change / info).

| Method | Tier | Notes |
|---|---|---|
| `abandontransaction` | DANGEROUS | |
| `abortrescan` | DANGEROUS | |
| `addmultisigaddress` | DANGEROUS | |
| `backupwallet` | CRITICAL | Writes wallet file to a server path. |
| `bumpfee` | CRITICAL | Moves funds (RBF). |
| `createwallet` | DANGEROUS | |
| `dumpprivkey` | CRITICAL | **Reveals a private key.** |
| `dumpwallet` | CRITICAL | **Dumps all keys to a server file.** |
| `encryptwallet` | CRITICAL | |
| `getaddressesbylabel` | CAUTION | Wallet info leak. |
| `getaddressinfo` | CAUTION | |
| `getbalance` | CAUTION | Wallet info leak. |
| `getbalances` | CAUTION | |
| `getnewaddress` | DANGEROUS | Mutates keypool. |
| `getrawchangeaddress` | DANGEROUS | |
| `getreceivedbyaddress` | CAUTION | |
| `getreceivedbylabel` | CAUTION | |
| `gettransaction` | CAUTION | |
| `getwalletinfo` | CAUTION | |
| `importaddress` | DANGEROUS | May trigger rescan. |
| `importdescriptors` | CRITICAL | Imports keys; heavy rescan. |
| `importmulti` | CRITICAL | Imports keys; heavy rescan. |
| `importprivkey` | CRITICAL | **Adds a private key**; rescan. |
| `importprunedfunds` | DANGEROUS | |
| `importpubkey` | DANGEROUS | |
| `importwallet` | CRITICAL | **Imports keys from dump file.** |
| `keypoolrefill` | DANGEROUS | |
| `listaddressgroupings` | CAUTION | |
| `listdescriptors` | CRITICAL | Can reveal private descriptors (with `true`). |
| `listlabels` | CAUTION | |
| `listlockunspent` | CAUTION | |
| `listreceivedbyaddress` | CAUTION | |
| `listreceivedbylabel` | CAUTION | |
| `listsinceblock` | CAUTION | |
| `listtransactions` | CAUTION | |
| `listunspent` | CAUTION | |
| `listwalletdir` | CAUTION | |
| `listwallets` | CAUTION | |
| `loadwallet` | DANGEROUS | Touches server filesystem paths. |
| `lockunspent` | DANGEROUS | |
| `migratewallet` | DANGEROUS | |
| `newkeypool` | DANGEROUS | |
| `psbtbumpfee` | CRITICAL | Fund movement (PSBT). |
| `removeprunedfunds` | DANGEROUS | |
| `rescanblockchain` | CAUTION | **Long blocking rescan** — DoS lever. |
| `restorewallet` | DANGEROUS | |
| `send` | CRITICAL | Moves funds. |
| `sendall` | CRITICAL | **Spends all confirmed UTXOs.** |
| `sendmany` | CRITICAL | Moves funds. |
| `sendtoaddress` | CRITICAL | Moves funds. |
| `sethdseed` | CRITICAL | **Sets a new HD seed.** |
| `setlabel` | DANGEROUS | |
| `setwalletflag` | DANGEROUS | |
| `signmessage` | DANGEROUS | Signing oracle (wallet key). |
| `signrawtransactionwithwallet` | CRITICAL | Signing oracle (wallet keys). |
| `simulaterawtransaction` | CAUTION | |
| `unloadwallet` | DANGEROUS | |
| `upgradewallet` | DANGEROUS | |
| `walletcreatefundedpsbt` | CRITICAL | Funds a PSBT from wallet. |
| `walletdisplayaddress` | DANGEROUS | |
| `walletlock` | DANGEROUS | |
| `walletpassphrase` | CRITICAL | **Holds decryption key in memory.** |
| `walletpassphrasechange` | CRITICAL | |
| `walletprocesspsbt` | CRITICAL | Signs PSBT with wallet. |

### Zmq

| Method | Tier | Notes |
|---|---|---|
| `getzmqnotifications` | CAUTION | Reveals ZMQ endpoints. |

## 5. Recommended baseline allowlist

For a validating/relay node feeding read-only consumers (explorers, monitoring, fee
estimation, broadcast-only services), allow **only** SAFE methods, then add CAUTION methods
case-by-case behind rate limits. Suggested default allowlist (no wallet, no control, no
network mutation):

This list is exactly the **SAFE** tier from the tables above (47 methods), and is the source
of truth for the proxy's hardcoded default allowlist — a unit test in `internal/policy`
parses this table and asserts the two stay in sync.

```
getbestblockhash getblock getblockchaininfo getblockcount getblockfilter
getblockhash getblockheader getchaintips getchaintxstats getdeploymentinfo
getdifficulty getmempoolancestors getmempooldescendants getmempoolentry
getmempoolinfo gettxout verifytxoutproof getmemoryinfo help uptime
getmininginfo getnetworkhashps getconnectioncount getnettotals ping
analyzepsbt combinepsbt combinerawtransaction converttopsbt createpsbt
createrawtransaction decodepsbt decoderawtransaction decodescript finalizepsbt
getrawtransaction joinpsbts testmempoolaccept utxoupdatepsbt createmultisig
deriveaddresses estimaterawfee estimatesmartfee getdescriptorinfo getindexinfo
validateaddress verifymessage
```

Add to the allowlist only when a consumer needs it (via `--extend-allowlist`):
- `sendrawtransaction` — broadcast services (DANGEROUS; consider a dedicated, rate-limited path).
- `getblockstats`, `gettxoutproof`, `getrawmempool`, `gettxoutsetinfo`, `scantxoutset`, `getblocktemplate` — heavy/CAUTION; behind strict rate limits and trusted callers only.
- `getpeerinfo`, `getnetworkinfo`, `getnodeaddresses` — operational/topology leak; trusted callers only.

Everything not on the allowlist is denied. This is enforced both by Bitcoin Core's
`-rpcwhitelistdefault=1` (first layer) and by the filtering proxy (second layer); see the
design document.
