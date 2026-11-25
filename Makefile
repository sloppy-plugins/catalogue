.DEFAULT_GOAL := help
SHELL := /bin/sh
PYTHON ?= python3
PYPI_INDEX ?= https://pypi.org/simple/
SDK_SOURCE ?=
PLUGIN_ROOT ?= ..
-include local.mk
export PYTHONDONTWRITEBYTECODE := 1
export PYTHONWARNINGS := error
PY := $(CURDIR)/.venv/bin/python
ifeq ($(OS),Windows_NT)
PY := $(CURDIR)/.venv/Scripts/python.exe
endif

.PHONY: help deps validate sync sync-check test check

help: ## List catalogue commands
	@awk 'BEGIN {FS = ":.*## "} /^[a-z0-9-]+:.*## / {printf "  %-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

deps: ## Install the public SDK catalogue tools (or select a standalone SDK checkout)
	$(PYTHON) -m venv "$(CURDIR)/.venv"
	"$(PY)" -W default -m pip install --index-url "$(PYPI_INDEX)" --upgrade pip
	"$(PY)" -m pip install --index-url "$(PYPI_INDEX)" "$(if $(SDK_SOURCE),$(SDK_SOURCE),sloppy-sdk==0.1.2)"

validate: ## Validate the catalogue format and every embedded manifest
	"$(PY)" -m sloppy_sdk.catalogue validate catalogue.json
	"$(PY)" -m sloppy_sdk.catalogue validate catalogue.public.json
	"$(PY)" -c 'from pathlib import Path; import sys; sys.path.insert(0, "scripts"); from discover import load_sources; load_sources(Path("sources.json"))'

sync: ## Add verified Git, npm, and PyPI releases from configured sources
	"$(PY)" scripts/sync-releases.py --plugins-dir "$(PLUGIN_ROOT)"

sync-check: ## Check configured releases without updating the catalogue
	"$(PY)" scripts/sync-releases.py --plugins-dir "$(PLUGIN_ROOT)" --check

test: ## Check synchronization using isolated local remotes
	"$(PY)" -m unittest discover -s tests -v

check: validate test ## Run catalogue validation and synchronization tests
