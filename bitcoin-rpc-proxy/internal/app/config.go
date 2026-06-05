// Package app wires configuration, components, and lifecycle for the proxy.
package app

import (
	"errors"
	"flag"
	"fmt"
	"net/url"
	"os"
	"strconv"
	"time"

	"github.com/dwellir-public/bitcoin-rpc-operator/bitcoin-rpc-proxy/internal/policy"
)

const (
	defaultListen          = "0.0.0.0:8331"
	defaultAdminListen     = "127.0.0.1:8360"
	defaultLogLevel        = "info"
	defaultUpstreamURL     = "http://127.0.0.1:8332"
	defaultUpstreamTimeout = 30 * time.Second
	defaultMaxBodyBytes    = 262144
)

// Config is the proxy's resolved configuration.
type Config struct {
	Listen           string
	AdminListen      string
	LogLevel         string
	UpstreamURL      string
	UpstreamTimeout  time.Duration
	UpstreamUser     string
	UpstreamPassword string
	MaxBodyBytes     int64
	ExtendAllowlist  []string
	Filter           bool
}

// Parse resolves configuration from CLI args with a per-flag PROXY_* env
// fallback (precedence: default -> env var -> flag). It returns the config and
// whether --version was requested. Validation is skipped when --version is set.
func Parse(args []string) (cfg *Config, printVersion bool, err error) {
	fs := flag.NewFlagSet("bitcoin-rpc-proxy", flag.ContinueOnError)
	listen := fs.String("listen", defaultListen, "main JSON-RPC listen address")
	adminListen := fs.String("admin-listen", defaultAdminListen, "admin (health/metrics) listen address")
	logLevel := fs.String("log-level", defaultLogLevel, "log level (debug, info, warn, error)")
	upstreamURL := fs.String("upstream-url", defaultUpstreamURL, "bitcoind JSON-RPC base URL")
	upstreamTimeout := fs.Duration("upstream-timeout", defaultUpstreamTimeout, "per-request upstream timeout")
	upstreamUser := fs.String("upstream-user", "", "health-probe bitcoind auth user (probe only)")
	upstreamPassword := fs.String("upstream-password", "", "health-probe bitcoind auth password")
	maxBody := fs.Int64("max-body-bytes", defaultMaxBodyBytes, "maximum request body size in bytes")
	extend := fs.String("extend-allowlist", "", "comma-separated methods to add to the SAFE allowlist")
	filter := fs.Bool("filter", true, "enforce the method allowlist; false forwards every method")
	version := fs.Bool("version", false, "print version and exit")

	if err := fs.Parse(args); err != nil {
		return nil, false, err
	}

	set := map[string]bool{}
	fs.Visit(func(f *flag.Flag) { set[f.Name] = true })

	applyEnvString(set, "listen", "PROXY_LISTEN", listen)
	applyEnvString(set, "admin-listen", "PROXY_ADMIN_LISTEN", adminListen)
	applyEnvLogLevel(set, logLevel)
	applyEnvString(set, "upstream-url", "PROXY_UPSTREAM_URL", upstreamURL)
	if err := applyEnvDuration(set, "upstream-timeout", "PROXY_UPSTREAM_TIMEOUT", upstreamTimeout); err != nil {
		return nil, false, err
	}
	applyEnvString(set, "upstream-user", "PROXY_UPSTREAM_USER", upstreamUser)
	applyEnvString(set, "upstream-password", "PROXY_UPSTREAM_PASSWORD", upstreamPassword)
	if err := applyEnvInt64(set, "max-body-bytes", "PROXY_MAX_BODY_BYTES", maxBody); err != nil {
		return nil, false, err
	}
	applyEnvString(set, "extend-allowlist", "PROXY_EXTEND_ALLOWLIST", extend)
	if err := applyEnvBool(set, "filter", "PROXY_FILTER", filter); err != nil {
		return nil, false, err
	}

	cfg = &Config{
		Listen:           *listen,
		AdminListen:      *adminListen,
		LogLevel:         *logLevel,
		UpstreamURL:      *upstreamURL,
		UpstreamTimeout:  *upstreamTimeout,
		UpstreamUser:     *upstreamUser,
		UpstreamPassword: *upstreamPassword,
		MaxBodyBytes:     *maxBody,
		ExtendAllowlist:  policy.ParseExtendList(*extend),
		Filter:           *filter,
	}

	if *version {
		return cfg, true, nil
	}
	if err := cfg.validate(); err != nil {
		return nil, false, err
	}
	return cfg, false, nil
}

func (c *Config) validate() error {
	if c.Listen == "" {
		return errors.New("listen address must not be empty")
	}
	if c.AdminListen == "" {
		return errors.New("admin-listen address must not be empty")
	}
	if c.UpstreamTimeout <= 0 {
		return errors.New("upstream-timeout must be positive")
	}
	if c.MaxBodyBytes <= 0 {
		return errors.New("max-body-bytes must be positive")
	}
	if u, err := url.Parse(c.UpstreamURL); err != nil || u.Scheme == "" || u.Host == "" {
		return fmt.Errorf("invalid upstream-url %q", c.UpstreamURL)
	}
	return nil
}

func applyEnvString(set map[string]bool, name, env string, target *string) {
	if set[name] {
		return
	}
	if v, ok := os.LookupEnv(env); ok {
		*target = v
	}
}

// applyEnvLogLevel honors PROXY_LOG_LEVEL, then the conventional LOG_LEVEL.
func applyEnvLogLevel(set map[string]bool, target *string) {
	if set["log-level"] {
		return
	}
	if v, ok := os.LookupEnv("PROXY_LOG_LEVEL"); ok {
		*target = v
		return
	}
	if v, ok := os.LookupEnv("LOG_LEVEL"); ok {
		*target = v
	}
}

func applyEnvDuration(set map[string]bool, name, env string, target *time.Duration) error {
	if set[name] {
		return nil
	}
	v, ok := os.LookupEnv(env)
	if !ok {
		return nil
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		return fmt.Errorf("invalid %s=%q: %w", env, v, err)
	}
	*target = d
	return nil
}

func applyEnvInt64(set map[string]bool, name, env string, target *int64) error {
	if set[name] {
		return nil
	}
	v, ok := os.LookupEnv(env)
	if !ok {
		return nil
	}
	n, err := strconv.ParseInt(v, 10, 64)
	if err != nil {
		return fmt.Errorf("invalid %s=%q: %w", env, v, err)
	}
	*target = n
	return nil
}

func applyEnvBool(set map[string]bool, name, env string, target *bool) error {
	if set[name] {
		return nil
	}
	v, ok := os.LookupEnv(env)
	if !ok {
		return nil
	}
	b, err := strconv.ParseBool(v)
	if err != nil {
		return fmt.Errorf("invalid %s=%q: %w", env, v, err)
	}
	*target = b
	return nil
}
