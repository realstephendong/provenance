#!/usr/bin/env bash
#
# One entry point for Provenance. The Makefile has a target per step; this wires
# them into the orders you actually use, and refuses to start anything that would
# fail three minutes later for a reason it could have checked up front.
#
#   ./run.sh              bring the whole stack up, then serve  (default)
#   ./run.sh serve        just the service, assuming data is indexed
#   ./run.sh ingest       (re)index, following USE_MOCK_DATA
#   ./run.sh bot          the on-demand Slack bot
#   ./run.sh demo         one canned request against a running service
#   ./run.sh eval         the eval harness
#   ./run.sh mcp          the MCP stdio server
#   ./run.sh extension    compile the VS Code extension (for F5)
#   ./run.sh package      build provenance.vsix to install into VS Code
#   ./run.sh status       what is up, what is indexed
#   ./run.sh stop         stop the service and Elasticsearch
#
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PY=.venv/bin/python
ES_URL=http://localhost:9200
SERVICE_URL=http://127.0.0.1:8000

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
die()  { printf '\033[31merror\033[0m %s\n' "$*" >&2; exit 1; }

# `.env` is the source of truth and is not exported into this shell, so read the
# one value we branch on rather than sourcing a file full of secrets.
env_get() {
  [ -f .env ] || return 0
  sed -n "s/^$1=//p" .env | tail -1 | tr -d '[:space:]'
}

# Mirrors config.USE_MOCK_DATA: anything but these counts as true.
use_mock() {
  case "$(env_get USE_MOCK_DATA | tr '[:upper:]' '[:lower:]')" in
    0|false|no|off) return 1 ;;
    *) return 0 ;;
  esac
}

up() { curl -sf --max-time 2 "$1" >/dev/null 2>&1; }

# --- preflight ----------------------------------------------------------------
# Every check here maps to something that otherwise fails mid-run: a missing venv
# after ES is already up, or a service that boots and refuses on its first query.
preflight() {
  [ -f .env ] || die ".env is missing.  cp .env.example .env, then add OPENAI_API_KEY"
  [ -n "$(env_get OPENAI_API_KEY)" ] || die "OPENAI_API_KEY is empty in .env. Every entrypoint refuses to boot without it."

  if [ ! -x "$PY" ]; then
    bold "installing (no .venv yet)"
    make install
  fi

  # USE_MOCK_DATA=false makes GitHub mandatory: it is the only source of a PR's
  # title, author and merge date, and the service refuses to boot without it.
  if ! use_mock; then
    [ -n "$(env_get GITHUB_TOKEN)" ] && [ -n "$(env_get GITHUB_REPO)" ] \
      || die "USE_MOCK_DATA=false needs GITHUB_TOKEN and GITHUB_REPO in .env (or set USE_MOCK_DATA=true)"
  fi
}

ensure_es() {
  if up "$ES_URL/_cluster/health"; then
    info "elasticsearch already up"
  else
    command -v docker >/dev/null || die "docker is not installed or not on PATH (Elasticsearch runs in it)"
    bold "starting elasticsearch"
    make es
  fi
}

ensure_seed() {
  # Only the fixture corpus needs generating; live Slack has no seed step.
  use_mock || return 0
  if [ -d seed/repo ] && [ -d seed/slack ]; then
    info "seed corpus already built"
  else
    bold "building seed corpus"
    make seed
  fi
}

# How many documents are indexed, or empty if the service/index is not reachable.
doc_count() {
  curl -sf --max-time 3 "$ES_URL/slack_threads/_count" 2>/dev/null \
    | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("count",""))' 2>/dev/null || true
}

ensure_index() {
  local n; n=$(doc_count)
  if [ -n "$n" ] && [ "$n" -gt 0 ] 2>/dev/null; then
    info "index already has $n documents (./run.sh ingest to rebuild)"
  else
    do_ingest
  fi
}

