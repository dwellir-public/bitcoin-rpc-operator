// Package handler implements the proxy's HTTP request handling: default-deny
// method filtering for single and batch JSON-RPC requests, and verbatim
// forwarding of allowed requests to bitcoind.
package handler

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"time"

	"github.com/rs/zerolog"

	"github.com/dwellir-public/bitcoin-rpc-operator/bitcoin-rpc-proxy/internal/policy"
	"github.com/dwellir-public/bitcoin-rpc-operator/bitcoin-rpc-proxy/internal/rpc"
	"github.com/dwellir-public/bitcoin-rpc-operator/bitcoin-rpc-proxy/internal/server"
	"github.com/dwellir-public/bitcoin-rpc-operator/bitcoin-rpc-proxy/internal/upstream"
)

const (
	decisionAllowed = "allowed"
	decisionDenied  = "denied"

	jsonContentType = "application/json"

	malformedMsg     = "malformed JSON-RPC request body"
	tooLargeMsg      = "request body too large"
	batchRejectedMsg = "request not executed: batch contains a method not allowed by proxy policy"
	timeoutMsg       = "upstream timeout"
	unavailableMsg   = "upstream unavailable"
	internalMsg      = "internal error"
	missingMethod    = "<missing>"
)

// Recorder records request outcomes for metrics. A nil Recorder is a no-op.
type Recorder interface {
	RecordRequest(decision string, status int)
	RecordDeniedMethod(method string)
	ObserveDuration(d time.Duration)
}

// RPC is the JSON-RPC filtering handler.
type RPC struct {
	allow   *policy.Allowlist
	up      *upstream.Client
	maxBody int64
	filter  bool
	rec     Recorder
}

// NewRPC builds the handler. When filter is false the allowlist is bypassed and
// every method is forwarded. rec may be nil.
func NewRPC(allow *policy.Allowlist, up *upstream.Client, maxBody int64, filter bool, rec Recorder) *RPC {
	return &RPC{allow: allow, up: up, maxBody: maxBody, filter: filter, rec: rec}
}

func (h *RPC) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	logger := server.Logger(r.Context())
	if h.rec != nil {
		defer func() { h.rec.ObserveDuration(time.Since(start)) }()
	}

	body, err := h.readBody(w, r)
	if err != nil {
		h.handleReadError(w, err, logger)
		return
	}

	env, perr := rpc.Parse(body)
	if perr != nil {
		h.reject(w, logger, http.StatusBadRequest, simpleError(rpc.CodeParseError, malformedMsg), "malformed")
		return
	}

	if h.anyDenied(env) {
		h.deny(w, env, logger)
		return
	}

	h.forward(w, r, env, start)
}

func (h *RPC) readBody(w http.ResponseWriter, r *http.Request) ([]byte, error) {
	r.Body = http.MaxBytesReader(w, r.Body, h.maxBody)
	return io.ReadAll(r.Body)
}

func (h *RPC) handleReadError(w http.ResponseWriter, err error, logger zerolog.Logger) {
	var maxErr *http.MaxBytesError
	if errors.As(err, &maxErr) {
		h.reject(w, logger, http.StatusRequestEntityTooLarge,
			simpleError(rpc.CodeInvalidRequest, tooLargeMsg), "too_large")
		return
	}
	h.reject(w, logger, http.StatusBadRequest, simpleError(rpc.CodeParseError, malformedMsg), "read_error")
}

func (h *RPC) forward(w http.ResponseWriter, r *http.Request, env *rpc.Envelope, start time.Time) {
	logger := server.Logger(r.Context())
	resp, err := h.up.Forward(r.Context(), r.URL.Path, r.Header, env.Raw)
	if err != nil {
		h.handleUpstreamError(w, env, err, logger)
		return
	}
	if h.rec != nil {
		h.rec.RecordRequest(decisionAllowed, resp.Status)
	}
	logger.Info().
		Int("status", resp.Status).
		Int("calls", len(env.Calls)).
		Dur("duration", time.Since(start)).
		Msg("request forwarded")
	writeUpstreamResponse(w, resp)
}

