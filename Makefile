SHELL := /bin/bash

TOX := uv run --group dev tox
CHARMCRAFT_ARGS ?=

.PHONY: lock fmt-test lint-test unit-test charm-test build-charm integration-test clean

lock:
	uv lock

fmt-test:
	$(TOX) -e format

lint-test:
	$(TOX) -e lint

unit-test:
	$(TOX) -e unit

charm-test: fmt-test lint-test unit-test

build-charm:
	charmcraft pack $(CHARMCRAFT_ARGS)

integration-test:
	$(TOX) -e integration

clean:
	charmcraft clean
	rm -rf .coverage .pytest_cache .ruff_cache .tox build parts prime stage
