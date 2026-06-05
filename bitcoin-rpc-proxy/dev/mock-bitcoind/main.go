// Mock bitcoind JSON-RPC backend for local bitcoin-rpc-proxy development.
//
// It speaks just enough of Bitcoin Core's wire protocol (docs/bitcoind-api.md) for
// the proxy to forward against: POST-only, hybrid JSON-RPC 1.0/2.0 with
// version-matched reply shapes, single and batch requests, optional Basic-auth
// 401, and a handful of canned method results. It is the test spy as a standalone
// binary; it executes nothing real (a "stop" is acknowledged but ignored).
package main

import (
	"encoding/base64"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"os"
)

// JSON-RPC error codes mirrored from src/rpc/protocol.h (subset).
const (
	codeParseError     = -32700
	codeInvalidRequest = -32600
	codeMethodNotFound = -32601
)

// jsonRPCVersion2 is the only jsonrpc value selecting 2.0 framing; anything else
// (including absence) is treated as 1.0/legacy. See bitcoind-api.md §1.
const jsonRPCVersion2 = "2.0"

// Canned values returned by the mock's RPC methods. Static: the proxy forwards
// them verbatim and never inspects the values.
const (
	cannedHeight        int64 = 901234
	cannedUptime        int64 = 123456
	cannedDifficulty          = 83126997340024.6
	cannedVerifyProg          = 0.9999998
	cannedBestBlockHash       = "00000000000000000000a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f6"
)

// rpcVersion is the JSON-RPC framing selected per request (bitcoind-api.md §1).
type rpcVersion int

const (
	verV1 rpcVersion = iota // 1.0/legacy: replies carry both result and error
	verV2                   // 2.0: replies carry jsonrpc:"2.0" and only one of result/error
)

type rpcReq struct {
	JSONRPC json.RawMessage `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params"`
}

type rpcErr struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

// rpcAuth, when set via MOCK_RPC_AUTH ("user:pass"), makes the mock require a
// matching HTTP Basic Authorization header and answer 401 otherwise. Lets the
// dev harness observe the proxy passing Authorization through verbatim.
var rpcAuth = os.Getenv("MOCK_RPC_AUTH")

func main() {
	addr := ":8332"
	if v := os.Getenv("MOCK_ADDR"); v != "" {
		addr = v
	}

	srv := &http.Server{Addr: addr, Handler: http.HandlerFunc(handle)}
	log.Printf("mock-bitcoind listening on %s (auth=%t)", addr, rpcAuth != "")
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		log.Fatal(err)
	}
}

func handle(w http.ResponseWriter, r *http.Request) {
	// bitcoind's JSON-RPC server handles only POST (bitcoind-api.md §2).
	if r.Method != http.MethodPost {
		http.Error(w, "JSONRPC server handles only POST requests", http.StatusMethodNotAllowed)
		return
	}
	if !authOK(r) {
		w.Header().Set("WWW-Authenticate", `Basic realm="jsonrpc"`)
		http.Error(w, "401 Unauthorized", http.StatusUnauthorized)
		return
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeReply(w, http.StatusInternalServerError, errorReply(verV1, nil, codeParseError, "read error"))
		return
	}

	// Batch (array) or single (object); anything else is a parse error.
	if len(body) > 0 && body[0] == '[' {
		handleBatch(w, body)
		return
	}
	var req rpcReq
	if err := json.Unmarshal(body, &req); err != nil {
		writeReply(w, http.StatusInternalServerError, errorReply(verV1, nil, codeParseError, "Parse error"))
		return
	}
	ver := detectVersion(req.JSONRPC)
	reply, rerr := dispatch(req)
	status := http.StatusOK
	if rerr != nil && ver != verV2 {
		status = httpStatus(rerr.Code) // V1 surfaces RPC errors as non-200 (bitcoind-api.md §1)
	}
	writeReply(w, status, reply)
}

