package rpc

import (
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestParseSingle(t *testing.T) {
	env, err := Parse([]byte(`{"jsonrpc":"2.0","method":"getblockcount","params":[],"id":7}`))
	require.NoError(t, err)
	assert.False(t, env.IsBatch)
	require.Len(t, env.Calls, 1)
	c := env.Calls[0]
	assert.Equal(t, "getblockcount", c.Method)
	assert.Equal(t, V2, c.Version)
	assert.True(t, c.HasID)
	assert.JSONEq(t, "7", string(c.ID))
}

func TestParseVersionDetection(t *testing.T) {
	cases := []struct {
		name string
		body string
		want Version
	}{
		{"absent", `{"method":"uptime"}`, V1Legacy},
		{"explicit 2.0", `{"jsonrpc":"2.0","method":"uptime"}`, V2},
		{"explicit 1.0", `{"jsonrpc":"1.0","method":"uptime"}`, V1Legacy},
		{"non-string", `{"jsonrpc":2.0,"method":"uptime"}`, V1Legacy},
		{"unknown string", `{"jsonrpc":"1.1","method":"uptime"}`, V1Legacy},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			env, err := Parse([]byte(tc.body))
			require.NoError(t, err)
			assert.Equal(t, tc.want, env.Calls[0].Version)
		})
	}
}

func TestParseMethodMissingOrNonString(t *testing.T) {
	cases := []string{
		`{}`,
		`{"params":[]}`,
		`{"method":123}`,
		`{"method":null}`,
		`{"method":["x"]}`,
	}
	for _, body := range cases {
		t.Run(body, func(t *testing.T) {
			env, err := Parse([]byte(body))
			require.NoError(t, err)
			assert.Empty(t, env.Calls[0].Method)
		})
	}
}

func TestParseNotificationVsNullID(t *testing.T) {
	notif, err := Parse([]byte(`{"method":"uptime"}`))
	require.NoError(t, err)
	assert.False(t, notif.Calls[0].HasID, "absent id is a notification")

	nullID, err := Parse([]byte(`{"method":"uptime","id":null}`))
	require.NoError(t, err)
	assert.True(t, nullID.Calls[0].HasID, "explicit null id is not a notification")
	assert.JSONEq(t, "null", string(nullID.Calls[0].ID))

	strID, err := Parse([]byte(`{"method":"uptime","id":"abc"}`))
	require.NoError(t, err)
	assert.JSONEq(t, `"abc"`, string(strID.Calls[0].ID))
}

func TestParseBatch(t *testing.T) {
	env, err := Parse([]byte(`[{"method":"getblockcount","id":1},{"jsonrpc":"2.0","method":"stop","id":2}]`))
	require.NoError(t, err)
	assert.True(t, env.IsBatch)
	require.Len(t, env.Calls, 2)
	assert.Equal(t, "getblockcount", env.Calls[0].Method)
	assert.Equal(t, V1Legacy, env.Calls[0].Version)
	assert.Equal(t, "stop", env.Calls[1].Method)
	assert.Equal(t, V2, env.Calls[1].Version)
}

func TestParseBatchNonObjectElement(t *testing.T) {
	env, err := Parse([]byte(`[{"method":"getblockcount","id":1}, 42, "x"]`))
	require.NoError(t, err)
	require.Len(t, env.Calls, 3)
	assert.Equal(t, "getblockcount", env.Calls[0].Method)
	assert.Empty(t, env.Calls[1].Method, "non-object element has no method")
	assert.Empty(t, env.Calls[2].Method)
}

func TestParseEmptyBatch(t *testing.T) {
	env, err := Parse([]byte(`[]`))
	require.NoError(t, err)
	assert.True(t, env.IsBatch)
	assert.Empty(t, env.Calls)
}

func TestParseRawPreserved(t *testing.T) {
	body := []byte(`  {"method":"getblockcount","id":1}  `)
	env, err := Parse(body)
	require.NoError(t, err)
	assert.Equal(t, body, env.Raw, "Raw must be the original body for verbatim forwarding")
}

func TestParseMalformed(t *testing.T) {
	cases := []string{``, `   `, `not json`, `{"method":`, `"a string"`, `42`, `true`, `null`}
	for _, body := range cases {
		t.Run(body, func(t *testing.T) {
			_, err := Parse([]byte(body))
			assert.ErrorIs(t, err, ErrMalformed)
		})
	}
}

func TestDenyErrorV1(t *testing.T) {
	env, err := Parse([]byte(`{"method":"stop","id":9}`))
	require.NoError(t, err)
	raw := env.Calls[0].DenyError()

	var got map[string]json.RawMessage
	require.NoError(t, json.Unmarshal(raw, &got))
	assert.Contains(t, got, "result")
	assert.JSONEq(t, "null", string(got["result"]))
	assert.JSONEq(t, "9", string(got["id"]))
	assert.NotContains(t, got, "jsonrpc")

	var e errObj
	require.NoError(t, json.Unmarshal(got["error"], &e))
	assert.Equal(t, CodeMethodNotFound, e.Code)
	assert.Contains(t, e.Message, "stop")
}

func TestDenyErrorV2(t *testing.T) {
	env, err := Parse([]byte(`{"jsonrpc":"2.0","method":"stop","id":"q"}`))
	require.NoError(t, err)
	raw := env.Calls[0].DenyError()

	var got map[string]json.RawMessage
	require.NoError(t, json.Unmarshal(raw, &got))
	assert.JSONEq(t, `"2.0"`, string(got["jsonrpc"]))
	assert.JSONEq(t, `"q"`, string(got["id"]))
	assert.NotContains(t, got, "result", "v2 error reply omits result")
}

func TestErrorReplyNullIDWhenAbsent(t *testing.T) {
	env, err := Parse([]byte(`{"method":"stop"}`)) // notification, no id
	require.NoError(t, err)
	raw := env.Calls[0].ErrorReply(CodeInternalError, "boom")

	var got map[string]json.RawMessage
	require.NoError(t, json.Unmarshal(raw, &got))
	assert.JSONEq(t, "null", string(got["id"]))
}

func TestDenyErrorEscapesMethodName(t *testing.T) {
	// A method name containing JSON metacharacters must not break the reply.
	env, err := Parse([]byte(`{"method":"evil\",\"x\":\"","id":1}`))
	require.NoError(t, err)
	raw := env.Calls[0].DenyError()
	assert.True(t, json.Valid(raw), "deny reply must be valid JSON")

	var got struct {
		Error errObj `json:"error"`
	}
	require.NoError(t, json.Unmarshal(raw, &got))
	assert.Contains(t, got.Error.Message, `evil","x":"`)
}

func FuzzParse(f *testing.F) {
	seeds := []string{
		`{"method":"getblockcount","id":1}`,
		`[{"method":"uptime"},{"method":"stop","id":2}]`,
		`[]`, `{}`, `null`, `42`, `"x"`, ``, `{"method":`,
	}
	for _, s := range seeds {
		f.Add([]byte(s))
	}
	f.Fuzz(func(t *testing.T, body []byte) {
		env, err := Parse(body)
		if err != nil {
			return
		}
		if !env.IsBatch {
			assert.Len(t, env.Calls, 1)
		}
		// Building replies must never panic for any parsed call.
		for _, c := range env.Calls {
			assert.True(t, json.Valid(c.DenyError()))
		}
	})
}
