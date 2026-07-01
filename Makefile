.PHONY: check lint type test install db-upgrade

PYTHON ?= python

install:
	$(PYTHON) -m pip install -e ".[dev]"

lint:
	ruff check .

type:
	mypy src

test:
	pytest -m "not live_contract"

db-upgrade:
	alembic upgrade head

check: lint type test
