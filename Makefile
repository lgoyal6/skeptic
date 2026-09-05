PY := ./.venv/bin/python
SEED ?= 1337
PORT ?= 8077

.PHONY: help lab lab-stop smoke run probe bench ablate ab export verify demo clean

help:
	@echo "skeptic -- the agent that reverse-engineers its own tools"
	@echo ""
	@echo "  make lab          start the instrumented lab tool on :$(PORT)"
	@echo "  make smoke        prove all 14 hidden rules fire on the wire"
	@echo "  make run          one task run against the lab"
	@echo "  make bench SEED=n full run set, scored against ground truth"
	@echo "  make ablate       same agent with memory wiped (the control)"
	@echo "  make ab           naive agent vs shim-equipped agent"
	@echo "  make export       emit TOOLS.md + the live MCP shim"
	@echo "  make verify       reproduce every number in the README"

lab:
	@pkill -f "uvicorn lab.server" 2>/dev/null || true
	@sleep 1
	@LAB_SEED=$(SEED) $(PY) -m uvicorn lab.server:app --host 127.0.0.1 --port $(PORT) --log-level warning & \
		sleep 3; curl -s http://127.0.0.1:$(PORT)/healthz && echo " lab up on :$(PORT) seed=$(SEED)"

lab-stop:
	@pkill -f "uvicorn lab.server" 2>/dev/null || true
	@echo "lab stopped"

smoke:
	@curl -s -X POST http://127.0.0.1:$(PORT)/_control/reset -H 'content-type: application/json' -d '{"seed":$(SEED)}' > /dev/null
	@$(PY) lab/smoke.py

clean:
	@rm -rf runs/*.jsonl probes/*.json __pycache__ */__pycache__
	@echo "cleaned"
