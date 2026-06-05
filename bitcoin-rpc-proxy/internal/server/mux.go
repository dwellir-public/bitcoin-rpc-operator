// Package server provides the HTTP routing, middleware, and lifecycle for the proxy.
package server

import (
	"net/http"

	"github.com/rs/zerolog"
)

// NewMux builds the main listener's router: all POST requests (to / or
// /wallet/<name>) go to rpcHandler; GET /health and GET /healthz serve the same
// health and liveness probes as the admin listener; other methods get 405 from
// the ServeMux. The chain wraps the mux with request-ID and panic-recovery
// middleware.
func NewMux(rpcHandler, health, probe http.Handler, logger zerolog.Logger) http.Handler {
	mux := http.NewServeMux()
	mux.Handle("POST /", rpcHandler)
	mux.Handle("GET /health", health)
	mux.Handle("GET /healthz", probe)
	return panicRecovery(withRequestID(mux, logger), logger)
}

// NewAdminMux builds the admin listener's router: JSON /health, plain /healthz,
// and /metrics.
func NewAdminMux(health, probe, metrics http.Handler, logger zerolog.Logger) http.Handler {
	mux := http.NewServeMux()
	mux.Handle("GET /health", health)
	mux.Handle("GET /healthz", probe)
	mux.Handle("GET /metrics", metrics)
	return panicRecovery(withRequestID(mux, logger), logger)
}
