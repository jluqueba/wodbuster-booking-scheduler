.PHONY: check lint type test install

PYTHON ?= python

install:
	$(PYTHON) -m pip install -e ".[dev]"

lint:
	ruff check .

type:
	mypy src

test:
	pytest -m "not live_contract"

check: lint type test
