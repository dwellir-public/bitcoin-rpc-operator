// Package upstream forwards JSON-RPC requests to bitcoind and polls it for health.
package upstream

import (
	"bytes"
	"context"
	"encoding/base64"
	"errors"
	"io"
	"net"
	"net/http"
	"time"
)

// Upstream errors returned by Forward.
var (
	ErrUpstreamTimeout     = errors.New("upstream timeout")
	ErrUpstreamUnavailable = errors.New("upstream unavailable")
)

const (
	maxIdleConns        = 100
	maxIdleConnsPerHost = 100
	idleConnTimeout     = 90 * time.Second
)

// hopByHop headers are connection-scoped and must not be forwarded upstream.
var hopByHop = map[string]struct{}{
	"Connection":          {},
	"Keep-Alive":          {},
	"Proxy-Authenticate":  {},
	"Proxy-Authorization": {},
	"Te":                  {},
	"Trailer":             {},
	"Transfer-Encoding":   {},
	"Upgrade":             {},
}

// Response holds an upstream HTTP response.
type Response struct {
	Status  int
	Headers http.Header
	Body    []byte
}

// Client forwards requests to a bitcoind JSON-RPC endpoint over a pooled transport.
type Client struct {
	baseURL    string
	client     *http.Client
	authHeader string // "Basic ..." injected on Forward when the caller sent none
}

// basicAuth builds an HTTP Basic Authorization header value, or "" when user is
// empty (auth disabled).
func basicAuth(user, password string) string {
	if user == "" {
		return ""
	}
	return "Basic " + base64.StdEncoding.EncodeToString([]byte(user+":"+password))
}

// NewClient creates an upstream client for baseURL with the given per-request timeout.
func NewClient(baseURL string, timeout time.Duration) *Client {
	return &Client{
		baseURL: baseURL,
		client: &http.Client{
			Timeout: timeout,
			Transport: &http.Transport{
				MaxIdleConns:        maxIdleConns,
				MaxIdleConnsPerHost: maxIdleConnsPerHost,
				IdleConnTimeout:     idleConnTimeout,
			},
		},
	}
}

// BaseURL returns the configured upstream base URL.
func (c *Client) BaseURL() string {
	return c.baseURL
}

// SetUpstreamAuth configures Basic credentials injected into forwarded requests
// that arrive without an Authorization header. A caller-supplied Authorization
// always takes precedence. An empty user disables injection. Call once before
// serving; it is not safe for concurrent use with Forward.
func (c *Client) SetUpstreamAuth(user, password string) {
	c.authHeader = basicAuth(user, password)
}

// Forward POSTs body to baseURL+path, passing through header (minus hop-by-hop
// headers) so a caller-supplied Authorization reaches bitcoind. When the caller
// sent no Authorization and upstream credentials are configured (SetUpstreamAuth),
// the proxy's own credentials are injected so unauthenticated callers (e.g. via
// HAProxy) still authenticate to bitcoind. It returns a classified error
// (ErrUpstreamTimeout / ErrUpstreamUnavailable) on failure.
func (c *Client) Forward(ctx context.Context, path string, header http.Header, body []byte) (*Response, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	copyForwardHeaders(req.Header, header)
	req.Header.Set("Content-Type", "application/json")
	if c.authHeader != "" && req.Header.Get("Authorization") == "" {
		req.Header.Set("Authorization", c.authHeader)
	}

	resp, err := c.client.Do(req)
	if err != nil {
		return nil, classifyUpstreamError(err)
	}
	defer func() { _ = resp.Body.Close() }()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, ErrUpstreamUnavailable
	}
	return &Response{Status: resp.StatusCode, Headers: resp.Header, Body: respBody}, nil
}

func copyForwardHeaders(dst, src http.Header) {
	for key, vals := range src {
		if _, skip := hopByHop[http.CanonicalHeaderKey(key)]; skip {
			continue
		}
		for _, v := range vals {
			dst.Add(key, v)
		}
	}
}

func classifyUpstreamError(err error) error {
	if errors.Is(err, context.DeadlineExceeded) {
		return ErrUpstreamTimeout
	}
	if netErr, ok := errors.AsType[net.Error](err); ok {
		if netErr.Timeout() {
			return ErrUpstreamTimeout
		}
		return ErrUpstreamUnavailable
	}
	return ErrUpstreamUnavailable
}