# --- commands -----------------------------------------------------------------
do_ingest() {
  # Both targets pass --recreate, so they replace each other rather than merging.
  if use_mock; then
    bold "indexing the seed export (USE_MOCK_DATA=true)"
    make ingest
  else
    bold "indexing live Slack (USE_MOCK_DATA=false)"
    [ -n "$(env_get SLACK_BOT_TOKEN)" ] || die "live ingest needs SLACK_BOT_TOKEN in .env"
    make ingest-slack
  fi
}

do_serve() {
  up "$SERVICE_URL/health" && die "something is already listening on :8000 (./run.sh stop first)"
  bold "serving on $SERVICE_URL   (ctrl-c to stop)"
  info "extension: open extension/ in VS Code and press F5"
  make serve
}

do_bot() {
  # The bot's own preflight is thorough; these two checks just fail faster and name
  # the file to edit. slack_bolt is imported lazily, so a stale venv surfaces here.
  for v in SLACK_BOT_TOKEN SLACK_APP_TOKEN; do
    [ -n "$(env_get "$v")" ] || die "the Slack bot needs $v in .env (see README, \"The Slack bot\")"
  done
  "$PY" -c 'import slack_bolt' 2>/dev/null || { bold "installing slack_bolt"; make install; }
  make slackbot
}

do_status() {
  bold "provenance status"
  info "branch      $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?') @ $(git rev-parse --short HEAD 2>/dev/null || echo '?')"
  use_mock && info "mode        USE_MOCK_DATA=true (seed fixtures)" \
           || info "mode        USE_MOCK_DATA=false (live GitHub/Jira/Sentry)"
  [ -x "$PY" ] && info "venv        $($PY -V)" || info "venv        missing"
  up "$ES_URL/_cluster/health" && info "elastic     up at $ES_URL" || info "elastic     down"
  if up "$SERVICE_URL/health"; then
    curl -sf "$SERVICE_URL/health" | "$PY" -c '
import json,sys
d=json.load(sys.stdin); s=d.get("source") or {}
print(f"  service     up, {d.get(\"docs\",0)} docs indexed (source: {s.get(\"source\",\"?\")})")
print(f"  embedder    {d.get(\"embedder_id\")}")
'
  else
    local n; n=$(doc_count)
    info "service     down  (./run.sh serve)"
    [ -n "$n" ] && info "index       $n documents in elasticsearch" \
                || info "index       empty or unreachable  (./run.sh ingest)"
  fi
}

do_stop() {
  pkill -f "uvicorn provenance.service.main" 2>/dev/null && info "service stopped" || info "service was not running"
  docker compose down 2>/dev/null && info "elasticsearch stopped" || true
}

case "${1:-all}" in
  all)       preflight; ensure_es; ensure_seed; ensure_index; do_serve ;;
  serve)     preflight; ensure_es; do_serve ;;
  ingest)    preflight; ensure_es; ensure_seed; do_ingest ;;
  bot)       preflight; ensure_es; do_bot ;;
  demo)      up "$SERVICE_URL/health" || die "the service is not running (./run.sh serve in another shell)"
             "$PY" scripts/demo_request.py ;;
  eval)      preflight; make eval ;;
  mcp)       preflight; make mcp ;;
  extension) make extension; info "now open extension/ in VS Code and press F5" ;;
  package)   # A .vsix you can install into your everyday VS Code, rather than the
             # F5 Extension Development Host. vsce is fetched on demand, not vendored.
             make extension
             ( cd extension && npx --yes @vscode/vsce package --no-dependencies \
                 --allow-missing-repository -o provenance.vsix )
             info "built extension/provenance.vsix"
             info "install: code --install-extension extension/provenance.vsix" ;;
  status)    do_status ;;
  stop)      do_stop ;;
  -h|--help|help) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0" ;;
  *)         die "unknown command '$1'  (try: ./run.sh --help)" ;;
esac
