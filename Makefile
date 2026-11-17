.PHONY: test arch snapshot run stop status benchmark gate e2e

PYTHON ?= .venv/bin/python

test:
	$(PYTHON) -m pytest -q

arch:
	$(PYTHON) -m pytest tests/test_architecture.py -q

snapshot:
	UPDATE_SNAPSHOT=1 $(PYTHON) -m pytest tests/test_single_turn_snapshot.py -q

run:
	./scripts/service.sh start

stop:
	./scripts/service.sh stop

status:
	./scripts/service.sh status

benchmark:
	$(PYTHON) scripts/eval_multiturn.py --base http://127.0.0.1:8002 --out eval_mt.json

gate:
	bash scripts/release_gate.sh

e2e:
	bash scripts/e2e_browser.sh
