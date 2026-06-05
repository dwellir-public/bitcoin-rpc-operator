// Command bitcoin-rpc-proxy is a default-deny filtering proxy for Bitcoin Core
// JSON-RPC. See the bitcoin-rpc-proxy README for design and configuration.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"

	"github.com/dwellir-public/bitcoin-rpc-operator/bitcoin-rpc-proxy/internal/app"
	"github.com/dwellir-public/bitcoin-rpc-operator/bitcoin-rpc-proxy/internal/common"
)

// Build information, injected via -ldflags at build time.
var (
	buildTimeUTC string
	commit       string
	version      string
)

func main() {
	cfg, printVersion, err := app.Parse(os.Args[1:])
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			os.Exit(0)
		}
		earlyLogger := common.NewLogger()
		earlyLogger.Fatal().Err(err).Msg("invalid configuration")
	}

	if printVersion {
		_, _ = fmt.Printf("bitcoin-rpc-proxy version %s (commit %s, build time %s)\n",
			version, commit, buildTimeUTC)
		os.Exit(0)
	}

	logger := common.NewLoggerWithLevel(cfg.LogLevel)
	logger.Info().
		Str("version", version).
		Str("commit", commit).
		Str("listen", cfg.Listen).
		Str("admin_listen", cfg.AdminListen).
		Str("upstream_url", cfg.UpstreamURL).
		Msg("bitcoin-rpc-proxy starting")

	application := app.New(cfg, logger, version+" ("+commit+")")
	if err := application.Run(context.Background()); err != nil {
		logger.Fatal().Err(err).Msg("server error")
	}
}
