.PHONY: test gate demo validate eval clean single

test:
	python3 -m unittest discover -s tests -t . -v

validate:
	python3 -m keel.cli validate --include-rejected

eval:
	python3 -m keel.cli eval

demo:
	python3 -m keel.cli demo

single:
	python3 build_single_file.py
	cd dist && python3 keel.py gate

# What CI runs. Any failure blocks the merge.
gate:
	python3 -m keel.cli validate
	python3 -m unittest discover -s tests -t .
	python3 -m keel.cli eval --gate
	python3 build_single_file.py --check

verify-status:
	python3 keel_verify.py status

clean:
	rm -f keel.db keel.db-wal keel.db-shm audit.jsonl
	find . -name __pycache__ -type d -exec rm -rf {} +
