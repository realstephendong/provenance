# PYTHON is overridable: the project targets 3.12, but any 3.12+ interpreter works.
#   make install PYTHON=python3.13
PYTHON ?= python3.12
PY := .venv/bin/python

.PHONY: install es seed ingest ingest-incremental reconcile serve mcp eval calibrate \
        extension demo clean slack-check ingest-slack ingest-slack-incremental reconcile-slack \
        slackbot deploy deploy-logs deploy-down local local-status local-purge

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

# The on-demand bot: /provenance and the "Index in Provenance" message shortcut.
# Shared long-running process; needs SLACK_BOT_TOKEN, SLACK_APP_TOKEN, and an allowlist.
slackbot:
	$(PY) -m provenance.slackbot

# Shared team deployment: Elasticsearch + API + workspace Slack bot.
# Configure .env first; see README, "Deploying the shared Slack bot".
deploy:
	@test -f .env || (echo "missing .env: cp .env.example .env, then configure it"; exit 1)
	@grep -Eq '^[[:space:]]*GITHUB_APP_PRIVATE_KEY_FILE[[:space:]]*=[[:space:]]*[^[:space:]#]+' .env || (echo "missing GITHUB_APP_PRIVATE_KEY_FILE in .env"; exit 1)
	docker compose -f docker-compose.deploy.yml up -d --build

deploy-logs:
	docker compose -f docker-compose.deploy.yml logs -f --tail=100

deploy-down:
	docker compose -f docker-compose.deploy.yml down

serve:
	.venv/bin/uvicorn provenance.service.main:app --reload --port 8000

# --- the private half of Backfill (this machine only) ---------------------------
# Normally the extension starts the connector and you never run these. They exist so
# a person can see and delete their own private index without the editor's
# cooperation -- a deletion you can only reach through the UI that created the data
# is not really a deletion.
#
# Pin the port if you use the browser sign-in: Slack matches the redirect URL exactly.
#   PROVENANCE_LOCAL_PORT=51737 make local
local:
	$(PY) -m provenance.local_agent.main serve

local-status:
	$(PY) -m provenance.local_agent.main status

local-purge:
	$(PY) -m provenance.local_agent.main purge

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
