.PHONY: test lint demo

test:
	python3 -m unittest discover -s tests

lint:
	claude plugin validate ./plugin --strict
	claude plugin validate . --strict

# Exit 1 means code still uses a dropped name, which the example does on purpose.
demo:
	python3 plugin/scripts/stillused.py examples/shop || test $$? -eq 1
