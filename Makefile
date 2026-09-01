PY ?= python

.PHONY: help install test lint typecheck check smoke boundary config datasets baselines estimator diagnosis reproduce clean

help:
	@echo "install    - install the package and dev dependencies"
	@echo "test       - run the test suite"
	@echo "lint       - ruff"
	@echo "typecheck  - mypy strict over world/ and agent/"
	@echo "check      - lint + typecheck + test  (what CI runs)"
	@echo "boundary   - run only the world/agent boundary guard"
	@echo "config     - write data/config_a.json and print its hash"
	@echo "datasets   - generate and freeze dataset_a, dataset_b and history"
	@echo "baselines  - run all four baselines with confidence intervals"
	@echo "estimator  - fit the recovery estimator and validate its calibration"
	@echo "diagnosis  - score every diagnosis arm (free; --live costs money)"
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

datasets:
	$(PY) scripts/generate_datasets.py

baselines:
	$(PY) scripts/run_baselines.py --config a --replications 30

estimator:
	$(PY) scripts/fit_estimator.py

diagnosis:
	$(PY) scripts/run_diagnosis.py --config a

smoke:
	$(PY) scripts/smoke_eval.py --n 50

# The single command that must regenerate every published number from a clean clone.
# Phases append their stage here as they land; it is never allowed to go stale.
# The datasets are frozen and committed, so reproduce verifies rather than regenerates:
# tests/test_determinism.py re-runs the generator and compares against the manifest.
reproduce: config
	$(PY) -m pytest -q
	$(PY) scripts/smoke_eval.py --n 50
	$(PY) scripts/run_baselines.py --config a --replications 30
	$(PY) scripts/fit_estimator.py
	$(PY) scripts/run_diagnosis.py --config a
	@echo "--- Phase 7+ stages append here (value engine, agent, sweeps)"

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache reports/*.png reports/*.md
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
