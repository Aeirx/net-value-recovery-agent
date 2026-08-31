PY ?= python

.PHONY: help install test lint typecheck check smoke boundary config reproduce clean

help:
	@echo "install    - install the package and dev dependencies"
	@echo "test       - run the test suite"
	@echo "lint       - ruff"
	@echo "typecheck  - mypy strict over world/ and agent/"
	@echo "check      - lint + typecheck + test  (what CI runs)"
	@echo "boundary   - run only the world/agent boundary guard"
	@echo "config     - write data/config_a.json and print its hash"
	@echo "smoke      - 50-transaction end-to-end smoke evaluation"
	@echo "reproduce  - regenerate every number in the README from scratch"

install:
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check netvalue tests scripts

typecheck:
	$(PY) -m mypy

check: lint typecheck test

boundary:
	$(PY) -m pytest tests/test_boundary.py -v

config:
	$(PY) scripts/write_config.py

smoke:
	$(PY) scripts/smoke_eval.py --n 50

# The single command that must regenerate every published number from a clean clone.
# Phases append their stage here as they land; it is never allowed to go stale.
reproduce: config
	$(PY) -m pytest -q
	@echo "--- Phase 3+ stages append here (datasets, baselines, agent, sweeps, report)"

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache reports/*.png reports/*.md
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
