package policy

import (
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestAllowedBaseline(t *testing.T) {
	a := NewAllowlist(nil)
	for _, m := range BaselineMethods() {
		assert.True(t, a.Allowed(m), "baseline method should be allowed: %s", m)
	}
	dangerous := []string{"stop", "sendrawtransaction", "dumpprivkey", "setban", "importprivkey"}
	for _, m := range dangerous {
		assert.False(t, a.Allowed(m), "non-baseline method should be denied: %s", m)
	}
}

func TestAllowedCaseSensitive(t *testing.T) {
	a := NewAllowlist(nil)
	assert.True(t, a.Allowed("getblockcount"))
	assert.False(t, a.Allowed("GetBlockCount"))
	assert.False(t, a.Allowed("GETBLOCKCOUNT"))
}

func TestExtendAllowlist(t *testing.T) {
	a := NewAllowlist([]string{"sendrawtransaction", "  getpeerinfo  ", "", "getblockcount"})
	assert.True(t, a.Allowed("sendrawtransaction"), "extend should add the method")
	assert.True(t, a.Allowed("getpeerinfo"), "extend entries should be trimmed")
	assert.True(t, a.Allowed("getblockcount"), "extend overlapping baseline stays allowed")
	assert.True(t, a.Allowed("getblock"), "extend must not drop baseline methods")
	assert.False(t, a.Allowed(""), "empty extend entry must not be allowed")
}

func TestParseExtendList(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want []string
	}{
		{"empty", "", nil},
		{"separators only", "  ,  , ", nil},
		{"single", "sendrawtransaction", []string{"sendrawtransaction"}},
		{"trim and drop empties", " a , ,b,  c  ", []string{"a", "b", "c"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := ParseExtendList(tc.in)
			if len(tc.want) == 0 {
				assert.Empty(t, got)
				return
			}
			assert.Equal(t, tc.want, got)
		})
	}
}

func TestMethodsSorted(t *testing.T) {
	a := NewAllowlist([]string{"zzz", "aaa"})
	got := a.Methods()
	assert.True(t, slices.IsSorted(got), "Methods() must be sorted")
	assert.Len(t, got, len(BaselineMethods())+2)
}

// TestBaselineMatchesSafeTier asserts the hardcoded baseline equals exactly the
// SAFE-tier methods documented in docs/bitcoin-rpc-methods.md, so the two never
// drift. Parses every table row whose tier column is a known risk tier.
func TestBaselineMatchesSafeTier(t *testing.T) {
	tiers := map[string]struct{}{
		"SAFE": {}, "CAUTION": {}, "DANGEROUS": {}, "CRITICAL": {},
	}

	data, err := os.ReadFile(methodsDocPath(t))
	require.NoError(t, err)

	var safe []string
	for _, line := range strings.Split(string(data), "\n") {
		if !strings.HasPrefix(strings.TrimSpace(line), "|") {
			continue
		}
		cells := strings.Split(line, "|")
		if len(cells) < 4 {
			continue
		}
		method := strings.Trim(strings.TrimSpace(cells[1]), "`")
		tier := strings.TrimSpace(cells[2])
		if _, ok := tiers[tier]; !ok {
			continue
		}
		if method == "" || strings.ContainsAny(method, " *`") {
			continue
		}
		if tier == "SAFE" {
			safe = append(safe, method)
		}
	}

	require.NotEmpty(t, safe, "parsed no SAFE methods; doc table format may have changed")
	assert.ElementsMatch(t, BaselineMethods(), safe,
		"policy baseline and docs/bitcoin-rpc-methods.md SAFE tier are out of sync")
}

func methodsDocPath(t *testing.T) string {
	t.Helper()
	_, thisFile, _, ok := runtime.Caller(0)
	require.True(t, ok)
	// .../bitcoin-rpc-proxy/internal/policy/ -> repo root is three directories up.
	repoRoot := filepath.Join(filepath.Dir(thisFile), "..", "..", "..")
	return filepath.Join(repoRoot, "docs", "bitcoin-rpc-methods.md")
}
