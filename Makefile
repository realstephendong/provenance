PY := .venv/bin/python

.PHONY: install qdrant seed ingest serve mcp eval extension demo clean

install:
	python3.12 -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r requirements.txt

qdrant:
	docker compose up -d
	@echo "dashboard: http://localhost:6333/dashboard"

seed:
	$(PY) seed/build_seed.py

ingest:
	$(PY) -m hindsight.ingest --export seed/slack --recreate

serve:
	.venv/bin/uvicorn hindsight.service.main:app --reload --port 8000

mcp:
	$(PY) -m hindsight.mcp_server.server

eval:
	$(PY) evals/run_eval.py

extension:
	cd extension && npm install && npm run compile

# Cold start to demo-ready.
demo: qdrant seed ingest
	@echo "now: make serve, then F5 in extension/"

clean:
	rm -rf seed/repo seed/slack .qdrant_storage
