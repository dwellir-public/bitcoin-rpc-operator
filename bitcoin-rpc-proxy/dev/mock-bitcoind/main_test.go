package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// post sends body to the mock handler and returns status and decoded body.
func post(t *testing.T, srv *httptest.Server, body string, header http.Header) (int, string) {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, srv.URL+"/", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	for k, vs := range header {
		req.Header[k] = vs
	}
	resp, err := srv.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(b)
}

func TestWireShapes(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(handle))
	defer srv.Close()

	t.Run("v1 success carries result and null error, no jsonrpc", func(t *testing.T) {
		code, body := post(t, srv, `{"method":"getblockcount","id":1}`, nil)
		if code != http.StatusOK {
			t.Fatalf("status = %d", code)
		}
		var m map[string]json.RawMessage
		if err := json.Unmarshal([]byte(body), &m); err != nil {
			t.Fatal(err)
		}
		if _, ok := m["jsonrpc"]; ok {
			t.Error("v1 reply must omit jsonrpc")
		}
		if string(m["error"]) != "null" {
			t.Errorf("v1 error = %s, want null", m["error"])
		}
		if string(m["result"]) == "" {
			t.Error("v1 reply must carry result")
		}
		if string(m["id"]) != "1" {
			t.Errorf("id = %s, want 1", m["id"])
		}
	})

	t.Run("v2 success carries jsonrpc and no error key", func(t *testing.T) {
		code, body := post(t, srv, `{"jsonrpc":"2.0","method":"getblockcount","id":7}`, nil)
		if code != http.StatusOK {
			t.Fatalf("status = %d", code)
		}
		var m map[string]json.RawMessage
		if err := json.Unmarshal([]byte(body), &m); err != nil {
			t.Fatal(err)
		}
		if string(m["jsonrpc"]) != `"2.0"` {
			t.Errorf("jsonrpc = %s", m["jsonrpc"])
		}
		if _, ok := m["error"]; ok {
			t.Error("v2 success must omit error key")
		}
	})

	t.Run("batch returns array with 200", func(t *testing.T) {
		code, body := post(t, srv, `[{"method":"getblockcount","id":1},{"method":"ping","id":2}]`, nil)
		if code != http.StatusOK {
			t.Fatalf("status = %d", code)
		}
		var arr []json.RawMessage
		if err := json.Unmarshal([]byte(body), &arr); err != nil {
			t.Fatalf("batch body not an array: %v", err)
		}
		if len(arr) != 2 {
			t.Fatalf("array len = %d, want 2", len(arr))
		}
	})

	t.Run("unknown method: v1 -> 404, v2 -> 200, both -32601", func(t *testing.T) {
		code, body := post(t, srv, `{"method":"nope","id":3}`, nil)
		if code != http.StatusNotFound {
			t.Errorf("v1 unknown status = %d, want 404", code)
		}
		if !strings.Contains(body, "-32601") {
			t.Errorf("missing -32601: %s", body)
		}
		code, body = post(t, srv, `{"jsonrpc":"2.0","method":"nope","id":3}`, nil)
		if code != http.StatusOK {
			t.Errorf("v2 unknown status = %d, want 200", code)
		}
		if !strings.Contains(body, "-32601") {
			t.Errorf("missing -32601: %s", body)
		}
	})

	t.Run("non-POST -> 405", func(t *testing.T) {
		resp, err := srv.Client().Get(srv.URL + "/")
		if err != nil {
			t.Fatal(err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode != http.StatusMethodNotAllowed {
			t.Errorf("GET status = %d, want 405", resp.StatusCode)
		}
	})
}

func TestAuth(t *testing.T) {
	rpcAuth = "alice:secret"
	defer func() { rpcAuth = "" }()
	srv := httptest.NewServer(http.HandlerFunc(handle))
	defer srv.Close()

	code, _ := post(t, srv, `{"method":"getblockcount","id":1}`, nil)
	if code != http.StatusUnauthorized {
		t.Errorf("no auth status = %d, want 401", code)
	}

	h := http.Header{}
	h.Set("Authorization", "Basic YWxpY2U6c2VjcmV0") // base64("alice:secret")
	code, _ = post(t, srv, `{"method":"getblockcount","id":1}`, h)
	if code != http.StatusOK {
		t.Errorf("authed status = %d, want 200", code)
	}
}
