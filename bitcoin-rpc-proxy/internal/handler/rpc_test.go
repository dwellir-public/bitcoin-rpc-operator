package handler

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/dwellir-public/bitcoin-operator/bitcoin-rpc-proxy/internal/policy"
	"github.com/dwellir-public/bitcoin-operator/bitcoin-rpc-proxy/internal/server"
	"github.com/dwellir-public/bitcoin-operator/bitcoin-rpc-proxy/internal/upstream"
)

const defaultMaxBody = 1 << 20

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type rpcReply struct {
	Error rpcError `json:"error"`
}

// spy is a stub bitcoind that records what it received.
type spy struct {
	mu       sync.Mutex
	calls    int
	body     []byte
	path     string
	status   int    // response status (0 -> 200)
	respBody string // response body
	delay    time.Duration
}

func (s *spy) handler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		s.mu.Lock()
		s.calls++
		s.body = b
		s.path = r.URL.Path
		status, respBody, delay := s.status, s.respBody, s.delay
		s.mu.Unlock()

		if delay > 0 {
			time.Sleep(delay)
		}
		if status == 0 {
			status = http.StatusOK
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(respBody))
	}
}

func (s *spy) snapshot() (int, []byte, string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.calls, s.body, s.path
}

type testStack struct {
	front *httptest.Server
	spy   *spy
}

func newStack(t *testing.T, maxBody int64, upstreamTimeout time.Duration, sp *spy) *testStack {
	t.Helper()
	bitcoind := httptest.NewServer(sp.handler())
	t.Cleanup(bitcoind.Close)

	up := upstream.NewClient(bitcoind.URL, upstreamTimeout)
	h := NewRPC(policy.NewAllowlist(nil), up, maxBody, true, nil)
	front := httptest.NewServer(server.NewMux(h, http.NotFoundHandler(), http.NotFoundHandler(), zerolog.Nop()))
	t.Cleanup(front.Close)

	return &testStack{front: front, spy: sp}
}

func post(t *testing.T, url, body string) *http.Response {
	t.Helper()
	resp, err := http.Post(url, "application/json", strings.NewReader(body))
	require.NoError(t, err)
	t.Cleanup(func() { _ = resp.Body.Close() })
	return resp
}

func readBody(t *testing.T, resp *http.Response) []byte {
	t.Helper()
	b, err := io.ReadAll(resp.Body)
	require.NoError(t, err)
	return b
}

func TestAllowedForwardedVerbatim(t *testing.T) {
	sp := &spy{status: http.StatusOK, respBody: `{"result":42,"error":null,"id":1}`}
	st := newStack(t, defaultMaxBody, time.Second, sp)

	const reqBody = `{"jsonrpc":"2.0","method":"getblockcount","params":[],"id":1}`
	resp := post(t, st.front.URL+"/", reqBody)

	assert.Equal(t, http.StatusOK, resp.StatusCode)
	assert.JSONEq(t, `{"result":42,"error":null,"id":1}`, string(readBody(t, resp)))

	calls, body, path := sp.snapshot()
	assert.Equal(t, 1, calls)
	assert.Equal(t, reqBody, string(body), "body must be forwarded byte-identical")
	assert.Equal(t, "/", path)
}

func TestDeniedNeverReachesUpstream(t *testing.T) {
	sp := &spy{}
	st := newStack(t, defaultMaxBody, time.Second, sp)

	resp := post(t, st.front.URL+"/", `{"method":"stop","id":1}`)
	assert.Equal(t, http.StatusForbidden, resp.StatusCode)

	var reply rpcReply
	require.NoError(t, json.Unmarshal(readBody(t, resp), &reply))
	assert.Equal(t, -32601, reply.Error.Code)
	assert.Contains(t, reply.Error.Message, "stop")

	calls, _, _ := sp.snapshot()
	assert.Zero(t, calls, "denied request must not reach upstream")
}

func TestFilterDisabledForwardsDeniedMethod(t *testing.T) {
	sp := &spy{status: http.StatusOK, respBody: `{"result":null,"error":null,"id":1}`}
	bitcoind := httptest.NewServer(sp.handler())
	t.Cleanup(bitcoind.Close)
	up := upstream.NewClient(bitcoind.URL, time.Second)
	h := NewRPC(policy.NewAllowlist(nil), up, defaultMaxBody, false, nil)
	front := httptest.NewServer(server.NewMux(h, http.NotFoundHandler(), http.NotFoundHandler(), zerolog.Nop()))
	t.Cleanup(front.Close)

	const reqBody = `{"method":"stop","id":1}`
	resp := post(t, front.URL+"/", reqBody)
	assert.Equal(t, http.StatusOK, resp.StatusCode, "filter disabled must forward an otherwise-denied method")

	calls, body, _ := sp.snapshot()
	assert.Equal(t, 1, calls, "request must reach upstream when filtering is off")
	assert.Equal(t, reqBody, string(body), "body must be forwarded byte-identical")
}

