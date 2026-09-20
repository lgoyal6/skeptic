PY   := ./.venv/bin/python
SEED ?= 1337
PORT ?= 8077
WORKERS ?= 2

# Every target listed in `help` is defined below. An earlier version of this
# file advertised bench, ab and export without defining them -- documentation
# that lied about its own tool, in a project about documentation that lies.

.PHONY: help lab lab-stop smoke detect recon learn settle apply probe run \
        bench status export ab ablate retire replay ui verify clean all \
        test replay-corpus gate mutations policies drift evaluate bundle offline capture

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
	@echo "  offline evidence (no network, no credentials, no model calls)"
	@echo "    make replay-corpus  replay all 10 fixtures and check every hash"
	@echo "    make gate           the CI regression gate: replay, compile, enforce"
	@echo "    make mutations      restore each old bug, prove a test dies"
	@echo "    make policies       four-arm probe-selection comparison"
	@echo "    make drift          a real API version change, classified"
	@echo "    make evaluate       the whole evaluation record in one command"
	@echo "    make bundle         export and verify one structured replay bundle"
	@echo "    make offline        everything above, start to finish (~1 min)"
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

# --- offline evidence -------------------------------------------------------
# None of these touch the network, need a credential, or spend a model call.
# They are what an independent checkout runs to check this project's claims.

test:
	@$(PY) -m pytest -q

replay-corpus:
	@$(PY) -m bench.replay_corpus

gate:
	@$(PY) -m bench.ci

mutations:
	@$(PY) -m bench.mutations

policies:
	@$(PY) -m bench.policy_eval --seeds 50

drift:
	@$(PY) -m bench.drift_demo

evaluate:
	@$(PY) -m bench.evaluate --write

bundle:
	@$(PY) -m bench.run_bundle --tool frankfurter --version v2-2026-09-19 --policy greedy --seed $(SEED)
	@$(PY) -m bench.run_bundle --verify runs/bundles/frankfurter-v2-2026-09-19-greedy-seed$(SEED).json

# Everything an outside reader needs, in one command, from a clean clone.
offline: test replay-corpus gate policies drift evaluate bundle
	@echo ""
	@echo "  offline verification complete: suite, corpus replay, regression gate,"
	@echo "  policy comparison, drift timeline, evaluation record, and run bundle."

# Re-record the fixtures from live services. Needs network; everything else
# does not. Raw captures are immutable, so this refuses to overwrite one.
capture:
	@$(PY) -m fixtures.capture --all

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
