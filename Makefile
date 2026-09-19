# PYTHON is overridable: the project targets 3.12, but any 3.12+ interpreter works.
#   make install PYTHON=python3.13
PYTHON ?= python3.12
PY := .venv/bin/python

.PHONY: install es seed ingest ingest-incremental reconcile serve mcp eval calibrate \
        extension demo clean slack-check ingest-slack ingest-slack-incremental reconcile-slack test

install:
	$(PYTHON) -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r requirements.txt

es:
	docker compose up -d
	@echo "waiting for elasticsearch..."
	@until curl -sf http://localhost:9200/_cluster/health > /dev/null; do sleep 1; done
	@echo "elasticsearch is up"

seed:
	$(PY) seed/build_seed.py

ingest:
	$(PY) -m provenance.ingest --export seed/slack --mode backfill --recreate

ingest-incremental:
	$(PY) -m provenance.ingest --export seed/slack --mode incremental

reconcile:
	$(PY) -m provenance.ingest --export seed/slack --mode reconcile

# Live Slack: needs SLACK_USER_TOKEN in .env (see README, "Connecting to Slack").
slack-check:
	$(PY) -m provenance.ingest.slack_check

ingest-slack:
	$(PY) -m provenance.ingest --source slack --mode backfill --recreate

ingest-slack-incremental:
	$(PY) -m provenance.ingest --source slack --mode incremental

reconcile-slack:
	$(PY) -m provenance.ingest --source slack --mode reconcile

test:
	$(PY) -m pytest tests -q

serve:
	.venv/bin/uvicorn provenance.service.main:app --reload --port 8000

mcp:
	$(PY) -m provenance.mcp_server.server

eval:
	$(PY) evals/run_eval.py

calibrate:
	$(PY) evals/run_eval.py --calibrate

extension:
	cd extension && npm install && npm run compile

demo: es seed ingest
	@echo "now: make serve, then F5 in extension/, or: make mcp"

clean:
	rm -rf seed/repo seed/slack .es_storage extension/out
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
