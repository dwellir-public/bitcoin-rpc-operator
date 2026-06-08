package upstream

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestForwardSuccess(t *testing.T) {
	var gotMethod, gotCT, gotAuth string
	var gotBody []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotMethod = r.Method
		gotCT = r.Header.Get("Content-Type")
		gotAuth = r.Header.Get("Authorization")
		gotBody, _ = io.ReadAll(r.Body)
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"result":42,"error":null,"id":1}`))
	}))
	defer srv.Close()

	c := NewClient(srv.URL, time.Second)
	hdr := http.Header{"Authorization": {"Basic dXNlcjpwYXNz"}}
	resp, err := c.Forward(context.Background(), "/", hdr, []byte(`{"method":"getblockcount"}`))
	require.NoError(t, err)

	assert.Equal(t, http.StatusOK, resp.Status)
	assert.JSONEq(t, `{"result":42,"error":null,"id":1}`, string(resp.Body))
	assert.Equal(t, http.MethodPost, gotMethod)
	assert.Equal(t, "application/json", gotCT)
	assert.Equal(t, "Basic dXNlcjpwYXNz", gotAuth, "Authorization must pass through")
	assert.JSONEq(t, `{"method":"getblockcount"}`, string(gotBody))
}

func TestForwardStripsHopByHop(t *testing.T) {
	var gotConn string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotConn = r.Header.Get("Keep-Alive")
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	c := NewClient(srv.URL, time.Second)
	hdr := http.Header{"Keep-Alive": {"timeout=5"}, "Authorization": {"Basic x"}}
	_, err := c.Forward(context.Background(), "/", hdr, []byte(`{}`))
	require.NoError(t, err)
	assert.Empty(t, gotConn, "hop-by-hop headers must not be forwarded")
}

func TestForwardInjectsUpstreamAuthWhenAbsent(t *testing.T) {
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	c := NewClient(srv.URL, time.Second)
	c.SetUpstreamAuth("user", "pass")
	_, err := c.Forward(context.Background(), "/", nil, []byte(`{}`))
	require.NoError(t, err)
	assert.Equal(t, basicAuth("user", "pass"), gotAuth, "proxy must inject upstream auth when caller sent none")
}

func TestForwardPreservesCallerAuth(t *testing.T) {
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	c := NewClient(srv.URL, time.Second)
	c.SetUpstreamAuth("user", "pass")
	hdr := http.Header{"Authorization": {"Basic caller"}}
	_, err := c.Forward(context.Background(), "/", hdr, []byte(`{}`))
	require.NoError(t, err)
	assert.Equal(t, "Basic caller", gotAuth, "caller-supplied Authorization must take precedence")
}

func TestForwardNoInjectionWhenUnconfigured(t *testing.T) {
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	c := NewClient(srv.URL, time.Second)
	_, err := c.Forward(context.Background(), "/", nil, []byte(`{}`))
	require.NoError(t, err)
	assert.Empty(t, gotAuth, "no auth must be injected when upstream credentials are unset")
}

func TestForwardUnavailable(t *testing.T) {
	// Reserved TEST-NET-1 address that does not accept connections quickly,
	// using a closed local port instead for determinism.
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	url := srv.URL
	srv.Close() // now nothing is listening

	c := NewClient(url, time.Second)
	_, err := c.Forward(context.Background(), "/", nil, []byte(`{}`))
	assert.ErrorIs(t, err, ErrUpstreamUnavailable)
}

func TestForwardTimeout(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(200 * time.Millisecond)
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	c := NewClient(srv.URL, 20*time.Millisecond)
	_, err := c.Forward(context.Background(), "/", nil, []byte(`{}`))
	assert.ErrorIs(t, err, ErrUpstreamTimeout)
}

func firstPoll(t *testing.T, srvURL string) *Health {
	t.Helper()
	return firstPollWithAuth(t, srvURL, "", "")
}

func firstPollWithAuth(t *testing.T, srvURL, user, password string) *Health {
	t.Helper()
	c := NewClient(srvURL, time.Second)
	p := NewPoller(c, time.Hour, zerolog.Nop())
	p.SetUpstreamAuth(user, password)

	ch := make(chan *Health, 1)
	p.OnPoll(func(h *Health) {
		select {
		case ch <- h:
		default:
		}
	})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go p.Run(ctx)

	select {
	case h := <-ch:
		return h
	case <-time.After(2 * time.Second):
		t.Fatal("poller did not report within timeout")
		return nil
	}
}

func TestPollerHealthyWithHeight(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"result":850000,"error":null,"id":"healthcheck"}`))
	}))
	defer srv.Close()

	h := firstPoll(t, srv.URL)
	assert.True(t, h.IsHealthy())
	assert.Equal(t, int64(850000), h.Height)
	assert.NoError(t, h.Err)
}

func TestPollerReachableButNoHeightOn401(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()

	h := firstPoll(t, srv.URL)
	assert.True(t, h.IsHealthy(), "401 means bitcoind is up")
	assert.Equal(t, heightUnknown, h.Height)
}

func TestPollerUnhealthyOn401WithCredentials(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()

	h := firstPollWithAuth(t, srv.URL, "user", "pass")
	assert.False(t, h.IsHealthy(), "401 with creds means the probe lacks RPC access")
	assert.ErrorIs(t, h.Err, ErrUpstreamUnauthorized)
}

func TestPollerProbeSendsCredentials(t *testing.T) {
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		_, _ = w.Write([]byte(`{"result":42,"error":null,"id":"healthcheck"}`))
	}))
	defer srv.Close()

	h := firstPollWithAuth(t, srv.URL, "user", "pass")
	assert.True(t, h.IsHealthy())
	assert.Equal(t, int64(42), h.Height)
	assert.Equal(t, "Basic dXNlcjpwYXNz", gotAuth, "probe must carry Basic auth when creds are set")
}

func TestPollerUnhealthyOn5xx(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	h := firstPoll(t, srv.URL)
	assert.False(t, h.IsHealthy())
	assert.ErrorIs(t, h.Err, ErrUpstream5xx)
}

func TestPollerUnhealthyWhenDown(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	url := srv.URL
	srv.Close()

	h := firstPoll(t, url)
	assert.False(t, h.IsHealthy())
	assert.ErrorIs(t, h.Err, ErrUpstreamUnavailable)
}

func TestHealthIsHealthyNil(t *testing.T) {
	var h *Health
	assert.False(t, h.IsHealthy())
}
