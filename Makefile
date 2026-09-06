.PHONY: lint typecheck test build install clean

lint:
	python3 -m compileall -q novel_agent tests
	git diff --check

typecheck:
	python3 -m compileall -q novel_agent

test:
	python3 -m unittest discover -s tests -v

build:
	python3 -m compileall -q novel_agent static tests

# Editable install exposes the `novel-agent-server` / `novel-agent-worker`
# console scripts and the `novel_agent` import without copying source.
install:
	python3 -m pip install -e .

# Remove only build/pycache artifacts. The data/ directory holds the real
# novel content and is intentionally never deleted here.
clean:
	find novel_agent tests -type d -name __pycache__ -prune -exec rm -rf {} +
	find novel_agent tests -name '*.pyc' -delete
	rm -rf build dist *.egg-info