// handleBatch replies with a JSON array; batches always return HTTP 200, with any
// per-element errors carried in the body (bitcoind-api.md §1, §5).
func handleBatch(w http.ResponseWriter, body []byte) {
	var reqs []rpcReq
	if err := json.Unmarshal(body, &reqs); err != nil {
		writeReply(w, http.StatusInternalServerError, errorReply(verV1, nil, codeParseError, "Parse error"))
		return
	}
	out := make([]json.RawMessage, 0, len(reqs))
	for _, req := range reqs {
		reply, _ := dispatch(req)
		out = append(out, reply)
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(out)
}

// dispatch returns the version-matched reply bytes for one call, plus the rpcErr
// (nil on success) so the single-request path can pick the V1 HTTP status.
func dispatch(req rpcReq) (json.RawMessage, *rpcErr) {
	ver := detectVersion(req.JSONRPC)
	switch req.Method {
	case "getblockcount":
		return successReply(ver, req.ID, cannedHeight), nil
	case "getblockchaininfo":
		return successReply(ver, req.ID, map[string]any{
			"chain":                "main",
			"blocks":               cannedHeight,
			"headers":              cannedHeight,
			"bestblockhash":        cannedBestBlockHash,
			"difficulty":           cannedDifficulty,
			"verificationprogress": cannedVerifyProg,
			"initialblockdownload": false,
			"pruned":               false,
		}), nil
	case "ping":
		return successReply(ver, req.ID, nil), nil
	case "uptime":
		return successReply(ver, req.ID, cannedUptime), nil
	case "stop":
		// Acknowledged but never acted on, so a request reaching the mock is
		// observable. In normal runs the proxy denies stop and it never arrives.
		return successReply(ver, req.ID, "Bitcoin Core stopping"), nil
	default:
		e := &rpcErr{Code: codeMethodNotFound, Message: "Method not found"}
		return errorReply(ver, req.ID, e.Code, e.Message), e
	}
}

func authOK(r *http.Request) bool {
	if rpcAuth == "" {
		return true
	}
	want := "Basic " + base64.StdEncoding.EncodeToString([]byte(rpcAuth))
	return r.Header.Get("Authorization") == want
}

func detectVersion(raw json.RawMessage) rpcVersion {
	var s string
	if err := json.Unmarshal(raw, &s); err != nil {
		return verV1
	}
	if s == jsonRPCVersion2 {
		return verV2
	}
	return verV1
}

func idOrNull(id json.RawMessage) json.RawMessage {
	if len(id) == 0 {
		return json.RawMessage("null")
	}
	return id
}

// successReply builds a version-matched success object. V1 carries both result and
// a null error; V2 carries jsonrpc:"2.0" and only result (bitcoind-api.md §1).
func successReply(ver rpcVersion, id json.RawMessage, result any) json.RawMessage {
	m := map[string]any{"id": idOrNull(id), "result": result}
	if ver == verV2 {
		m["jsonrpc"] = jsonRPCVersion2
	} else {
		m["error"] = nil
	}
	b, _ := json.Marshal(m)
	return b
}

// errorReply builds a version-matched error object. V1 carries a null result; V2
// carries jsonrpc:"2.0" and omits result.
func errorReply(ver rpcVersion, id json.RawMessage, code int, msg string) json.RawMessage {
	m := map[string]any{"id": idOrNull(id), "error": rpcErr{Code: code, Message: msg}}
	if ver == verV2 {
		m["jsonrpc"] = jsonRPCVersion2
	} else {
		m["result"] = nil
	}
	b, _ := json.Marshal(m)
	return b
}

func writeReply(w http.ResponseWriter, status int, reply json.RawMessage) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(reply)
}

// httpStatus maps an RPC error code to the HTTP status bitcoind uses for V1 replies
// (src/rpc/protocol.h HTTPStatusCode).
func httpStatus(code int) int {
	switch code {
	case codeMethodNotFound:
		return http.StatusNotFound
	case codeInvalidRequest, codeParseError:
		return http.StatusBadRequest
	default:
		return http.StatusInternalServerError
	}
}
