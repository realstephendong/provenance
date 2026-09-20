'use strict';
// The shared context every step receives. Built once, in bin/provenance-start.js.

const path = require('path');
const { readJson, writeJson } = require('./util');

function buildContext({ repoRoot, userCwd, flags, log }) {
  const runDir = path.join(repoRoot, '.run');
  const extensionDir = path.join(repoRoot, 'extension');
  const isWin = process.platform === 'win32';

  const paths = {
    repoRoot,
    runDir,
    state: path.join(runDir, 'state.json'),
    serviceLog: path.join(runDir, 'service.log'),
    servicePid: path.join(runDir, 'service.pid'),
    launcherLog: path.join(runDir, 'launcher.log'),
    launcherLock: path.join(runDir, 'launcher.lock'),
    envFile: path.join(repoRoot, '.env'),
    compose: path.join(repoRoot, 'docker-compose.yml'),
    requirements: path.join(repoRoot, 'requirements.txt'),
    venvDir: path.join(repoRoot, '.venv'),
    venvPython: isWin
      ? path.join(repoRoot, '.venv', 'Scripts', 'python.exe')
      : path.join(repoRoot, '.venv', 'bin', 'python'),
    extensionDir,
    vsix: path.join(extensionDir, 'provenance.vsix'),
    checkpoint: path.join(repoRoot, '.provenance', 'ingest_checkpoint.json'),
    seedSlack: path.join(repoRoot, 'seed', 'slack'),
    buildSeed: path.join(repoRoot, 'seed', 'build_seed.py'),
  };

  const state = readJson(paths.state, {});

  const ctx = {
    repoRoot,
    userCwd,
    flags,
    log,
    paths,
    // Replaced by the config step: process env layered over provenance/.env.
    env: { ...process.env },
    esUrl: 'http://localhost:9200',
    index: 'slack_threads',
    service: { host: '127.0.0.1', port: 8000, url: 'http://127.0.0.1:8000' },
    slackToken: '',
    python: null,
    codeBin: null,
    state,
    summaryRows: [],

    /** Merge values into .run/state.json (hashes, timestamps). */
    saveState(patch) {
      Object.assign(state, patch);
      writeJson(paths.state, state);
    },
    /** Options for child processes: always the repo root as cwd, the merged env, and
     *  child output streamed into the terminal under the running step. */
    child(extra = {}) {
      return { cwd: repoRoot, env: ctx.env, onLine: (l) => log.detail(l), ...extra };
    },
    addSummary(label, value) {
      ctx.summaryRows = ctx.summaryRows.filter(([k]) => k !== label);
      ctx.summaryRows.push([label, value]);
    },
  };
  return ctx;
}

module.exports = { buildContext };
