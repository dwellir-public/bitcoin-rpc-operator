package app

import (
	"context"
	"errors"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/rs/zerolog"

	"github.com/dwellir-public/bitcoin-rpc-operator/bitcoin-rpc-proxy/internal/handler"
	"github.com/dwellir-public/bitcoin-rpc-operator/bitcoin-rpc-proxy/internal/policy"
	"github.com/dwellir-public/bitcoin-rpc-operator/bitcoin-rpc-proxy/internal/server"
	"github.com/dwellir-public/bitcoin-rpc-operator/bitcoin-rpc-proxy/internal/upstream"
)

const (
	shutdownTimeout = 5 * time.Second
	pollInterval    = 10 * time.Second
	serverCount     = 2
)

// App is the top-level application container.
type App struct {
	logger          zerolog.Logger
	server          *server.Server
	adminServer     *server.Server
	poller          *upstream.Poller
	shutdownTimeout time.Duration
}

// New wires the components from cfg and returns a ready-to-run App.
func New(cfg *Config, logger zerolog.Logger, version string) *App {
	metrics := handler.NewMetrics()
	client := upstream.NewClient(cfg.UpstreamURL, cfg.UpstreamTimeout)
	client.SetUpstreamAuth(cfg.UpstreamUser, cfg.UpstreamPassword)
	allow := policy.NewAllowlist(cfg.ExtendAllowlist)
	rpcHandler := handler.NewRPC(allow, client, cfg.MaxBodyBytes, cfg.Filter, metrics)

	poller := upstream.NewPoller(client, pollInterval, logger)
	poller.SetUpstreamAuth(cfg.UpstreamUser, cfg.UpstreamPassword)
	poller.OnPoll(func(h *upstream.Health) {
		if h.IsHealthy() {
			metrics.MarkUpstreamHealthy()
			return
		}
		metrics.MarkUpstreamUnhealthy()
	})
	health := handler.NewHealth(poller, version)

	mainMux := server.NewMux(rpcHandler, health, health.ProbeHandler(), logger)
	adminMux := server.NewAdminMux(health, health.ProbeHandler(), metrics.Handler(), logger)

	return &App{
		logger:          logger,
		server:          server.New(cfg.Listen, mainMux, logger),
		adminServer:     server.New(cfg.AdminListen, adminMux, logger),
		poller:          poller,
		shutdownTimeout: shutdownTimeout,
	}
}

// Run starts the servers and health poller, blocking until a shutdown signal or
// a fatal server error, then shuts down gracefully.
func (a *App) Run(ctx context.Context) error {
	signalCtx, stop := signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)
	defer stop()

	pollerCtx, pollerCancel := context.WithCancel(signalCtx)
	var wg sync.WaitGroup
	wg.Go(func() { a.poller.Run(pollerCtx) })
	defer func() {
		pollerCancel()
		wg.Wait()
	}()

	errCh := make(chan error, serverCount)
	go func() { errCh <- a.server.Start() }()
	go func() { errCh <- a.adminServer.Start() }()

	var startErr error
	consumed := 0
	select {
	case startErr = <-errCh:
		consumed = 1
	case <-signalCtx.Done():
		a.logger.Info().Msg("shutdown signal received")
	}

	// Start the shutdown clock only once shutdown is actually triggered. Creating
	// it before the blocking select would let it expire during normal operation,
	// so a graceful shutdown after more than shutdownTimeout of uptime would pass
	// an already-expired context to Server.Shutdown and skip the connection drain.
	shutdownCtx, cancel := context.WithTimeout(context.Background(), a.shutdownTimeout)
	defer cancel()
	shutdownErr := a.shutdown(shutdownCtx)
	// Each Start() goroutine sends exactly once; shutdown guarantees both have
	// returned, so collect any results we haven't read yet. This keeps a second
	// bind failure from being silently dropped (graceful stops send nil).
	for i := consumed; i < serverCount; i++ {
		startErr = errors.Join(startErr, <-errCh)
	}
	return errors.Join(startErr, shutdownErr)
}

func (a *App) shutdown(ctx context.Context) error {
	return errors.Join(a.server.Shutdown(ctx), a.adminServer.Shutdown(ctx))
}

// Server returns the main JSON-RPC server (useful in tests for the bound address).
func (a *App) Server() *server.Server { return a.server }

// AdminServer returns the admin server.
func (a *App) AdminServer() *server.Server { return a.adminServer }
