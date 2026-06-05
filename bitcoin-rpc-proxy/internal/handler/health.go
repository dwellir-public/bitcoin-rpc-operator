package handler

import (
	"net/http"

	"github.com/dwellir-public/bitcoin-operator/bitcoin-rpc-proxy/internal/upstream"
)

const (
	statusOK       = "ok"
	statusStarting = "starting"
	statusDegraded = "degraded"
)

// UpstreamHealth exposes the latest cached upstream health (implemented by *upstream.Poller).
type UpstreamHealth interface {
	Health() *upstream.Health
}

// Health serves the admin health endpoints: JSON detail on /health and a
// plain-text liveness probe on /healthz.
type Health struct {
	up      UpstreamHealth
	version string
}

// NewHealth creates a health handler backed by up.
func NewHealth(up UpstreamHealth, version string) *Health {
	return &Health{up: up, version: version}
}

type healthResponse struct {
	Status   string `json:"status"`
	Version  string `json:"version,omitempty"`
	Upstream string `json:"upstream"`
	Height   *int64 `json:"height,omitempty"`
	Error    string `json:"error,omitempty"`
}

// ProbeHandler returns the /healthz liveness probe: always 200 (process is alive).
func (*Health) ProbeHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
}

// ServeHTTP serves the JSON /health detail, reflecting upstream reachability.
func (h *Health) ServeHTTP(w http.ResponseWriter, _ *http.Request) {
	resp, code := h.evaluate()
	resp.Version = h.version
	writeJSONValue(w, code, resp)
}

func (h *Health) evaluate() (healthResponse, int) {
	state := h.up.Health()
	if state == nil {
		return healthResponse{Status: statusStarting, Upstream: "pending"}, http.StatusServiceUnavailable
	}
	if !state.IsHealthy() {
		resp := healthResponse{Status: statusDegraded, Upstream: "unreachable"}
		if state.Err != nil {
			resp.Error = state.Err.Error()
		}
		return resp, http.StatusServiceUnavailable
	}
	resp := healthResponse{Status: statusOK, Upstream: statusOK}
	if state.Height >= 0 {
		height := state.Height
		resp.Height = &height
	}
	return resp, http.StatusOK
}
