// Package policy decides which Bitcoin Core JSON-RPC methods the proxy
// forwards. The default-allow set is the SAFE tier from
// docs/bitcoin-rpc-methods.md; operators widen it via --extend-allowlist.
package policy

import (
	"slices"
	"strings"
)

// baseline is the default-allow set: exactly the SAFE-tier methods from
// docs/bitcoin-rpc-methods.md. TestBaselineMatchesSafeTier keeps the two in
// sync; update both together.
var baseline = []string{
	// Blockchain
	"getbestblockhash",
	"getblock",
	"getblockchaininfo",
	"getblockcount",
	"getblockfilter",
	"getblockhash",
	"getblockheader",
	"getchaintips",
	"getchaintxstats",
	"getdeploymentinfo",
	"getdifficulty",
	"getmempoolancestors",
	"getmempooldescendants",
	"getmempoolentry",
	"getmempoolinfo",
	"gettxout",
	"verifytxoutproof",
	// Control
	"getmemoryinfo",
	"help",
	"uptime",
	// Mining
	"getmininginfo",
	"getnetworkhashps",
	// Network
	"getconnectioncount",
	"getnettotals",
	"ping",
	// Rawtransactions
	"analyzepsbt",
	"combinepsbt",
	"combinerawtransaction",
	"converttopsbt",
	"createpsbt",
	"createrawtransaction",
	"decodepsbt",
	"decoderawtransaction",
	"decodescript",
	"finalizepsbt",
	"getrawtransaction",
	"joinpsbts",
	"testmempoolaccept",
	"utxoupdatepsbt",
	// Util
	"createmultisig",
	"deriveaddresses",
	"estimaterawfee",
	"estimatesmartfee",
	"getdescriptorinfo",
	"getindexinfo",
	"validateaddress",
	"verifymessage",
}

// Allowlist is an immutable set of permitted JSON-RPC method names.
type Allowlist struct {
	allowed map[string]struct{}
}

// BaselineMethods returns a copy of the SAFE-tier baseline method names.
func BaselineMethods() []string {
	out := make([]string, len(baseline))
	copy(out, baseline)
	return out
}

// ParseExtendList splits a comma-separated method list (e.g. the value of
// --extend-allowlist) into trimmed, non-empty entries.
func ParseExtendList(csv string) []string {
	parts := strings.Split(csv, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// NewAllowlist returns an Allowlist of the baseline plus any extend entries.
// extend entries are trimmed; empty entries are ignored; duplicates collapse.
// Matching is case-sensitive, mirroring bitcoind.
func NewAllowlist(extend []string) *Allowlist {
	a := &Allowlist{allowed: make(map[string]struct{}, len(baseline)+len(extend))}
	for _, m := range baseline {
		a.allowed[m] = struct{}{}
	}
	for _, m := range extend {
		if m = strings.TrimSpace(m); m != "" {
			a.allowed[m] = struct{}{}
		}
	}
	return a
}

// Allowed reports whether method is permitted. Matching is case-sensitive.
func (a *Allowlist) Allowed(method string) bool {
	_, ok := a.allowed[method]
	return ok
}

// Methods returns the sorted method names currently in the allowlist.
func (a *Allowlist) Methods() []string {
	out := make([]string, 0, len(a.allowed))
	for m := range a.allowed {
		out = append(out, m)
	}
	slices.Sort(out)
	return out
}
