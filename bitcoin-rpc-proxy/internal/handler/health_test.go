package handler

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/dwellir-public/bitcoin-operator/bitcoin-rpc-proxy/internal/upstream"
)

type fakeHealth struct {
	state *upstream.Health
}

func (f fakeHealth) Health() *upstream.Health { return f.state }

func serveHealth(t *testing.T, state *upstream.Health) (*httptest.ResponseRecorder, healthResponse) {
	t.Helper()
	h := NewHealth(fakeHealth{state: state}, "v-test")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/health", nil))
	var resp healthResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	return rec, resp
}

func TestHealthStarting(t *testing.T) {
	rec, resp := serveHealth(t, nil)
	assert.Equal(t, http.StatusServiceUnavailable, rec.Code)
	assert.Equal(t, statusStarting, resp.Status)
}

func TestHealthOK(t *testing.T) {
	rec, resp := serveHealth(t, &upstream.Health{Reachable: true, Height: 850000})
	assert.Equal(t, http.StatusOK, rec.Code)
	assert.Equal(t, statusOK, resp.Status)
	assert.Equal(t, "v-test", resp.Version)
	require.NotNil(t, resp.Height)
	assert.Equal(t, int64(850000), *resp.Height)
}

func TestHealthOKWithoutHeight(t *testing.T) {
	rec, resp := serveHealth(t, &upstream.Health{Reachable: true, Height: -1})
	assert.Equal(t, http.StatusOK, rec.Code)
	assert.Nil(t, resp.Height, "unknown height is omitted")
}

func TestHealthDegraded(t *testing.T) {
	rec, resp := serveHealth(t, &upstream.Health{Reachable: false, Err: errors.New("boom")})
	assert.Equal(t, http.StatusServiceUnavailable, rec.Code)
	assert.Equal(t, statusDegraded, resp.Status)
	assert.Contains(t, resp.Error, "boom")
}

func TestHealthProbe(t *testing.T) {
	h := NewHealth(fakeHealth{}, "v-test")
	rec := httptest.NewRecorder()
	h.ProbeHandler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	assert.Equal(t, http.StatusOK, rec.Code)
	assert.Equal(t, "ok", rec.Body.String())
}
