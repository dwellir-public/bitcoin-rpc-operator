package server

import (
	"context"
	"errors"
	"net"
	"net/http"
	"sync/atomic"
	"time"

	"github.com/rs/zerolog"
)

const (
	readTimeout       = 30 * time.Second
	readHeaderTimeout = 5 * time.Second
	idleTimeout       = 60 * time.Second
)

// Server wraps http.Server with deterministic startup and graceful shutdown.
type Server struct {
	srv      *http.Server
	logger   zerolog.Logger
	ready    chan struct{}
	listenOn atomic.Value // bound addr string after Start
}

// New creates a Server bound to addr serving handler. WriteTimeout is left
// unset so slow upstream responses are not cut off; ReadHeaderTimeout guards
// against slowloris.
func New(addr string, handler http.Handler, logger zerolog.Logger) *Server {
	return &Server{
		srv: &http.Server{
			Addr:              addr,
			Handler:           handler,
			ReadTimeout:       readTimeout,
			ReadHeaderTimeout: readHeaderTimeout,
			IdleTimeout:       idleTimeout,
		},
		logger: logger,
		ready:  make(chan struct{}),
	}
}

// Start listens and serves, blocking until the server stops. It returns nil on
// a graceful shutdown.
func (s *Server) Start() error {
	ln, err := net.Listen("tcp", s.srv.Addr)
	if err != nil {
		return err
	}
	s.listenOn.Store(ln.Addr().String())
	s.logger.Info().Str("addr", ln.Addr().String()).Msg("http server starting")
	close(s.ready)

	if err := s.srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}

// Shutdown gracefully stops the server.
func (s *Server) Shutdown(ctx context.Context) error {
	s.logger.Info().Msg("http server shutting down")
	return s.srv.Shutdown(ctx)
}

// Addr returns the bound address after Start (useful when listening on :0).
func (s *Server) Addr() string {
	if v, ok := s.listenOn.Load().(string); ok {
		return v
	}
	return s.srv.Addr
}

// Ready returns a channel closed once the server is listening.
func (s *Server) Ready() <-chan struct{} {
	return s.ready
}
