// Package rpc parses Bitcoin Core JSON-RPC request bodies enough to filter
// them by method, and builds version-matched error replies. It mirrors the
// wire behavior documented in docs/bitcoind-api.md; it does not validate
// params or execute anything (allowed bodies are forwarded verbatim).
package rpc

import (
	"bytes"
	"encoding/json"
	"errors"
)

// jsonRPCVersion2 is the only string that selects JSON-RPC 2.0 framing; any
// other value (including absence) is treated as 1.0/legacy. See bitcoind-api.md §1.
const jsonRPCVersion2 = "2.0"

// JSON-RPC error codes the proxy emits itself (subset of src/rpc/protocol.h).
const (
	CodeParseError     = -32700
	CodeInvalidRequest = -32600
	CodeMethodNotFound = -32601
	CodeInternalError  = -32603
)

// ErrMalformed is returned by Parse when the body is not valid JSON or its
// top-level value is neither an object (single call) nor an array (batch).
var ErrMalformed = errors.New("malformed JSON-RPC request body")

// Version is the JSON-RPC framing of a single call.
type Version int

const (
	// V1Legacy is JSON-RPC 1.0/1.1: replies carry both result and error keys.
	V1Legacy Version = iota
	// V2 is JSON-RPC 2.0: replies carry jsonrpc:"2.0" and exactly one of result/error.
	V2
)

// Call is one JSON-RPC call: either a single request or one batch element.
// A missing or non-string method leaves Method empty, which the handler treats
// as not-allowed. A call is a notification when HasID is false.
type Call struct {
	Method  string          // empty if absent or not a JSON string
	ID      json.RawMessage // echoed verbatim in replies; valid only when HasID
	Version Version
	HasID   bool // true when the request carried an id key (even if null)
}

// Envelope is the parsed request: a single call or a batch, plus the original
// body to forward verbatim when every call is allowed.
type Envelope struct {
	IsBatch bool
	Calls   []Call
	Raw     []byte
}

type rawCall struct {
	JSONRPC json.RawMessage `json:"jsonrpc"`
	Method  json.RawMessage `json:"method"`
	ID      json.RawMessage `json:"id"`
}

// Parse reads a JSON-RPC request body into an Envelope. It returns ErrMalformed
// for invalid JSON or a non-object/non-array top-level value. Per-call problems
// (missing/non-string method, non-object batch elements) are not errors: they
// yield a Call with an empty Method, which the handler denies.
func Parse(body []byte) (*Envelope, error) {
	trimmed := bytes.TrimSpace(body)
	if len(trimmed) == 0 || !json.Valid(trimmed) {
		return nil, ErrMalformed
	}
	switch trimmed[0] {
	case '{':
		return &Envelope{Calls: []Call{parseElement(trimmed)}, Raw: body}, nil
	case '[':
		var elems []json.RawMessage
		if err := json.Unmarshal(trimmed, &elems); err != nil {
			return nil, ErrMalformed
		}
		calls := make([]Call, len(elems))
		for i, el := range elems {
			calls[i] = parseElement(el)
		}
		return &Envelope{IsBatch: true, Calls: calls, Raw: body}, nil
	default:
		return nil, ErrMalformed
	}
}

func parseElement(raw json.RawMessage) Call {
	trimmed := bytes.TrimSpace(raw)
	call := Call{Version: V1Legacy}
	if len(trimmed) == 0 || trimmed[0] != '{' {
		return call // not an object: unclassifiable, denied downstream
	}
	var rc rawCall
	if err := json.Unmarshal(trimmed, &rc); err != nil {
		return call
	}
	call.Version = detectVersion(rc.JSONRPC)
	call.Method = extractString(rc.Method)
	if rc.ID != nil {
		call.HasID = true
		call.ID = rc.ID
	}
	return call
}

func detectVersion(raw json.RawMessage) Version {
	if extractString(raw) == jsonRPCVersion2 {
		return V2
	}
	return V1Legacy
}

// extractString returns the Go string for a JSON string value, or "" if raw is
// absent or not a JSON string.
func extractString(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var s string
	if err := json.Unmarshal(raw, &s); err != nil {
		return ""
	}
	return s
}

type errObj struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type replyV1 struct {
	Result any             `json:"result"` // always nil -> null
	Error  errObj          `json:"error"`
	ID     json.RawMessage `json:"id"`
}

type replyV2 struct {
	JSONRPC string          `json:"jsonrpc"`
	Error   errObj          `json:"error"`
	ID      json.RawMessage `json:"id"`
}

// ErrorReply builds a JSON-RPC error reply for this call, matching its version
// and echoing its id (null when absent). Used for deny and upstream-failure replies.
func (c Call) ErrorReply(code int, message string) json.RawMessage {
	id := c.ID
	if !c.HasID || !json.Valid(id) {
		id = json.RawMessage("null")
	}
	var reply any
	if c.Version == V2 {
		reply = replyV2{JSONRPC: jsonRPCVersion2, Error: errObj{code, message}, ID: id}
	} else {
		reply = replyV1{Error: errObj{code, message}, ID: id}
	}
	b, err := json.Marshal(reply)
	if err != nil {
		// Unreachable: id is validated above and message is a Go string.
		return json.RawMessage(`{"result":null,"error":{"code":-32603,"message":"internal error"}}`)
	}
	return b
}

// DenyError is the standard version-matched "method not allowed" reply (403 body).
func (c Call) DenyError() json.RawMessage {
	return c.ErrorReply(CodeMethodNotFound, "method '"+c.Method+"' not allowed by proxy policy")
}
