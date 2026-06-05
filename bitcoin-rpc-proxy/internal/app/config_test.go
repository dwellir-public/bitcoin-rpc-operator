package app

import (
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestParseDefaults(t *testing.T) {
	cfg, ver, err := Parse(nil)
	require.NoError(t, err)
	assert.False(t, ver)
	assert.Equal(t, defaultListen, cfg.Listen)
	assert.Equal(t, defaultAdminListen, cfg.AdminListen)
	assert.Equal(t, defaultLogLevel, cfg.LogLevel)
	assert.Equal(t, defaultUpstreamURL, cfg.UpstreamURL)
	assert.Equal(t, defaultUpstreamTimeout, cfg.UpstreamTimeout)
	assert.Equal(t, int64(defaultMaxBodyBytes), cfg.MaxBodyBytes)
	assert.Empty(t, cfg.ExtendAllowlist)
	assert.True(t, cfg.Filter, "filtering is on by default")
}

func TestParseFilter(t *testing.T) {
	t.Run("flag", func(t *testing.T) {
		cfg, _, err := Parse([]string{"--filter=false"})
		require.NoError(t, err)
		assert.False(t, cfg.Filter)
	})
	t.Run("env", func(t *testing.T) {
		t.Setenv("PROXY_FILTER", "false")
		cfg, _, err := Parse(nil)
		require.NoError(t, err)
		assert.False(t, cfg.Filter)
	})
	t.Run("flag beats env", func(t *testing.T) {
		t.Setenv("PROXY_FILTER", "false")
		cfg, _, err := Parse([]string{"--filter=true"})
		require.NoError(t, err)
		assert.True(t, cfg.Filter)
	})
	t.Run("invalid env", func(t *testing.T) {
		t.Setenv("PROXY_FILTER", "notabool")
		_, _, err := Parse(nil)
		require.Error(t, err)
	})
}

func TestParseFlags(t *testing.T) {
	cfg, _, err := Parse([]string{
		"--listen=127.0.0.1:9", "--upstream-timeout=5s",
		"--max-body-bytes=1024", "--extend-allowlist=sendrawtransaction, getpeerinfo",
	})
	require.NoError(t, err)
	assert.Equal(t, "127.0.0.1:9", cfg.Listen)
	assert.Equal(t, 5*time.Second, cfg.UpstreamTimeout)
	assert.Equal(t, int64(1024), cfg.MaxBodyBytes)
	assert.Equal(t, []string{"sendrawtransaction", "getpeerinfo"}, cfg.ExtendAllowlist)
}

func TestParseEnvFallback(t *testing.T) {
	t.Setenv("PROXY_LISTEN", "0.0.0.0:1234")
	t.Setenv("PROXY_UPSTREAM_TIMEOUT", "12s")
	t.Setenv("PROXY_MAX_BODY_BYTES", "2048")
	t.Setenv("PROXY_EXTEND_ALLOWLIST", "getpeerinfo")

	cfg, _, err := Parse(nil)
	require.NoError(t, err)
	assert.Equal(t, "0.0.0.0:1234", cfg.Listen)
	assert.Equal(t, 12*time.Second, cfg.UpstreamTimeout)
	assert.Equal(t, int64(2048), cfg.MaxBodyBytes)
	assert.Equal(t, []string{"getpeerinfo"}, cfg.ExtendAllowlist)
}

func TestParseUpstreamAuth(t *testing.T) {
	t.Run("flags", func(t *testing.T) {
		cfg, _, err := Parse([]string{"--upstream-user=rpc", "--upstream-password=secret"})
		require.NoError(t, err)
		assert.Equal(t, "rpc", cfg.UpstreamUser)
		assert.Equal(t, "secret", cfg.UpstreamPassword)
	})
	t.Run("env", func(t *testing.T) {
		t.Setenv("PROXY_UPSTREAM_USER", "rpc")
		t.Setenv("PROXY_UPSTREAM_PASSWORD", "secret")
		cfg, _, err := Parse(nil)
		require.NoError(t, err)
		assert.Equal(t, "rpc", cfg.UpstreamUser)
		assert.Equal(t, "secret", cfg.UpstreamPassword)
	})
	t.Run("empty by default", func(t *testing.T) {
		cfg, _, err := Parse(nil)
		require.NoError(t, err)
		assert.Empty(t, cfg.UpstreamUser)
		assert.Empty(t, cfg.UpstreamPassword)
	})
}

func TestParseFlagBeatsEnv(t *testing.T) {
	t.Setenv("PROXY_LISTEN", "0.0.0.0:1234")
	cfg, _, err := Parse([]string{"--listen=127.0.0.1:9"})
	require.NoError(t, err)
	assert.Equal(t, "127.0.0.1:9", cfg.Listen, "explicit flag must override env")
}

func TestParseLogLevelEnvPriority(t *testing.T) {
	t.Setenv("LOG_LEVEL", "warn")
	t.Setenv("PROXY_LOG_LEVEL", "debug")
	cfg, _, err := Parse(nil)
	require.NoError(t, err)
	assert.Equal(t, "debug", cfg.LogLevel, "PROXY_LOG_LEVEL takes priority over LOG_LEVEL")
}

func TestParseInvalidEnvValues(t *testing.T) {
	t.Run("duration", func(t *testing.T) {
		t.Setenv("PROXY_UPSTREAM_TIMEOUT", "notaduration")
		_, _, err := Parse(nil)
		assert.Error(t, err)
	})
	t.Run("int", func(t *testing.T) {
		t.Setenv("PROXY_MAX_BODY_BYTES", "notanint")
		_, _, err := Parse(nil)
		assert.Error(t, err)
	})
}

func TestParseValidationErrors(t *testing.T) {
	cases := [][]string{
		{"--upstream-timeout=0"},
		{"--max-body-bytes=0"},
		{"--upstream-url=notaurl"},
	}
	for _, args := range cases {
		t.Run(args[0], func(t *testing.T) {
			_, _, err := Parse(args)
			assert.Error(t, err)
		})
	}
}

func TestParseVersionSkipsValidation(t *testing.T) {
	cfg, ver, err := Parse([]string{"--version", "--max-body-bytes=0"})
	require.NoError(t, err)
	assert.True(t, ver)
	assert.NotNil(t, cfg)
}
