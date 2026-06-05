package server

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"net/http"

	"github.com/rs/zerolog"
)

type contextKey string

const (
	requestIDKey contextKey = "request_id"
	loggerKey    contextKey = "logger"
)

const requestIDBytes = 4

// RequestID returns the request ID stored in ctx, or "" if absent.
func RequestID(ctx context.Context) string {
	if id, ok := ctx.Value(requestIDKey).(string); ok {
		return id
	}
	return ""
}

// Logger returns the request-scoped logger in ctx, or a no-op logger if absent.
func Logger(ctx context.Context) zerolog.Logger {
	if l, ok := ctx.Value(loggerKey).(zerolog.Logger); ok {
		return l
	}
	return zerolog.Nop()
}

func newRequestID() string {
	b := make([]byte, requestIDBytes)
	if _, err := rand.Read(b); err != nil {
		return "00000000"
	}
	return hex.EncodeToString(b)
}

// withRequestID attaches a short request ID and a request-scoped logger to the
// context, and echoes the ID in the X-Request-ID response header.
func withRequestID(next http.Handler, baseLogger zerolog.Logger) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := newRequestID()
		logger := baseLogger.With().
			Str("request_id", id).
			Str("http_method", r.Method).
			Str("path", r.URL.Path).
			Logger()

		w.Header().Set("X-Request-ID", id)

		ctx := context.WithValue(r.Context(), requestIDKey, id)
		ctx = context.WithValue(ctx, loggerKey, logger)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// panicRecovery converts a panic in a downstream handler into a 500 response.
func panicRecovery(next http.Handler, logger zerolog.Logger) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				logger.Error().
					Str("request_id", RequestID(r.Context())).
					Interface("panic", rec).
					Msg("panic recovered")
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusInternalServerError)
				_, _ = w.Write([]byte(`{"result":null,"error":{"code":-32603,"message":"internal error"},"id":null}`))
			}
		}()
		next.ServeHTTP(w, r)
	})
}
