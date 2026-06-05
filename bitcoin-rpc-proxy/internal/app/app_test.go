package app

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestAppEndToEnd(t *testing.T) {
	var upstreamCalls atomic.Int64
	bitcoind := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		// Ignore the health poller's own getblockcount probe; count only proxied requests.
		if !strings.Contains(string(body), "healthcheck") {
			upstreamCalls.Add(1)
		}
		_, _ = w.Write([]byte(`{"result":1,"error":null,"id":1}`))
	}))
	defer bitcoind.Close()

	cfg := &Config{
		Listen:          "127.0.0.1:0",
		AdminListen:     "127.0.0.1:0",
		LogLevel:        "error",
		UpstreamURL:     bitcoind.URL,
		UpstreamTimeout: time.Second,
		MaxBodyBytes:    1 << 20,
		Filter:          true,
	}

	application := New(cfg, zerolog.Nop(), "v-test")
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- application.Run(ctx) }()

	<-application.Server().Ready()
	<-application.AdminServer().Ready()
	mainURL := "http://" + application.Server().Addr()
	adminURL := "http://" + application.AdminServer().Addr()

	assert.Equal(t, http.StatusOK, getStatus(t, adminURL+"/healthz"))
	assert.Equal(t, http.StatusOK, getStatus(t, adminURL+"/metrics"))

	// Health probes are also served on the main RPC listener.
	assert.Equal(t, http.StatusOK, getStatus(t, mainURL+"/healthz"))
	assert.Equal(t, http.StatusOK, getStatus(t, mainURL+"/health"))

	denied := postStatus(t, mainURL+"/", `{"method":"stop","id":1}`)
	assert.Equal(t, http.StatusForbidden, denied)
	assert.Zero(t, upstreamCalls.Load(), "denied request must not reach upstream")

	allowed := postStatus(t, mainURL+"/", `{"method":"getblockcount","id":1}`)
	assert.Equal(t, http.StatusOK, allowed)
	assert.Equal(t, int64(1), upstreamCalls.Load())

	cancel()
	select {
	case err := <-done:
		assert.NoError(t, err, "graceful shutdown should return nil")
	case <-time.After(5 * time.Second):
		t.Fatal("app did not shut down within timeout")
	}
}

// TestGracefulShutdownAfterTimeoutElapsed guards against the shutdown context
// being created before Run blocks: with that bug, any uptime past shutdownTimeout
// left the context already expired, so shutting down with a request in flight
// returned context.DeadlineExceeded and skipped the connection drain instead of
// letting the request finish and returning nil.
func TestGracefulShutdownAfterTimeoutElapsed(t *testing.T) {
	reqStarted := make(chan struct{})
	release := make(chan struct{})
	bitcoind := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		// The health poller probes upstream on its own; only block the client's
		// proxied call so its connection stays active across the shutdown.
		if !strings.Contains(string(body), "healthcheck") {
			close(reqStarted)
			<-release // hold the proxy->upstream call open so the client connection stays active
		}
		_, _ = w.Write([]byte(`{"result":1,"error":null,"id":1}`))
	}))
	defer bitcoind.Close()

	cfg := &Config{
		Listen:          "127.0.0.1:0",
		AdminListen:     "127.0.0.1:0",
		LogLevel:        "error",
		UpstreamURL:     bitcoind.URL,
		UpstreamTimeout: time.Second,
		MaxBodyBytes:    1 << 20,
		Filter:          true,
	}

	application := New(cfg, zerolog.Nop(), "v-test")
	application.shutdownTimeout = 200 * time.Millisecond

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- application.Run(ctx) }()

	<-application.Server().Ready()
	<-application.AdminServer().Ready()
	mainURL := "http://" + application.Server().Addr()

	// Exceed shutdownTimeout of uptime: the buggy context (created up-front) is now
	// already expired.
	time.Sleep(300 * time.Millisecond)

	reqStatus := make(chan int, 1)
	go func() { reqStatus <- postStatus(t, mainURL+"/", `{"method":"getblockcount","id":1}`) }()
	<-reqStarted // request is now active in the proxy

	cancel()                          // trigger shutdown with the request still in flight
	time.Sleep(20 * time.Millisecond) // let Shutdown start draining
	close(release)                    // request completes well within a fresh 200ms window

	select {
	case err := <-done:
		assert.NoError(t, err, "graceful shutdown should drain the in-flight request and return nil")
	case <-time.After(5 * time.Second):
		t.Fatal("app did not shut down within timeout")
	}
	assert.Equal(t, http.StatusOK, <-reqStatus, "in-flight request should complete during drain")
}

func getStatus(t *testing.T, url string) int {
	t.Helper()
	resp, err := http.Get(url)
	require.NoError(t, err)
	defer func() { _ = resp.Body.Close() }()
	_, _ = io.Copy(io.Discard, resp.Body)
	return resp.StatusCode
}

func postStatus(t *testing.T, url, body string) int {
	t.Helper()
	resp, err := http.Post(url, "application/json", strings.NewReader(body))
	require.NoError(t, err)
	defer func() { _ = resp.Body.Close() }()
	_, _ = io.Copy(io.Discard, resp.Body)
	return resp.StatusCode
}
