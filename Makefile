export PYTHONPATH=src

.PHONY: test eval

test:
	python -m unittest discover -s tests

eval:
	python -m member_services.harness
