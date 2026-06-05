package handler

import (
	"net/http"
	"strconv"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// Metrics is the Prometheus implementation of Recorder plus the upstream-health
// gauge. It keeps to a small, low-cardinality set of series (no per-client labels).
type Metrics struct {
	requests   *prometheus.CounterVec
	denied     *prometheus.CounterVec
	duration   prometheus.Histogram
	upstreamUp prometheus.Gauge
	registry   *prometheus.Registry
}

// NewMetrics creates and registers the proxy metrics.
func NewMetrics() *Metrics {
	registry := prometheus.NewRegistry()
	m := &Metrics{
		requests: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "btc_rpc_proxy_requests_total",
			Help: "Total proxied requests by decision (allowed|denied) and HTTP status.",
		}, []string{"decision", "status"}),
		denied: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "btc_rpc_proxy_denied_method_total",
			Help: "Total denials by JSON-RPC method.",
		}, []string{"method"}),
		duration: prometheus.NewHistogram(prometheus.HistogramOpts{
			Name:    "btc_rpc_proxy_request_duration_seconds",
			Help:    "End-to-end request handling duration in seconds.",
			Buckets: prometheus.DefBuckets,
		}),
		upstreamUp: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "btc_rpc_proxy_upstream_healthy",
			Help: "Whether the upstream bitcoind is reachable (1) or not (0).",
		}),
		registry: registry,
	}
	registry.MustRegister(m.requests, m.denied, m.duration, m.upstreamUp)
	return m
}

// Handler returns the /metrics HTTP handler.
func (m *Metrics) Handler() http.Handler {
	return promhttp.HandlerFor(m.registry, promhttp.HandlerOpts{Registry: m.registry})
}

// RecordRequest counts a completed request by decision and status.
func (m *Metrics) RecordRequest(decision string, status int) {
	m.requests.WithLabelValues(decision, strconv.Itoa(status)).Inc()
}

// RecordDeniedMethod counts a denied method.
func (m *Metrics) RecordDeniedMethod(method string) {
	m.denied.WithLabelValues(method).Inc()
}

// ObserveDuration records a request's handling duration.
func (m *Metrics) ObserveDuration(d time.Duration) {
	m.duration.Observe(d.Seconds())
}

// MarkUpstreamHealthy sets the upstream-health gauge to 1.
func (m *Metrics) MarkUpstreamHealthy() {
	m.upstreamUp.Set(1)
}

// MarkUpstreamUnhealthy sets the upstream-health gauge to 0.
func (m *Metrics) MarkUpstreamUnhealthy() {
	m.upstreamUp.Set(0)
}
