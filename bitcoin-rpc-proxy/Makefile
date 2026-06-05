BUILD_TIME_UTC := $(shell date -u +'%Y-%m-%d_%H:%M:%S')
COMMIT  := $(shell git rev-parse --short HEAD)
VERSION := $(shell cat VERSION)
VERSION_LABEL ?= $(VERSION)

.PHONY: build fmt lint run test explain

BINARY := bin/bitcoin-rpc-proxy
CMD := ./cmd/bitcoin-rpc-proxy

.DEFAULT_GOAL := explain

explain:
	@echo "Usage: make [target]"
	@echo ""
	@echo "Options for test targets:"
	@echo "  [N=...]              - Number of times to run tests (default 1)"
	@echo "  [RACE=1]             - Run tests with race detector"
	@echo "  [VERSION_LABEL=...]  - Override version label"
	@echo "  [V=1]                - Enable verbose output"
	@echo ""
	@echo "Targets:"
	@echo "  build            - Build the proxy binary for the host OS/Arch."
	@echo "  fmt              - Format the code."
	@echo "  lint             - Run golangci-lint (see .golangci.yml)."
	@echo "  run              - Run the proxy."
	@echo "  test             - Run unit tests."
	@echo "  explain          - Display this help message."

# Number of times to run tests, default 1
N ?= 1

# Flag V=1 for verbose mode
TEST_FLAGS :=
ifdef V
	TEST_FLAGS += -v
endif
ifdef RACE
	TEST_FLAGS += -race
endif

build:
	@echo "==> Building binary"
	go build -ldflags "-X main.buildTimeUTC=$(BUILD_TIME_UTC) -X main.commit=$(COMMIT) -X main.version=$(VERSION_LABEL)" -o $(BINARY) $(CMD)

fmt:
	@echo "==> Formatting code"
	gofmt -s -w .

lint:
	@echo "==> Running golangci-lint"
	golangci-lint run

run:
	@echo "==> Running bitcoin-rpc-proxy"
	go run $(CMD)

test:
	@echo "==> Running test suite"
	go test -shuffle=on -count=$(N) $(TEST_FLAGS) ./...
