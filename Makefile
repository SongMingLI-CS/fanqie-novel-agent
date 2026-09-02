.PHONY: lint typecheck test build
lint:
	python3 -m compileall -q novel_agent tests
	git diff --check
typecheck:
	python3 -m compileall -q novel_agent
test:
	python3 -m unittest discover -s tests -v
build:
	python3 -m compileall -q novel_agent static tests
