.PHONY: check lint type templates test install db-upgrade

PYTHON ?= python

install:
	$(PYTHON) -m pip install -e ".[dev]"

lint:
	ruff check .

type:
	mypy src

templates:
	djlint src/wodbuster_worker/templates --lint

test:
	pytest -m "not live_contract"

db-upgrade:
	alembic upgrade head

check: lint type templates test
