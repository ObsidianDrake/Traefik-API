SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

PYTHON ?= python3
VENV ?= venv
VENV_PYTHON := $(VENV)/bin/python

.PHONY: all venv backend executable clean

all: executable

venv:
	@echo "==========================================="
	@echo "1. Preparing virtual environment..."
	@echo "==========================================="
	@if [ -x "$(VENV_PYTHON)" ]; then \
		echo "Virtual environment already exists: $(VENV)"; \
	elif [ -e "$(VENV)" ]; then \
		echo "Error: $(VENV) exists but has no executable bin/python." >&2; \
		exit 1; \
	else \
		$(PYTHON) -m venv "$(VENV)"; \
	fi

backend: venv
	@echo "==========================================="
	@echo "2. Installing build dependencies..."
	@echo "==========================================="
	"$(VENV_PYTHON)" -m pip install -r requirements-build.txt

executable: backend
	@echo "==========================================="
	@echo "3. Building standalone executable..."
	@echo "==========================================="
	"$(VENV_PYTHON)" -m PyInstaller --clean --noconfirm --onefile \
		--name traefik-route-api \
		--specpath build \
		--hidden-import uvicorn.logging \
		--hidden-import uvicorn.loops.asyncio \
		--hidden-import uvicorn.protocols.http.h11_impl \
		--hidden-import uvicorn.lifespan.on \
		run_api.py
	@echo "==========================================="
	@echo "Build complete: dist/traefik-route-api"
	@echo "Load your environment settings, then run the executable."
	@echo "==========================================="

clean:
	@echo "Cleaning binary build artifacts..."
	rm -rf build dist
