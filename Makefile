.PHONY: run install test

install:
	pip install -r requirements.txt -r requirements-dev.txt

run:
	./scripts/start.sh

test:
	python -m pytest -q
