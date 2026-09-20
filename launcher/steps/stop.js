'use strict';
// The --stop pipeline: same Step shape as the start pipeline.
//   1. end the uvicorn process recorded in .run/service.pid
//   2. `docker compose stop` (the Elasticsearch container; its data volume is kept)

const fs = require('fs');
const {
  IS_WIN, sleep, run, runOrThrow, isPidAlive, killTree,
} = require('../util');
const { probeService, readPid, isLocalUrl, dockerUp, composeCommand } = require('../services');

/** Best-effort name/command of a PID, to avoid killing an unrelated process that reused it. */
async function describeProcess(pid) {
  if (IS_WIN) {
    const r = await run('tasklist', ['/FI', `PID eq ${pid}`, '/FO', 'CSV', '/NH'], { timeoutMs: 10000 });
    const m = /^"([^"]+)"/.exec(r.lines.find((l) => l.startsWith('"')) || '');
    return m ? m[1] : '';
  }
  const r = await run('ps', ['-p', String(pid), '-o', 'command='], { timeoutMs: 10000 });
  return r.output.trim();
}

const looksLikeService = (desc) => (IS_WIN ? /python/i.test(desc) : /uvicorn/.test(desc));

const stopService = {
  id: 'stop-service',
  title: 'API service',

  async check(ctx) {
    const pid = readPid(ctx.paths.servicePid);
    if (!pid) {
      const probe = await probeService(ctx.service.url);
      if (probe.kind === 'provenance') {
        return {
          done: true,
          status: 'warn',
          note: `a service is running on port ${ctx.service.port} but was not started by provenance-start (no pid file); stop it manually`,
        };
      }
      return { done: true, note: 'not running' };
    }
    if (!isPidAlive(pid)) {
      fs.rmSync(ctx.paths.servicePid, { force: true });
      return { done: true, note: 'not running (stale pid file removed)' };
    }
    return { done: false, pid, note: `stopping pid ${pid}` };
  },

  async run(ctx, { pid }) {
    const desc = await describeProcess(pid);
    if (!looksLikeService(desc)) {
      fs.rmSync(ctx.paths.servicePid, { force: true });
      return {
        status: 'warn',
        note: `pid ${pid} is "${desc || 'unknown'}", not a Provenance service; left it running and removed the stale pid file`,
      };
    }
    killTree(pid, { group: !IS_WIN });
    for (let i = 0; i < 20 && isPidAlive(pid); i++) await sleep(500);
    if (isPidAlive(pid)) {
      killTree(pid, { force: true, group: !IS_WIN });
      await sleep(500);
    }
    fs.rmSync(ctx.paths.servicePid, { force: true });
    return { note: 'stopped' };
  },
};

const stopElasticsearch = {
  id: 'stop-elasticsearch',
  title: 'Elasticsearch',

  async check(ctx) {
    if (!isLocalUrl(ctx.esUrl)) return { done: true, note: 'external Elasticsearch, left alone' };
    if (!(await dockerUp())) return { done: true, note: 'Docker is not running' };
    return { done: false, note: 'docker compose stop' };
  },

  async run(ctx) {
    const compose = await composeCommand();
    await runOrThrow(compose.cmd, [...compose.args, '-f', ctx.paths.compose, 'stop'], ctx.child(),
      'docker compose stop');
    return { note: 'container stopped (data kept in .es_storage)' };
  },
};

module.exports = [stopService, stopElasticsearch];
