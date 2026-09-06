PY   := ./.venv/bin/python
SEED ?= 1337
PORT ?= 8077
WORKERS ?= 4

# Every target listed in `help` is defined below. An earlier version of this
# file advertised bench, ab and export without defining them -- documentation
# that lied about its own tool, in a project about documentation that lies.

.PHONY: help lab lab-stop smoke detect recon learn settle apply probe run \
        bench status export ab ablate retire replay ui verify clean all

help:
	@echo "skeptic -- the agent that reverse-engineers its own tools"
	@echo ""
	@echo "  setup"
	@echo "    make lab            start the instrumented lab on :$(PORT)"
	@echo "    make lab-stop       stop it"
	@echo "    make smoke          prove all 14 hidden rules fire on the wire"
	@echo "    make detect         prove the contract layer sees all 14"
	@echo ""
	@echo "  the loop"
	@echo "    make recon          sweep the documented promises, mint hypotheses"
	@echo "    make settle         settle open hypotheses (concurrent probes)"
	@echo "    make apply          apply saved probe verdicts to the belief store"
	@echo "    make learn          recon + settle + apply, end to end"
	@echo "    make run            one task run against the lab"
	@echo ""
	@echo "  evidence"
	@echo "    make bench          score beliefs against ground truth"
	@echo "    make status         what is believed, and how sure"
	@echo "    make export         emit TOOLS.md + the guard manifest"
	@echo "    make ab             naive agent vs shim-equipped agent"
	@echo "    make ablate         memory / wiped / shuffled control"
	@echo "    make retire         watch a belief unlearn when the world changes"
	@echo "    make replay         what would these beliefs have saved?"
	@echo "    make verify         reproduce every number in the README"
	@echo ""
	@echo "  make ui             serve the panels for `ao preview`"

# --- setup -----------------------------------------------------------------

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

detect:
	@$(PY) -m agent.detect_smoke

# --- the loop --------------------------------------------------------------

recon:
	@$(PY) -m bench.session --cycles 0

settle:
	@$(PY) -u -m bench.settle --workers $(WORKERS)

apply:
	@$(PY) -m bench.apply_probes

learn: recon settle apply bench

run:
	@$(PY) cli.py run --task inventory

probe:
	@$(PY) cli.py probe --max-probes 2

# --- evidence --------------------------------------------------------------

bench:
	@$(PY) cli.py bench

status:
	@$(PY) cli.py status

export:
	@$(PY) -m shim.export

ab:
	@$(PY) -u -m bench.ab

ablate:
	@$(PY) -u -m bench.ablate

retire:
	@$(PY) -u -m bench.retire_demo

replay:
	@$(PY) -m replay.counterfactual

ui:
	@echo "serving on http://127.0.0.1:8099 -- then: ao preview http://127.0.0.1:8099"
	@$(PY) -m ui.server

# Reproduce every claim the README makes, in order.
verify: smoke detect bench replay
	@echo ""
	@echo "  the A/B and the ablation each cost real model calls; run them with:"
	@echo "    make ab"
	@echo "    make ablate"
	@echo "    make retire"

clean:
	@rm -rf runs/*.jsonl probes/*.json __pycache__ */__pycache__
	@echo "cleaned (beliefs and run history kept; use git to reset those)"