func (h *RPC) handleUpstreamError(w http.ResponseWriter, env *rpc.Envelope, err error, logger zerolog.Logger) {
	status := http.StatusBadGateway
	msg := unavailableMsg
	if errors.Is(err, upstream.ErrUpstreamTimeout) {
		status = http.StatusGatewayTimeout
		msg = timeoutMsg
	}
	if h.rec != nil {
		h.rec.RecordRequest(decisionAllowed, status)
	}
	logger.Warn().Err(err).Int("status", status).Msg("upstream forward failed")
	writeJSON(w, status, envelopeErrorBody(env, rpc.CodeInternalError, msg))
}

func (h *RPC) deny(w http.ResponseWriter, env *rpc.Envelope, logger zerolog.Logger) {
	body, denied := h.denyBody(env)
	if h.rec != nil {
		h.rec.RecordRequest(decisionDenied, http.StatusForbidden)
		for _, m := range denied {
			h.rec.RecordDeniedMethod(m)
		}
	}
	logger.Info().Strs("denied_methods", denied).Bool("batch", env.IsBatch).Msg("request denied by policy")
	writeJSON(w, http.StatusForbidden, body)
}

// denyBody builds the 403 body (single object or per-element array) and returns
// the method labels that triggered the denial.
func (h *RPC) denyBody(env *rpc.Envelope) (json.RawMessage, []string) {
	var denied []string
	if !env.IsBatch {
		call := env.Calls[0]
		denied = append(denied, methodLabel(call))
		return call.DenyError(), denied
	}
	parts := make([]json.RawMessage, len(env.Calls))
	for i, call := range env.Calls {
		if h.allowed(call) {
			parts[i] = call.ErrorReply(rpc.CodeInvalidRequest, batchRejectedMsg)
			continue
		}
		denied = append(denied, methodLabel(call))
		parts[i] = call.DenyError()
	}
	return marshalArray(parts), denied
}

func (h *RPC) anyDenied(env *rpc.Envelope) bool {
	if !h.filter {
		return false
	}
	for _, call := range env.Calls {
		if !h.allowed(call) {
			return true
		}
	}
	return false
}

func (h *RPC) allowed(call rpc.Call) bool {
	return call.Method != "" && h.allow.Allowed(call.Method)
}

func (h *RPC) reject(w http.ResponseWriter, logger zerolog.Logger, status int, body json.RawMessage, reason string) {
	if h.rec != nil {
		h.rec.RecordRequest(decisionDenied, status)
	}
	logger.Info().Int("status", status).Str("reason", reason).Msg("request rejected")
	writeJSON(w, status, body)
}

func methodLabel(call rpc.Call) string {
	if call.Method == "" {
		return missingMethod
	}
	return call.Method
}

// envelopeErrorBody builds a version-matched error body for a whole-envelope
// failure (e.g. upstream error): an array for batches, a single object otherwise.
func envelopeErrorBody(env *rpc.Envelope, code int, msg string) json.RawMessage {
	if env.IsBatch {
		parts := make([]json.RawMessage, len(env.Calls))
		for i, call := range env.Calls {
			parts[i] = call.ErrorReply(code, msg)
		}
		return marshalArray(parts)
	}
	if len(env.Calls) == 1 {
		return env.Calls[0].ErrorReply(code, msg)
	}
	return simpleError(code, msg)
}

// simpleError builds a standalone V1 error with a null id, for failures with no
// parsed call (malformed body, oversize).
func simpleError(code int, msg string) json.RawMessage {
	return rpc.Call{}.ErrorReply(code, msg)
}

func marshalArray(parts []json.RawMessage) json.RawMessage {
	b, err := json.Marshal(parts)
	if err != nil {
		return simpleError(rpc.CodeInternalError, internalMsg)
	}
	return b
}

func writeJSON(w http.ResponseWriter, status int, body []byte) {
	w.Header().Set("Content-Type", jsonContentType)
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

// writeJSONValue marshals v and writes it; on marshal failure it writes a 500
// internal error instead.
func writeJSONValue(w http.ResponseWriter, status int, v any) {
	body, err := json.Marshal(v)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, simpleError(rpc.CodeInternalError, internalMsg))
		return
	}
	writeJSON(w, status, body)
}

func writeUpstreamResponse(w http.ResponseWriter, resp *upstream.Response) {
	contentType := resp.Headers.Get("Content-Type")
	if contentType == "" {
		contentType = jsonContentType
	}
	w.Header().Set("Content-Type", contentType)
	w.WriteHeader(resp.Status)
	_, _ = w.Write(resp.Body)
}
