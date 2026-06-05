package handler

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestMetricsExposition(t *testing.T) {
	m := NewMetrics()
	m.RecordRequest(decisionAllowed, http.StatusOK)
	m.RecordRequest(decisionDenied, http.StatusForbidden)
	m.RecordDeniedMethod("stop")
	m.ObserveDuration(5 * time.Millisecond)
	m.MarkUpstreamHealthy()

	rec := httptest.NewRecorder()
	m.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	require.Equal(t, http.StatusOK, rec.Code)

	body := rec.Body.String()
	assert.Contains(t, body, `btc_rpc_proxy_requests_total{decision="allowed",status="200"} 1`)
	assert.Contains(t, body, `btc_rpc_proxy_requests_total{decision="denied",status="403"} 1`)
	assert.Contains(t, body, `btc_rpc_proxy_denied_method_total{method="stop"} 1`)
	assert.Contains(t, body, "btc_rpc_proxy_request_duration_seconds")
	assert.Contains(t, body, "btc_rpc_proxy_upstream_healthy 1")
}

func TestMetricsUpstreamUnhealthy(t *testing.T) {
	m := NewMetrics()
	m.MarkUpstreamUnhealthy()

	rec := httptest.NewRecorder()
	m.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	assert.Contains(t, rec.Body.String(), "btc_rpc_proxy_upstream_healthy 0")
}
