// Package common provides shared utilities (logging) for the proxy.
package common

import (
	"fmt"
	"os"
	"path"
	"runtime"
	"strings"
	"time"

	"github.com/rs/zerolog"
	"github.com/rs/zerolog/pkgerrors"
)

const defaultLogLevel = zerolog.InfoLevel

// callerSkip is the stack depth at which the originating call site sits when
// the hook runs (hook -> zerolog event -> log call).
const callerSkip = 3

// callerHook adds caller file:line to warn+ levels, and also at debug/trace.
type callerHook struct {
	level zerolog.Level
}

// Run implements zerolog.Hook.
func (h callerHook) Run(e *zerolog.Event, level zerolog.Level, _ string) {
	if level < zerolog.WarnLevel &&
		h.level != zerolog.DebugLevel &&
		h.level != zerolog.TraceLevel {
		return
	}
	_, file, line, ok := runtime.Caller(callerSkip)
	if !ok {
		return
	}
	e.Str("caller", fmt.Sprintf("%s:%d", path.Base(file), line))
}

// NewLogger returns a zerolog.Logger with pretty console output at INFO level.
func NewLogger() zerolog.Logger {
	return NewLoggerWithLevel("")
}

// NewLoggerWithLevel returns a zerolog.Logger with pretty console output at the
// given level. Priority: level arg > LOG_LEVEL env var > INFO.
func NewLoggerWithLevel(level string) zerolog.Logger {
	zerolog.ErrorStackMarshaler = pkgerrors.MarshalStack
	zerolog.TimeFieldFormat = zerolog.TimeFormatUnix

	lvlStr := level
	if lvlStr == "" {
		lvlStr = os.Getenv("LOG_LEVEL")
	}

	logLevel, err := zerolog.ParseLevel(strings.ToLower(lvlStr))
	if logLevel == zerolog.NoLevel || err != nil {
		logLevel = defaultLogLevel
	}
	zerolog.SetGlobalLevel(logLevel)

	logger := zerolog.New(
		zerolog.ConsoleWriter{Out: os.Stdout, TimeFormat: time.DateTime},
	).Level(logLevel).With().Timestamp().Logger()

	return logger.Hook(callerHook{level: logLevel})
}
