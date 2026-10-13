# Requer uv (https://docs.astral.sh/uv/). O uv baixa o Python 3.12 sozinho se preciso,
# então não depende do python3 do sistema (3.9 no macOS, abaixo do mínimo 3.11).

PYTHON_VERSION ?= 3.12
VENV           := .venv
PY             := $(VENV)/bin/python
HOST           ?= 127.0.0.1
PORT           ?= 8000

.PHONY: help setup run test test-network clean

help:
	@echo "make setup         cria .venv e instala o projeto com extras de dev"
	@echo "make run           sobe o servidor em http://$(HOST):$(PORT) com --reload"
	@echo "make test          testes offline (fixtures)"
	@echo "make test-network  testes contra as APIs reais"
	@echo "make clean         remove .venv e caches"
	@echo ""
	@echo "Variáveis: OPENALEX_MAILTO, GRAPH_DB, PORT, HOST"

$(PY):
	uv venv --python $(PYTHON_VERSION) $(VENV)

setup: $(PY)
	uv pip install --python $(PY) -e ".[dev]"

run: $(PY)
	@if [ -z "$$OPENALEX_MAILTO" ]; then echo "aviso: OPENALEX_MAILTO não definido — OpenAlex vai aplicar limites menores"; fi
	$(PY) -m uvicorn app.main:app --reload --host $(HOST) --port $(PORT)

test: $(PY)
	$(PY) -m pytest

test-network: $(PY)
	$(PY) -m pytest -m network

clean:
	rm -rf $(VENV) .pytest_cache *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
