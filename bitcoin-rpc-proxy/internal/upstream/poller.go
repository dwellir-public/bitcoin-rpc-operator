package upstream

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sync/atomic"
	"time"

	"github.com/rs/zerolog"
)

// healthCheckPath and getblockcountBody are the lightweight liveness probe.
const healthCheckPath = "/"

// heightUnknown marks a poll where the block height could not be determined
// (e.g. the probe was unauthenticated and bitcoind returned 401).
const heightUnknown int64 = -1

var getblockcountBody = []byte(`{"jsonrpc":"1.0","id":"healthcheck","method":"getblockcount","params":[]}`)

// ErrUpstream5xx indicates the upstream returned a 5xx status to the health probe.
var ErrUpstream5xx = errors.New("upstream returned 5xx")

// ErrUpstreamUnauthorized indicates bitcoind rejected the probe's credentials.
var ErrUpstreamUnauthorized = errors.New("upstream rejected health-probe credentials")

// Health is the cached result of a getblockcount health poll. Without probe
// credentials any non-5xx response counts as reachable (a 401 still means
// bitcoind is up) and Height is filled only when a 200 carried a numeric result.
// With credentials configured, a 401/403 is instead treated as unhealthy, so the
// probe asserts real RPC access rather than mere TCP liveness.
type Health struct {
	Reachable bool      // upstream answered (no transport error/timeout, status < 500)
	Height    int64     // block height, or heightUnknown
	Err       error     // last poll error, if any
	PollAt    time.Time // wall clock when this poll completed
}

// IsHealthy reports whether the most recent poll reached the upstream.
func (h *Health) IsHealthy() bool {
	return h != nil && h.Reachable
}

// Poller periodically probes the upstream with getblockcount and caches the result.
type Poller struct {
	client     *Client
	interval   time.Duration
	health     atomic.Pointer[Health]
	logger     zerolog.Logger
	onPoll     func(*Health)
	authHeader string // "Basic ..." when probe credentials are configured, else ""
}

// NewPoller creates a poller that probes client every interval.
func NewPoller(client *Client, interval time.Duration, logger zerolog.Logger) *Poller {
	return &Poller{
		client:   client,
		interval: interval,
		logger:   logger.With().Str("component", "poller").Logger(),
	}
}

// SetUpstreamAuth configures Basic credentials sent only on the health probe (not
// on forwarded client traffic). When user is empty, the probe stays unauthenticated.
func (p *Poller) SetUpstreamAuth(user, password string) {
	p.authHeader = basicAuth(user, password)
}

// OnPoll sets a callback invoked after each poll with the cached health.
func (p *Poller) OnPoll(fn func(*Health)) {
	p.onPoll = fn
}

// Health returns the latest cached poll result, or nil if no poll has completed.
func (p *Poller) Health() *Health {
	return p.health.Load()
}

// Run polls immediately, then every interval until ctx is canceled.
func (p *Poller) Run(ctx context.Context) {
	p.poll(ctx)

	ticker := time.NewTicker(p.interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			p.poll(ctx)
		}
	}
}

func (p *Poller) poll(ctx context.Context) {
	now := time.Now()

	resp, err := p.client.Forward(ctx, healthCheckPath, p.probeHeader(), getblockcountBody)
	if err != nil {
		p.logger.Warn().Err(err).Msg("health poll failed")
		p.store(&Health{Height: heightUnknown, Err: err, PollAt: now})
		return
	}

	if resp.Status >= http.StatusInternalServerError {
		statusErr := fmt.Errorf("%w: %d", ErrUpstream5xx, resp.Status)
		p.logger.Warn().Err(statusErr).Int("status", resp.Status).Msg("health poll: upstream 5xx")
		p.store(&Health{Height: heightUnknown, Err: statusErr, PollAt: now})
		return
	}

	// With credentials configured the probe asserts real RPC access, so an auth
	// rejection is unhealthy rather than mere "bitcoind is up".
	if p.authHeader != "" && (resp.Status == http.StatusUnauthorized || resp.Status == http.StatusForbidden) {
		statusErr := fmt.Errorf("%w: %d", ErrUpstreamUnauthorized, resp.Status)
		p.logger.Warn().Err(statusErr).Int("status", resp.Status).Msg("health poll: upstream rejected credentials")
		p.store(&Health{Height: heightUnknown, Err: statusErr, PollAt: now})
		return
	}

	h := &Health{Reachable: true, Height: parseHeight(resp.Body), PollAt: now}
	p.logger.Trace().Int("status", resp.Status).Int64("height", h.Height).Msg("health poll ok")
	p.store(h)
}

// probeHeader returns the header sent with the health probe: an Authorization
// header when credentials are configured, otherwise nil.
func (p *Poller) probeHeader() http.Header {
	if p.authHeader == "" {
		return nil
	}
	return http.Header{"Authorization": {p.authHeader}}
}

func (p *Poller) store(h *Health) {
	p.health.Store(h)
	if p.onPoll != nil {
		p.onPoll(h)
	}
}

type heightResult struct {
	Result *int64 `json:"result"`
}

// parseHeight returns the getblockcount result, or heightUnknown if the body is
// not a 200-style result with a numeric result field.
func parseHeight(body []byte) int64 {
	var r heightResult
	if err := json.Unmarshal(body, &r); err != nil || r.Result == nil {
		return heightUnknown
	}
	return *r.Result
}
