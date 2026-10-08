.PHONY: setup test lint fmt demo study serve sample clean airflow-setup airflow-test docker

PY ?= python3
VENV := .venv
BIN := $(VENV)/bin

setup:            ## create a virtualenv and install the project with dev tools
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -e ".[dev]"

test:             ## unit + data-quality + integration tests with coverage
	$(BIN)/pytest --cov=pitlake --cov-report=term-missing

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

fmt:
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

demo:             ## build the lake from the offline sample and show a few answers
	$(BIN)/pitlake ingest
	$(BIN)/pitlake asof NVDA eps_diluted --period-type annual --as-of 2024-06-01
	$(BIN)/pitlake asof NVDA eps_diluted --period-type annual --as-of 2025-03-01
	$(BIN)/pitlake changes --ticker NVDA --classification split_adjustment --limit 3
	$(BIN)/pitlake corrections list

study:            ## regenerate the research example from the local lake
	$(BIN)/pitlake study --out docs/research/point-in-time-vs-latest.md

serve:            ## API + analyst page on http://127.0.0.1:8000 (docs at /docs)
	$(BIN)/pitlake serve

sample:           ## refresh data/sample from live SEC + GitHub (needs SEC_USER_AGENT)
	$(BIN)/python scripts/make_sample.py

clean:
	rm -rf data/lake data/bronze .pytest_cache .coverage .ruff_cache

AIRFLOW_VERSION := 3.3.2
AF := .venv-airflow/bin

airflow-setup:    ## separate Python 3.12 env with Airflow + this project
	python3.12 -m venv .venv-airflow
	$(AF)/pip install "apache-airflow==$(AIRFLOW_VERSION)" --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-$(AIRFLOW_VERSION)/constraints-3.12.txt"
	$(AF)/pip install -e . pytest

airflow-test:     ## DAG structure + end-to-end `airflow dags test` on the sample
	$(AF)/pytest tests/test_dag.py

docker:           ## build and run ingest + API in containers (http://localhost:8000)
	docker compose up --build