func TestMixedBatchRejectedWhole(t *testing.T) {
	sp := &spy{}
	st := newStack(t, defaultMaxBody, time.Second, sp)

	resp := post(t, st.front.URL+"/",
		`[{"method":"getblockcount","id":1},{"method":"stop","id":2}]`)
	assert.Equal(t, http.StatusForbidden, resp.StatusCode)

	var arr []map[string]json.RawMessage
	require.NoError(t, json.Unmarshal(readBody(t, resp), &arr))
	require.Len(t, arr, 2)
	// Element 0 (allowed) was not executed; element 1 (stop) is the denied method.
	assert.Contains(t, arr[0], "error")
	assert.Contains(t, arr[1], "error")

	calls, _, _ := sp.snapshot()
	assert.Zero(t, calls, "a batch with any denied method must not reach upstream")
}

func TestAllowedBatchForwarded(t *testing.T) {
	sp := &spy{respBody: `[{"result":1,"id":1},{"result":2,"id":2}]`}
	st := newStack(t, defaultMaxBody, time.Second, sp)

	const reqBody = `[{"method":"getblockcount","id":1},{"method":"uptime","id":2}]`
	resp := post(t, st.front.URL+"/", reqBody)
	assert.Equal(t, http.StatusOK, resp.StatusCode)

	calls, body, _ := sp.snapshot()
	assert.Equal(t, 1, calls)
	assert.Equal(t, reqBody, string(body))
}

func TestWalletPathForwarded(t *testing.T) {
	sp := &spy{respBody: `{"result":null,"error":null,"id":1}`}
	st := newStack(t, defaultMaxBody, time.Second, sp)

	post(t, st.front.URL+"/wallet/mywallet", `{"method":"getblockcount","id":1}`)

	_, _, path := sp.snapshot()
	assert.Equal(t, "/wallet/mywallet", path, "wallet path must be forwarded verbatim")
}

func TestOversizeRejected(t *testing.T) {
	sp := &spy{}
	st := newStack(t, 32, time.Second, sp)

	big := `{"method":"getblockcount","id":"` + strings.Repeat("x", 200) + `"}`
	resp := post(t, st.front.URL+"/", big)
	assert.Equal(t, http.StatusRequestEntityTooLarge, resp.StatusCode)

	calls, _, _ := sp.snapshot()
	assert.Zero(t, calls)
}

func TestMalformedRejected(t *testing.T) {
	sp := &spy{}
	st := newStack(t, defaultMaxBody, time.Second, sp)

	resp := post(t, st.front.URL+"/", `not json`)
	assert.Equal(t, http.StatusBadRequest, resp.StatusCode)

	calls, _, _ := sp.snapshot()
	assert.Zero(t, calls)
}

func TestNonPostGets405(t *testing.T) {
	sp := &spy{}
	st := newStack(t, defaultMaxBody, time.Second, sp)

	resp, err := http.Get(st.front.URL + "/")
	require.NoError(t, err)
	t.Cleanup(func() { _ = resp.Body.Close() })
	assert.Equal(t, http.StatusMethodNotAllowed, resp.StatusCode)
}

func TestUpstreamUnavailable502(t *testing.T) {
	bitcoind := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	upURL := bitcoind.URL
	bitcoind.Close() // nothing listening

	up := upstream.NewClient(upURL, time.Second)
	h := NewRPC(policy.NewAllowlist(nil), up, defaultMaxBody, true, nil)
	front := httptest.NewServer(server.NewMux(h, http.NotFoundHandler(), http.NotFoundHandler(), zerolog.Nop()))
	t.Cleanup(front.Close)

	resp := post(t, front.URL+"/", `{"method":"getblockcount","id":1}`)
	assert.Equal(t, http.StatusBadGateway, resp.StatusCode)
}

func TestUpstreamTimeout504(t *testing.T) {
	sp := &spy{delay: 200 * time.Millisecond, respBody: `{"result":1}`}
	st := newStack(t, defaultMaxBody, 20*time.Millisecond, sp)

	resp := post(t, st.front.URL+"/", `{"method":"getblockcount","id":1}`)
	assert.Equal(t, http.StatusGatewayTimeout, resp.StatusCode)
}
