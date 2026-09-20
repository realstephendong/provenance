'use strict';
// Probes shared by the start and stop pipelines: Elasticsearch, Docker, the API service.

const fs = require('fs');
const { run, which, httpGetJson, StepError } = require('./util');

const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]', '::1']);

function isLocalUrl(url) {
  try { return LOCAL_HOSTS.has(new URL(url).hostname); } catch { return true; }
}

/** Cluster health JSON if Elasticsearch answers and is not red, else null. */
async function esHealth(esUrl) {
  const r = await httpGetJson(`${esUrl}/_cluster/health`);
  if (r && r.status === 200 && r.json && r.json.status && r.json.status !== 'red') return r.json;
  return null;
}

async function indexCount(esUrl, index) {
  const r = await httpGetJson(`${esUrl}/${index}/_count`);
  if (!r || r.status !== 200 || !r.json) return 0; // 404 => index does not exist yet
  return Number(r.json.count) || 0;
}

async function dockerUp() {
  if (!which('docker')) return false;
  const r = await run('docker', ['info'], { timeoutMs: 25000 });
  return r.code === 0;
}

/** `docker compose` (v2) or the legacy `docker-compose`. */
async function composeCommand() {
  const v2 = await run('docker', ['compose', 'version'], { timeoutMs: 15000 });
  if (v2.code === 0) return { cmd: 'docker', args: ['compose'] };
  if (which('docker-compose')) return { cmd: 'docker-compose', args: [] };
  throw new StepError('Neither "docker compose" nor "docker-compose" is available', {
    hint: 'Install Docker Desktop (it includes Compose): https://www.docker.com/products/docker-desktop/',
  });
}

/**
 * What is on the service port?
 *   none        nothing answered
 *   provenance  a Provenance /health response ({ok:boolean, api_key_present, ...})
 *   other       something else is listening
 */
async function probeService(serviceUrl) {
  const r = await httpGetJson(`${serviceUrl}/health`);
  if (!r) return { kind: 'none' };
  const j = r.json;
  if (j && typeof j.ok === 'boolean' && 'api_key_present' in j) {
    return { kind: 'provenance', ok: j.ok, body: j };
  }
  return { kind: 'other', status: r.status };
}

function readPid(pidFile) {
  try {
    const n = parseInt(fs.readFileSync(pidFile, 'utf8'), 10);
    return Number.isInteger(n) && n > 0 ? n : null;
  } catch {
    return null;
  }
}

module.exports = {
  isLocalUrl, esHealth, indexCount, dockerUp, composeCommand, probeService, readPid,
};
